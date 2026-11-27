#!/usr/bin/env python3
"""「卡活到第几秒」——把 presence csv 换算成相对时刻，并同时相对**我们的读**摆一次。

为什么单独有个模块而不是脚本里的一段：2026-11-26 那条决定性结论（把第一次开 BAR0 从 +4 s 推到
+20 s，卡的死亡就从 +10~13 s 推到 +25~30 s）是临时写在 temp/r_ctrl.sh 里算的。它不该是一次性的：
每一批都需要知道两件事 —— 最后一发之后卡活了多久、以及**最后一次我们碰它之后**过了多久才死。
第二条差值才是"我们的读是不是凶手"的量；只看第一条会一直把它误读成"那一发坑杀了它"。

输入都是既有产物：
  presence csv（bench/judge-entry/run.sh 挂的记录器）：`HH:MM:SS.mmm,de 10 c2 20 ` 或不可达的样子
  agent.log：判决行里的 `t_bar0=NN.Ns`（从代理看到 awaiting_verdict 到第一次开 BAR0 的秒数）
  campaign.csv：`HH:MM:SS,phase,tries,...`，用最后一条 executing 作为"最后一发"的原点
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import re
import sys

GOOD = "de 10 c2 20"
PROBES = (2, 5, 8, 10, 12, 15, 20, 25, 30, 45, 60, 90, 120)


def _t(s: str):
    s = s.strip()
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def read_presence(path: str):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, errors="replace") as f:
        for ln in f:
            parts = ln.strip().split(",", 1)
            if len(parts) != 2:
                continue
            t = _t(parts[0])
            if t:
                rows.append((t, parts[1].strip()))
    return rows


def last_executing(run_dir: str):
    """批内最后一发真正执行的时刻（campaign.csv 的 executing 行）。

    不用 DictReader：这个 csv 有没有表头历史上变过，而"哪一行算执行中"不该取决于键名。
    """
    p = os.path.join(run_dir, "campaign.csv")
    last = None
    if not os.path.exists(p):
        return None
    with open(p, errors="replace") as f:
        for row in csv_rows(f):
            if len(row) > 1 and row[1] == "executing":
                t = _t(row[0])
                if t:
                    last = t
    return last


def csv_rows(handle):
    import csv
    for row in csv.reader(handle):
        yield row


def last_read(run_dir: str):
    """最后一发的 (执行时刻, t_bar0) —— 我们需要"我们最后一次碰它"是在什么时候。

    时刻取自 agent.log 那行的打印时间（代理打印判决行的瞬间 ≈ 读完的时刻），t_bar0 是它自己报的
    相对秒数。两个都给，别让人去猜。
    """
    p = os.path.join(run_dir, "agent.log")
    shot_t, t_bar0, verdicts = None, None, 0
    if not os.path.exists(p):
        return shot_t, t_bar0, verdicts
    with open(p, errors="replace") as f:
        for ln in f:
            m = re.search(r"attempt (\d+) delay=(-?\d+) vd=(-?\d+)", ln)
            if not m:
                continue
            verdicts += 1
            tb = re.search(r"t_bar0=([0-9.]+)s", ln)
            t_cfg = re.search(r"t_cfg=([0-9.]+)s", ln)
            # 代理没有打印自己的时钟，所以"读的时刻"= 那一发的 executing 时刻 + t_bar0。
            # 这里只留最后一发的 t_bar0；时刻由调用方与 campaign.csv 相加得到。
            t_bar0 = float(tb.group(1)) if tb else (float(t_cfg.group(1)) if t_cfg else None)
    return shot_t, t_bar0, verdicts


def table(rows, origin, label, out=print):
    out("   相对%s的存活表（presence 只读 config）：" % label)
    for k in PROBES:
        want = origin + dt.timedelta(seconds=k)
        near = min(rows, key=lambda r: abs((r[0] - want).total_seconds()))
        d = abs((near[0] - want).total_seconds())
        if d > 1.5:
            out("      +%d s 那之后没有样本了（记录器停了或机器重启过）" % k)
            continue
        out("      +%d s  %-6s (%s)" % (k, "在场" if near[1] == GOOD else "不在",
                                       near[0].strftime("%H:%M:%S")))
    alive = [r for r in rows if r[0] >= origin + dt.timedelta(seconds=30) and r[1] == GOOD]
    gone = [r for r in rows if r[0] >= origin and r[1] != GOOD]
    first_gone = min((r[0] for r in gone), default=None)
    gap = (first_gone - origin).total_seconds() if first_gone else None
    out("      活过 +30s：%s；第一次不可达：+%s"
        % ("是（%d 个样本）" % len(alive) if alive else "否",
           ("%.1fs" % gap) if gap is not None else "整段没看到掉"))
    return bool(alive), gap


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", help="运行目录；不给就取 logs/*-crack-* 里最新的那个")
    ap.add_argument("--csv", default="/root/mig-card2/presence7.csv")
    ap.add_argument("--logs", default="logs", help="运行目录的父目录（判决机上通常不带）")
    a = ap.parse_args(argv)
    run = a.run
    if not run:
        cand = sorted(glob.glob(os.path.join(a.logs, "*-crack-*")), key=lambda p: os.path.getmtime(p))
        run = cand[-1] if cand else None
    rows = read_presence(a.csv)
    print("== 卡活到第几秒（run=%s csv=%s）==" % (run, a.csv))
    if not rows:
        print("   presence 一个样本都没有：判据不成立（记录器没挂上，或判决机中途重启过）")
        return 3
    origin = (last_executing(run) if run else None) or rows[0][0]
    if origin is rows[0][0]:
        print("   没找到 executing 行：原点退化成最早样本，下面的相对秒数要打折看")
    alive_after_shot, gap_shot = table(rows, origin, "最后一发")
    _, t_bar0, nver = last_read(run or "")
    if t_bar0 is not None and nver:
        read_at = origin + dt.timedelta(seconds=t_bar0)
        print("   我们最后一次开 BAR0：发后 %.1f s（%s），判决 %d 发%s"
              % (t_bar0, read_at.strftime("%H:%M:%S"), nver,
                 "" if nver == 1 else "  ← 多发批：t_bar0 取最后一条判决、原点取最后一条 executing，"
                                      "不是同一发时这一行只当量级看"))
        if gap_shot is not None:
            print("      死亡 - 我们的读 = %.1f s   ← 这一列才是【读是不是凶手】的量" % (gap_shot - t_bar0))
            print("      死亡 - 那一发坑   = %.1f s   ← 只看这一列会一直误判成坑杀的" % gap_shot)
    else:
        print("   这一批一发 BAR0 都没开（config-only 判决）：死亡时刻只能相对那一发坑来摆")
    print("   判据：活过 +30s = %s" % ("**卡活下来**（这才是要抓窗口的形状）" if alive_after_shot
                                       else "没活过 +30s"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
