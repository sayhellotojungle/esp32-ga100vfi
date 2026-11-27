#!/usr/bin/env python3
"""把"这一批没反应"拆成"什么都没打到"与"打早了"：批前/批末各抓一次 FF 行，比对 row 18。

为什么值得多开两次 BAR0：熔丝链的读取顺序里 **RIR sense 排在其它 sense 之前**（博客原话
"chronological checkpoint"），所以"控制行没翻（DSOV 仍 1）但 row 18 变了"不是噪声，而是
**相位偏早**的确证，而且方向是给的：φ 往晚移一格。现行判决只看 DSOV/CRC，这种一发被读成
`nothing`，于是"这一带打了 240 发 0 反应"里可能混着一堆"其实碰到了，只是碰早了"。

代价与红线：BAR0 是本台架唯一造成过整机死机的通路（红线 1：`FUSECTRL != IDLE` 时一个 BAR0
都不开）。所以这里是**每批两次**（批前基线、批末比对，都在卡健康、控制器 IDLE 时做），
不是每发一次 —— 每发一次会把 60 发变成 120 次 BAR0 会话，那个量级我否掉了。

比的是**与本机自己基线的差异**，不是绝对值：RIR 内容随 SKU 与个体不同，别人的 row 18 没有
任何参考价值（"绝不抄配方"同一条）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 早期分析点名的那一行（未烧项里的 chain2 PCIe 位所在行）。可用 EARLY_ROW 覆盖，
# 换卡/换代时不必改代码 —— 但要换得有凭证：先抓一次基线再改，否则第一份 post 会整片"变了"。
EARLY_ROW = 18


def load(path: str | Path) -> dict:
    """读一份 capture。形状必须与 `burn.py capture -o x.json` 真写出来的那个一致：
    `{"ff_rows": {"18": 305419896, ...}, ...}`（键是十进制行号字符串，值是整数）。
    同时收 `rows`（测试与本仓早期 dump 的形状）与 `0x` 字符串值。
    这里认错形状的后果很难看：整片"没读到"，于是 every 批都判成 nojudge，而那句话听起来
    像是"卡不配合"，实际是我们的解析器不对。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    rows = data.get("ff_rows") or data.get("rows") or (data if all(
        str(k).isdigit() for k in data) else {})
    out = {}
    for key, value in rows.items():
        try:
            out[int(key)] = int(str(value), 0)
        except (TypeError, ValueError):
            continue
    return out


def changed(pre: dict, post: dict) -> dict:
    """只在两边都读到的行上比。单边缺行不是"变了"，是"没读到"。"""
    return {row: (pre[row], post[row]) for row in sorted(set(pre) & set(post))
            if pre[row] != post[row]}


def verdict(pre: dict, post: dict, watch: int = EARLY_ROW) -> tuple[str, str]:
    """返回 (档位, 一句话依据)。档位：nojudge / nothing / early / partial。

    `nojudge` 与 `nothing` 的分别是这条规则的全部意义：批末没抓到 = **没有凭证**，把它写成
    "没反应"就会把"我们看不见"读成"卡没反应"，进而把加深当成合理下一步 —— 本台架为这类混淆
    付过两次发数的学费（`dud` 与 `nothing` 不能同形）。
    """
    if not pre or not post:
        return "nojudge", "批前或批末没抓到行（BAR0 一次没开成或卡不在）：无凭证，不能算 nothing"
    diff = changed(pre, post)
    if not diff:
        return "nothing", f"{len(set(pre) & set(post))} 行逐字节同基线"
    rows = ", ".join(f"{r}:0x{a:08x}->0x{b:08x}" for r, (a, b) in sorted(diff.items())[:6])
    if watch in diff and len(diff) == 1:
        return "early", (f"只有 row {watch} 翻了（{rows}）—— 打到了 RIR sense 而控制行没翻："
                         "**相位偏早**，φ 往晚移一格；这不是「没反应」，也不许当成加深许可")
    if watch in diff:
        return "partial", f"row {watch} 与另外 {len(diff) - 1} 行一起翻了（{rows}）：部分行被采歪，就地精修"
    return "partial", f"row {watch} 没动，但 {len(diff)} 行变了（{rows}）：有扰动，相位/深度都要重估"


def report(pre_path: str | Path, post_path: str | Path, watch: int = EARLY_ROW) -> str:
    def safe(path):
        try:
            return load(path)
        except (OSError, ValueError):
            return {}
    pre, post = safe(pre_path), safe(post_path)
    tier, why = verdict(pre, post, watch)
    lines = [f"early 档判读：{tier} —— {why}"]
    if tier == "early":
        lines.append("   处置：停批，φ 往晚一格（`delay` 减小 / 晚相位），深度**不动**；"
                     "命中判据仍是 DSOV=0 + window: OPEN，这一档只是方向凭证")
    elif tier == "partial":
        lines.append("   处置：就地精修，禁止换坐标（`partial` 是最接近成功的一档）")
    elif tier == "nojudge":
        lines.append("   处置：这批的\"没反应\"不可信 —— 先补一次 capture 再谈要不要加深")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pre", help="批前 capture（burn.py capture 的输出）")
    ap.add_argument("post", help="批末 capture")
    ap.add_argument("--watch", type=int, default=EARLY_ROW,
                    help=f"相位刻度行（默认 {EARLY_ROW}）")
    args = ap.parse_args(argv)
    text = report(args.pre, args.post, args.watch)
    print(text)
    # 非零退出让 crack.sh/人不可能把它当成"和往常一样"：early/partial/nojudge 都是要改动作的
    return 0 if "early 档判读：nothing" in text else 3


if __name__ == "__main__":
    sys.exit(main())
