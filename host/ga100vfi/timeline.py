#!/usr/bin/env python3
"""把一轮批次的日志拆成两份可判读的证据：时刻归因 + 坐标清点。

为什么要有这个工具（2026-11-26）：固件从 0.3.x 起就在免费记每一笔计划步的
`st <i> act=<a> want=<us> got=<us> dur=<us>`，以及每一发的 `slip / reset_slip`，而我们**一次都没有
按 act 分组统计过**。结果就是"我方时刻不确定（A 类随机）"这句话被引用了十几次，却没人回答过
"到底是哪一步在抖、抖多少"。没有这个分布，任何"要把时间表做成确定性的"改动都是凭感觉；有了它，
才知道该不该动优先级/预装 FIFO，以及动了之后有没有效。

两份报告：

  timing  —— 从 board.log 读 `st`/`glitch done`：每个 act 的到达偏差 (got-want) 与总线阻塞时长
            (dur) 的 n/min/p50/p90/max，以及 reset_slip 的分布。**只看数字，不改任何东西。**
            判读规矩写在结论里：如果散布集中在 act=8（GA_VMIN 收尾那笔）而释放沿（act=4）本身
            很稳，那"释放时刻不准"就不是命中问题。

  shots   —— 从 agent.log（或 verdicts.tsv）读每一发的坐标与读数：按 **PMBus 码**清点这一批到底
            覆盖了多少个物理坐标（tries=40 / codes=3 就是“抄了 37 发重复”），脏读比例
            （stable=NO），以及 config 应答与第一次开 BAR0 的时刻分布。
            **这两列不许读成“卡的静默有多久”**：t_cfg 回答的只是“卡答话有多快”，而卡再没答话的
            那些发压根不在分布里（表会自己声明这是幸存者分布）；t_bar0 的下界就是我们自己定的
            FIRST_READ_DELAY。要把静默下限调小，需要的是**没答话的那些发**的分布，不是这两列的
            平均数 —— 用没死过的样本证明不会死，是本台架付过学费的错法（README 的“引用 A 类
            之前先给出这两张表”同一条纪律）。

用法：
    python3 -m ga100vfi.timeline ~/ga100vfi/logs/20261126-0212-crack-0.5.5-2x222_372
    python3 -m ga100vfi.timeline <run_dir> --only shots
    python3 -m ga100vfi.timeline /path/to/board.log --only timing
退出码：0 = 报告出来了；2 = 两个文件都读不到（不要拿空报告当"没有抖动"）。
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

from ga100vfi.envelope import HEALTHY_CRC, HEALTHY_DSOV, mv_to_code

# glitch_plan.h 的 act 枚举。名字写在这里是为了让统计**可读**：一张只有数字的表不会告诉我们
# "抖的是收尾那笔写"，而这句话是决定要不要动固件的依据。
ACT_NAMES = {
    1: "GA_VOUT", 2: "GA_MODE", 3: "GA_RST_LOW", 4: "GA_RST_REL",
    5: "GA_SCL_LOW", 6: "GA_SCL_REL", 7: "GA_MODE_ON", 8: "GA_VMIN",
    9: "GA_VS", 10: "GA_CLR",
}

_ST = re.compile(r"^\s*st\s+(\d+)\s+act=(\d+)\s+want=(-?\d+)\s+got=(-?\d+)\s+dur=(\d+)us")
_DONE = re.compile(r"\bslip=(-?\d+)\s+reset_slip=(-?\d+)")
_STRETCH = re.compile(r"\bstretch_us=(-?\d+)")     # 卡把 SCL 拉了多久（坑的真实迟到量）
_REANCHOR = re.compile(r"\breanchor=(\d+)")        # 这一发跑在哪种几何模式下（0.5.9+）
_VS = re.compile(r"^\s*vs\s+t=([-+]?\d+)us\s+v=([-+]?\d+)mV")   # 固件用 %+ld 打时刻，带正号
_SHOT_HDR = re.compile(r"^\s*glitch\s+vd=(-?\d+)\b.*\bdelay=(-?\d+)\b")
# 判决行：与 bench/crack.sh 的解析器同一条骨架，额外字段（code=/stable=/t_cfg=）都是可选的。
_VERDICT = re.compile(r"attempt\s+(\d+)\s+delay=(-?\d+)\s+vd=(-?\d+)(.*?)\s+->\s+(\w+)")
_CODE = re.compile(r"\bcode=(\d+)")
_STABLE = re.compile(r"\bstable=(yes|NO)")
_TCFG = re.compile(r"\bt_cfg=([\d.]+)s")
_TBAR0 = re.compile(r"\bt_bar0=([\d.]+)s")
_DSOV = re.compile(r"\bDSOV=0x([0-9a-fA-F]{1,8})")
_CRC = re.compile(r"\bCRC=0x([0-9a-fA-F]{1,8})")


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def _fmt(values: list[int], unit: str = "µs") -> str:
    if not values:
        return "—"
    return (f"n={len(values)} min={min(values)}{unit} p50={_pct(values, 0.5)}{unit} "
            f"p90={_pct(values, 0.9)}{unit} max={max(values)}{unit}")


def parse_board_log(text: str) -> dict:
    """`st` 逐笔 + 每发 slip/reset_slip/stretch_us + 轨压采样。纯读，不做判断。"""
    steps: dict[int, dict[str, list[int]]] = defaultdict(lambda: {"slip": [], "dur": []})
    slips: list[int] = []
    reset_slips: list[int] = []
    stretch: list[int] = []
    reanchor: list[int] = []
    rails: list[tuple[int, int]] = []
    shots = 0
    for line in text.splitlines():
        m = _ST.match(line)
        if m:
            _i, act, want, got, dur = m.groups()
            act = int(act)
            steps[act]["slip"].append(int(got) - int(want))
            steps[act]["dur"].append(int(dur))
            continue
        if _SHOT_HDR.match(line):
            shots += 1
            continue
        m = _DONE.search(line)
        if m:
            slips.append(int(m.group(1)))
            reset_slips.append(int(m.group(2)))
            s = _STRETCH.search(line)
            if s:
                stretch.append(int(s.group(1)))
            r = _REANCHOR.search(line)
            if r:
                reanchor.append(int(r.group(1)))
            continue
        m = _VS.match(line)
        if m:
            rails.append((int(m.group(1)), int(m.group(2))))
    return {"steps": dict(steps), "slips": slips, "reset_slips": reset_slips,
            "stretch": stretch, "reanchor": reanchor,
            "rails": rails, "shot_headers": shots}


def report_timing(board_log: str) -> str:
    d = parse_board_log(board_log)
    out = ["== 时刻归因（board.log）=="]
    if not d["steps"]:
        out.append("   没有 `st` 行：这一批没开逐笔记录（swin=0 且不是 0.5.3+ 的 profile 路径），"
                   "A 类随机性无法评估 —— 别把空表读成『没有抖动』。")
    for act in sorted(d["steps"]):
        name = ACT_NAMES.get(act, f"act{act}")
        s = d["steps"][act]
        spread = max(s["slip"]) - min(s["slip"]) if s["slip"] else 0
        out.append(f"   {name:>10}  到达偏差(got-want) {_fmt(s['slip'])}  极差={spread}µs")
        out.append(f"   {'':>10}  总线阻塞(dur)      {_fmt(s['dur'])}")
    if d["reset_slips"]:
        r = d["reset_slips"]
        if 1 in d["reanchor"]:
            out.append(f"   reset_slip 分布 {_fmt(r)}  ← 本批重锚开启：这列是**相对坑落下之后**的准点率，"
                       "归我们（压不动是执行器的问题，不是卡的）")
        elif d["reanchor"]:
            out.append(f"   reset_slip 分布 {_fmt(r)}  ← 本批 GA_REANCHOR=0：这列相对 t0，里面混着卡拉伸 SCL 的"
                       "时长（看 stretch_us），不许当成我们自己的抖动")
        else:
            out.append(f"   reset_slip 分布 {_fmt(r)}  ← ≤0.5.8 没有 reanchor 标记：这列相对 t0。今天逐 act"
                       "核对过：它几乎等于前一笔 VOUT 写的 dur（写结束 +114 → 释放 +118），也就是说它是"
                       "**阻塞式总线写**的排队时间，不是抢占，也不是命中窗")
        out.append(f"   glitch slip  分布 {_fmt(d['slips'])}")
    if d["stretch"]:
        out.append(f"   stretch_us 分布 {_fmt(d['stretch'])}  ← 卡把 SCL 拉了多久（计划 36µs）：这是卡侧真实"
                   "行为，也是坑的真实迟到量。重锚开启时它被整条波形吸收，pw/gap/td 才是真实几何；"
                   "不开时同一发的坑实际长度随机在 108~202µs（配方写的是 222）")
    if d["rails"]:
        lo_t, lo = min(d["rails"], key=lambda tv: tv[1])
        base = [v for t, v in d["rails"] if v > 700]
        out.append(f"   0x8b 轨压采样 {len(d['rails'])} 点：最低 {lo} mV @ {lo_t:+d}µs，"
                   f"前置基线中位 {statistics.median(base) if base else '—'} mV"
                   "（命令值与轨上真值的差，就是『vd 这条轴到底动了什么』的答案；"
                   "最低点出现在释放之后 = 那是卡的浪涌，不是我们的坑）")
    out.append("   判读：先确认散布落在哪一步，再决定动不动代码；引用 A 类之前先给出这张表。")
    return "\n".join(out)


def parse_agent_log(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        m = _VERDICT.search(line)
        if not m:
            continue
        attempt, delay, vd, mid, verdict = m.groups()
        vd = int(vd)
        code = int(_CODE.search(mid).group(1)) if _CODE.search(mid) else mv_to_code(vd)
        stable = _STABLE.search(mid)
        t_cfg, t_bar0 = _TCFG.search(mid), _TBAR0.search(mid)
        dsov = _DSOV.search(mid)
        crc = _CRC.search(mid)
        rows.append({"attempt": int(attempt), "delay": int(delay), "vd": vd, "code": code,
                     "verdict": verdict,
                     "stable": (stable.group(1) == "yes") if stable else None,
                     "t_cfg": float(t_cfg.group(1)) if t_cfg else None,
                     "t_bar0": float(t_bar0.group(1)) if t_bar0 else None,
                     "dsov": int(dsov.group(1), 16) if dsov else None,
                     "crc": int(crc.group(1), 16) if crc else None})
    return rows


def report_shots(agent_log: str) -> str:
    rows = parse_agent_log(agent_log)
    out = ["== 坐标清点（agent.log）=="]
    if not rows:
        out.append("   没有判决行 —— 这一批要么没人判（看 board.log 的 phase），要么日志不全。")
        return "\n".join(out)
    by_code: dict[tuple[int, int], list[str]] = defaultdict(list)
    for r in rows:
        by_code[(r["code"], r["delay"])].append(r["verdict"])
    out.append(f"   {len(rows)} 发判决，覆盖 {len(by_code)} 个 (码, delay) 坐标；"
               f"码空间只有 14 个（300..380 mV / 6.25 mV）")
    for (code, delay), verdicts in sorted(by_code.items()):
        rep = Counter(verdicts)
        out.append(f"   code={code:<3}({code_to_mv_str(code)}) delay={delay:<4} "
                   f"{len(verdicts)} 发  " + " ".join(f"{k}×{v}" for k, v in rep.items()))
    worst = max(by_code.items(), key=lambda kv: len(kv[1]))
    if len(worst[1]) > 1:
        out.append(f"   最重复的坐标：code={worst[0][0]} delay={worst[0][1]} 打了 {len(worst[1])} 发。"
                   " 想重复同一发是锤击（有意），想搜索却重复就是白烧 —— 两者要在台账里分得开。")
    stamped = [r for r in rows if r["stable"] is not None]
    if stamped:
        dirty = [r for r in stamped if r["stable"] is False]
        out.append(f"   盖章率 {len(stamped) - len(dirty)}/{len(stamped)}；"
                   f"未盖章 {len(dirty)} 发 —— 这些行的读数既不能当反应、也不能当没反应")
    else:
        out.append("   这批没有 stable= 字段（<0.5.5 的代理）：脏读与真反应同形，判决里的 CRC 变化"
                   "只能当线索，命中必须用 burn status 的 window: OPEN 判")
    tc = [r["t_cfg"] for r in rows if r["t_cfg"] is not None]
    tb = [r["t_bar0"] for r in rows if r["t_bar0"] is not None]
    if tc:
        # t_cfg 的量法是"代理看见 awaiting_verdict 起，config 第一次应答"，探它只用**读 config**
        # （红线 1 允许；BAR0 仍然只在 floor 之后开）。所以它说的是**卡答话有多快**，不是**卡沉默
        # 多久** —— 这两件事在"能不能调小静默下限"这个问题上经常被混为一谈。
        out.append(f"   config 应答时刻 {_fmt(tc, 's')}（{len(tc)}/{len(rows)} 发有值）")
    missing = len(rows) - len(tc)
    if tc and missing:
        # 幸存者偏差的披露：卡再没答话的那些发**根本不在上面那份分布里**（t_cfg=None）。一份
        # "p90=0.0s" 的分布如果只由活着的发组成，它证明的恰好是需要它证明的反面。
        out.append(f"   [!!] {missing} 发没有 config 应答时刻 = 那些发里卡到判决结束都没答话。"
                   "上面那份是**幸存者分布**：单凭它调小静默下限，等于用没死过的样本证明不会死")
    elif tc:
        out.append("   （本批每发都有 config 应答 —— 这份没有幸存者偏差，但它只说明'答话快'）")
    elif rows and not tb:
        # 整批都没有这两列 = 代理比 0.5.5 旧，不是"卡没答话"。把没记当成没答，会指挥人去查一条
        # 根本不存在的故障（与 stable= 那一句同一个道理：缺字段与缺证据不是一件事）。
        out.append("   这批没有 t_cfg=/t_bar0= 字段（<0.5.5 的代理）：既不能说卡答话快，"
                   "也不能说它没答话 —— 调下限的凭证要从这一版起才开始攒")
    if tb:
        # min(t_bar0) 就是我们自己定的 FIRST_READ_DELAY：它量的是闸门，不是卡。
        out.append(f"   第一次开 BAR0  {_fmt(tb, 's')}  ← 最小值 = 代理的 FIRST_READ_DELAY 本身，"
                   "它量的是我们定的闸门，不是卡的静默")
    off = [r for r in rows if r["dsov"] is not None and r["dsov"] != HEALTHY_DSOV]
    dirty_crc = [r for r in rows if r["crc"] is not None and r["crc"] != HEALTHY_CRC]
    out.append(f"   DSOV 离开基线 {len(off)} 发；CRC 离开 0x55 {len(dirty_crc)} 发")
    if off:
        out.append("   [!!] 有反应：这几发的坐标要单独冻结，别让梯子继续替我们下注")
    return "\n".join(out)


def code_to_mv_str(code: int) -> str:
    from ga100vfi.envelope import code_to_mv
    return f"{code_to_mv(code)}mV"


def report(run: Path, only: str = "both") -> tuple[str, int]:
    """run 可以是运行目录，也可以直接是 board.log / agent.log。"""
    if run.is_dir():
        board = (run / "board.log").read_text(errors="replace") if (run / "board.log").exists() else ""
        agent = (run / "agent.log").read_text(errors="replace") if (run / "agent.log").exists() else ""
    else:
        if run.name.startswith("board"):
            board, agent = run.read_text(errors="replace"), ""
        elif run.name.startswith("agent"):
            board, agent = "", run.read_text(errors="replace")
        else:
            board = agent = ""
    parts, found = [], False
    if only in ("both", "timing") and board:
        parts.append(report_timing(board))
        found = True
    if only in ("both", "shots") and agent:
        parts.append(report_shots(agent))
        found = True
    if not found:
        return (f"timeline: {run} 里没有可解析的 board.log/agent.log —— "
                "空报告不能读成『没有抖动』", 2)
    return "\n\n".join(parts), 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run", help="运行目录（含 board.log/agent.log），或其中一个文件")
    ap.add_argument("--only", choices=("both", "timing", "shots"), default="both")
    args = ap.parse_args(argv)
    text, code = report(Path(args.run).expanduser(), args.only)
    print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
