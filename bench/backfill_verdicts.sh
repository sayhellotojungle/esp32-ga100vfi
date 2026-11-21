#!/bin/bash
# 把"运行目录里有、verdicts.tsv 里没有"的发数补进真值。
#
# 为什么需要它：`verdicts.tsv` 是包络的真值（`load_shots` 的规矩是"台账优先、运行目录只补差额"），
# 而 0.3.x 回滚后的判决代理只写 agent.log，不再写这个文件 —— 2026-11-21 实测它从 05:25 起 5.5 小时
# 一行没长，里面还有一行 263 个 NUL 字节（一次硬断电打断过追加）。真值停更的后果不是"少几行记录"：
# 门禁按 pw 分档数"浅档攒了几发"，攒够才允许加深，而 pw 只在台账行里 —— 化石台账会让加深判据悄悄
# 变成"永远没攒够"，或者反过来被运行目录副本喂出假数字。
#
# 用法（**显式列运行目录**）：
#   bench/backfill_verdicts.sh $HOME/ga100vfi/logs/20261121-070713-crack-0.3.2-2x222_374 …
# 为什么不让它自己扫全部目录：crack.sh 收尾时会自己追加本批的行（同一份数据的另一个写者），
# 全扫会把新批次重数一遍。要重放哪几个目录是人的判断，写下来才可追溯。
#
# 幂等：按 (vd, pw, delay, 脉冲数, 判决) **计数**比对，已有几行就只补缺的差额，所以同一批可以
# 反复重放。时间戳用回填当时的时间 —— 台账靠顺序判"最近一发是不是刚把卡打死"，补写的行本来就该
# 排在后面。
set -uo pipefail
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
VERDICTS=${VERDICTS:-$GA_BASE/../logs/verdicts.tsv}
[ $# -ge 1 ] || { echo "用法：$0 <运行目录> …（见文件头注释）"; exit 2; }
[ -f "$VERDICTS" ] || : > "$VERDICTS"
VERDICTS="$VERDICTS" python3 - "$@" <<'PY'
import collections, os, re, sys, time

verdicts = os.environ["VERDICTS"]
# 目录名自描述：<时间>-crack-<固件>-<脉冲数>x<脉宽>_<起点 vd>[_d<delay>]
RUN_DIR = re.compile(r"crack-[^/]*?(?P<pulses>\d+)x(?P<pw>\d+)_(?P<vd0>\d+)")
LINE = re.compile(r"attempt (?P<attempt>\d+) delay=(?P<delay>-?\d+) vd=(?P<vd>-?\d+)"
                  r"(?P<regs>.*?) -> (?P<verdict>[a-z_]+)(?P<tail>.*)")
# 台账行：`05:12:44\tvd=354 pw=198 d=117 p=3 -> under slip=48/5 [DSOV=… CRC=…]`
ROW = re.compile(r"^\s*(?P<t>\d{2}:\d{2}:\d{2})\s+vd=(?P<vd>-?\d+)\s+pw=(?P<pw>-?\d+)\s+"
                 r"d=(?P<delay>-?\d+)\s+p=(?P<pulses>\d+)\s*->\s*(?P<verdict>[a-z_]+)")

have = collections.Counter()
dirty = 0
for line in open(verdicts, errors="replace"):
    m = ROW.match(line)
    if m:
        g = m.groupdict()
        have[(int(g["vd"]), int(g["pw"]), int(g["delay"]), int(g["pulses"]), g["verdict"])] += 1
    elif line.strip("\x00 \t\r\n"):       # 看得见但读不懂的行：报出来，不静默吞
        dirty += 1
print(f"真值现有 {sum(have.values())} 发"
      + (f"，另有 {dirty} 行读不懂（NUL 或旧格式，跳过）" if dirty else ""))

added = 0
for d in sys.argv[1:]:
    m = RUN_DIR.search(os.path.basename(os.path.normpath(d)))
    if not m:
        print(f"   [!!] 跳过 {d}：目录名里没有脉冲数×脉宽，无法知道 pw")
        continue
    pulses, pw = int(m.group("pulses")), int(m.group("pw"))
    want = collections.Counter()
    fields = {}
    for name in ("agent.log", "ladder.log"):
        try:
            lines = open(os.path.join(d, name), errors="replace").readlines()
        except OSError:
            continue
        for line in lines:
            mm = LINE.search(line)
            if not mm:
                continue
            g = mm.groupdict()
            key = (int(g["vd"]), pw, int(g["delay"]), pulses, g["verdict"])
            want[key] += 1
            fields.setdefault(key, (" ".join(x.strip() for x in (g["tail"], g["regs"]) if x.strip())))
        break                                   # agent.log 在就不再读 ladder.log，避免同发两份
    if not want:
        print(f"   {os.path.basename(d)}：判决 0 行（这一批本来就没产生判决，不补）")
        continue
    deficit = want - have
    if not deficit:
        print(f"   {os.path.basename(d)}：{sum(want.values())} 发已在真值里，不补")
        continue
    with open(verdicts, "a") as f:
        for key, n in sorted(deficit.items()):
            for _ in range(n):
                f.write(f"{time.strftime('%H:%M:%S')}\tvd={key[0]} pw={key[1]} d={key[2]} "
                        f"p={key[3]} -> {key[4]}"
                        + (f" {fields[key]}" if fields.get(key) else "") + "\n")
                have[key] += 1
                added += 1
            print(f"   +{n} 发 vd={key[0]} pw={key[1]} d={key[2]} p={key[3]} -> {key[4]}")
print(f"== 回填 {added} 发；真值现在 {sum(have.values())} 发 ==")
PY
