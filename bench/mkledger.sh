#!/bin/bash
# (Re)build the cross-run ledger from every campaign run's agent.log, so the scarce
# shots each power cycle produces accumulate into one (delay, vd) -> verdict table.
set -uo pipefail
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
LOGS=$GA_BASE/logs
LEDGER=$LOGS/ledger.txt
# 目录不存在就明说，别留一个 FileNotFoundError 让人以为是脚本坏了。
if [ ! -d "$LOGS" ]; then
  echo "没有日志目录 $LOGS：台账来自 crack.sh 的落盘，先跑过战役再汇总。"
  echo "（部署目录不是默认值时，用 GA_BASE=<部署目录> bash $0）"
  exit 1
fi
: > "$LEDGER.tmp"
# 汇总块的非零退出必须传出去：静默产出一份空台账，比报错退出危险得多（下一轮会照着
# 空表决定往哪个电压段补发）。
python3 - "$LOGS" "$LEDGER.tmp" <<'PY' || { echo "[!!] 台账生成失败，$LEDGER 保持原样"; rm -f "$LEDGER.tmp"; exit 1; }
import glob, os, re, sys
logs, out = sys.argv[1], sys.argv[2]
pat = re.compile(r"attempt (\d+) delay=(-?\d+) vd=(-?\d+).* -> (\w+)")
rows = []
# 目录名规则是 crack.sh 定的：<ts>-crack-<固件版本>-<pulses x pw>_<vd>。这里曾经是写死的
# "*-crack2f385b0"，固件版本一换就悄悄汇总不到任何东西 —— 台账空掉的危险性在于它看起来
# 像"还没有数据"。所以现在既放宽模式，也在零命中时明确报错。
runs = sorted(glob.glob(os.path.join(logs, "*-crack*", "agent.log")))
if not runs:
    print(f"  [!!] {logs} 下没有任何 *-crack*/agent.log。要么真的还没打过硬仗，要么 crack.sh 的"
          f"台账目录命名改了而这里没跟上 —— 后者必须修，不能让台账静默为空。")
    raise SystemExit(1)
for log in runs:
    run = os.path.basename(os.path.dirname(log))
    body = open(log, errors="replace").read()
    found = pat.findall(body)
    for a, d, v, r in found:
        rows.append((run, d, v, r))
    # A poisoned attempt never gets a verdict fed (red line 1), but the coordinates are
    # known from the last line, and "this parameter set can trip poison" is key data.
    if "POISON" in body and found:
        d, v = found[-1][1], found[-1][2]
        if not (rows and rows[-1][:3] == (run, d, v, "poison")):
            rows.append((run, d, v, "poison"))
    if "lost_streak" in body or "停手" in body:
        print(f"  [note] {run}: 出现过连续 lost（卡不在总线上），这些发数不算数据")
with open(out, "w") as f:
    for run, d, v, r in rows:
        f.write(f"{run}\t{d}\t{v}\t{r}\n")
print(f"  写入 {len(rows)} 发")
PY
mv "$LEDGER.tmp" "$LEDGER"
echo "== $LEDGER =="
cat "$LEDGER"
echo
echo "== 合并后的窗口表 =="
python3 - "$LEDGER" <<'PY'
import collections, sys
tab = collections.defaultdict(collections.Counter)
for line in open(sys.argv[1]):
    p = line.rstrip("\n").split("\t")
    if len(p) < 4:
        continue
    tab[(int(p[1]), int(p[2]))][p[3]] += 1
if not tab:
    print("  （还没有真实判决数据）"); raise SystemExit
print("   delay    vd   合计  under  over  poison  hit  lost   判读")
for (d, v) in sorted(tab):
    c = tab[(d, v)]
    n = sum(c.values())
    bad = c.get("over", 0) + c.get("poison", 0)
    note = ""
    if c.get("hit"):
        note = "<== 有命中迹象，下一轮在这里加密"
    elif bad and c.get("under"):
        note = "<- under 与 over 混现 = 边界就在这个 vd 附近"
    elif bad:
        note = "<- 全是 over/poison：太深，往浅调（vd 调高）"
    elif c.get("under"):
        note = "全是 under：还不够深"
    print(f"   {d:>5}  {v:>4}  {n:>4}  {c.get('under',0):>5}  {c.get('over',0):>5}"
          f"  {c.get('poison',0):>6}  {c.get('hit',0):>3}  {c.get('lost',0):>4}   {note}")
PY
