#!/bin/bash
# 命中窗口里：生成并交叉验证这张卡的 `--mig` 烧写计划。全只读（只发 CMD_READ），一发不烧。
#
# 为什么要有这个脚本：README §2.5 那四道闸门（ffcalc 采集 → 覆盖率 → 认卡 → sim/verify →
# 本仓库独立 crosscheck）以前是人肉按顺序敲的，而窗口里最稀缺的就是注意力与顺序。
# 两条独立采集路径（本仓库的 FuseMacro 与 ffcalc 自己的 ffmacro）在这里第一次对账：
# 它们对不上就说明快照不可信，计划再漂亮也不能烧。
#
# 用法（判决机上）：
#   DIR=/root/ga100vfi/logs/<...>-window bash bench/planmig.sh   # 不给 DIR 就取最新窗口目录
# 产出：$W/rows-a.txt（本仓库路径的行读；TWICE=1 时多读一遍并对账）、$W/card-dump.json（含指纹身份）、
#       $W/plan-mig.json（ffcalc gen --mig 的计划，行代价由它按这张卡现算）
set -uo pipefail
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
FFCALC=${FFCALC:-$HOME/ffcalc}
BDF=${BDF:-0000:01:00.0}
W=${DIR:-$(ls -dt "$GA_BASE"/logs/*-window 2>/dev/null | head -1)}
[ -n "${W:-}" ] || { echo "[!!] 没有窗口目录：先跑 bench/window.sh"; exit 2; }
[ -d "$FFCALC/ffcalc" ] || { echo "[!!] 找不到 ffcalc：$FFCALC"; exit 2; }
# 以这份 ffcalc 为准：判决机上还有第二份旧拷贝挂在 sys.path 上（/root/ffrun），从别的 cwd 调
# `python3 -m ffcalc` 会打到那份连 __main__ 都没有的包上（今天就是这么红的）。
export PYTHONPATH=$FFCALC${PYTHONPATH:+:$PYTHONPATH}
echo "== 窗口目录 $W ／ ffcalc $FFCALC ／ BDF $BDF =="

if [ -n "${SKIP_READ:-}" ]; then
  # 同一个窗口里重跑闸门时用：熔丝读不变化，两份产物已经在那儿了，没必要再花两分钟重读。
  echo "== 1./2. 沿用已有产物（SKIP_READ=1）=="
  ls -l --time-style=+%T "$W/rows-a.txt" "$W/card-dump.json" || { echo "[!!] 产物不全，别跳读"; exit 2; }
else
  echo "== 1. 本仓库路径读一遍 512 行（CMD_READ，只读；每行一次命令寄存器写，约 50 s）=="
  cd "$GA_BASE/host" || exit 1
  echo "   开读 $(date +%T) …"
  python3 -m ga100vfi.burn --bdf "$BDF" capture -o "$W/rows-a.txt"
  if [ -n "${TWICE:-}" ]; then
    echo "   再读一遍 $(date +%T) …（TWICE=1：同一个窗口里两遍逐字节一致才认快照，README §2.4）"
    python3 -m ga100vfi.burn --bdf "$BDF" capture -o "$W/rows-b.txt"
    diff "$W/rows-a.txt" "$W/rows-b.txt" && echo "   [ok] 两遍逐字节一致" || {
      echo "[!!] 同一个窗口里两遍读不一致 → 快照不可信，别烧"; exit 3; }
  fi

  echo "== 2. ffcalc 自己的采集器再采一份（61 个 option/defective/info 字 + 它那条 CMD_READ，约 50 s）=="
  cd "$FFCALC" || exit 1
  echo "   开读 $(date +%T) …"
  python3 -m ffcalc.capture --bdf "$BDF" --read-ff-rows -o "$W/card-dump.json"
fi

echo "== 3. 两条独立采集路径对账（可信区间 0..486；IFF 区 487..511 本台架读不准，见 burn.py 注释）=="
cd "$FFCALC" || exit 1
python3 - "$W" "$FFCALC" <<'PY' || exit 3
import json, sys
w, ff = sys.argv[1], sys.argv[2]
sys.path.insert(0, ff)
from ffcalc import dump as snap        # 行值的唯一解码器（磁盘上是 "0x%08x" 字符串）
live = snap.normalize_rows(json.load(open(w + "/card-dump.json"))["ff_rows"], "ffcalc dump")
ours = {}
for line in open(w + "/rows-a.txt"):
    line = line.split("#", 1)[0].strip()
    if line:
        row, val = line.split()
        ours[int(row)] = snap.int_value(val, "rows-a")
bad = [(r, ours[r], live.get(r)) for r in sorted(ours) if r <= 486 and live.get(r) != ours[r]]
print("   ffcalc 读到 %d 行；两条路径在 0..486 差异 %d 行 %s"
      % (len(live), len(bad), bad[:6] or ""))
sys.exit(1 if bad else 0)
PY

echo "== 4. 用本仓库那遍行读覆盖（现场读优先）并核对 512 行覆盖率 =="
python3 -m ffcalc mergeff "$W/card-dump.json" "$W/rows-a.txt"

echo "== 5. 认卡（判决机没有 nvidia-smi ⇒ 用 defective+SBFE 指纹；哈希算法取 ffcalc 本体）=="
python3 "$GA_BASE/tools/attach_identity.py" "$W/card-dump.json" --bdf "$BDF" --ffcalc "$FFCALC"

echo "== 6. gen --mig：行代价由 ffcalc 按这张卡的现场算，不抄档案 =="
python3 -m ffcalc gen "$W/card-dump.json" -o "$W/plan-mig.json" --mig

echo "== 7. sim：离线重放这份计划 =="
python3 -m ffcalc sim "$W/card-dump.json" "$W/plan-mig.json" | tail -6

echo "== 8. verify：计划必须认这张卡 =="
python3 -m ffcalc verify "$W/plan-mig.json" --dump "$W/card-dump.json"

echo "== 9. 第四道：本仓库独立实现的 crosscheck + 目标行现场回读 =="
cd "$GA_BASE/host" && python3 -m ga100vfi.burn --bdf "$BDF" crosscheck "$W/card-dump.json" "$W/plan-mig.json"

echo "== 10. 上一轮的连续性：这张卡烧过一次，旧计划的行必须原样在场 =="
python3 - "$W" "$FFCALC" "$GA_BASE/tools" <<'PY'
import json, os, sys
w, ff, tools = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, ff); sys.path.insert(0, tools)
from ffcalc import dump as snap           # 行值在磁盘上是 "0x%08x" 字符串，唯一解码器在这里
from ffcalc import ingest
import attach_identity as ai
mmap = ingest.load_map()
d = json.load(open(w + "/card-dump.json"))
rows = snap.normalize_rows(d["ff_rows"], "card dump")
plan = json.load(open(w + "/plan-mig.json"))
used = sorted(r for r, v in rows.items() if v and 222 <= r <= 486)
print("   FF 区（222..486）已用 %d 行，最高 %d ⇒ 可用行从 %d 起"
      % (len(used), max(used), max(used) + 1))
# 认卡：指纹 = defective 全字 + SBFE。中途烧过一次会把 SBFE 从 0 翻到 1，整串指纹就变了
# （上一张卡实测 58bf9ac8→fc497d01）。所以这里同时算【去掉 SBFE 的那半】：它才对应 die 本身。
live = ai.build_entries(d, mmap)
fp_live = ingest.fingerprint(live)
fp_die = ingest.fingerprint([e for e in live if not e[0].startswith("info.")])
print("   本次快照指纹 %s；去掉 SBFE 的 die 指纹 %s" % (fp_live, fp_die))
for name in sorted(os.listdir(os.path.join(ff, "cards"))):
    a = json.load(open(os.path.join(ff, "cards", name)))
    try:
        e = ai.build_entries(a, mmap)
    except SystemExit:
        continue                            # 档案快照缺指纹输入（旧版采集），不参与对账
    die = ingest.fingerprint([x for x in e if not x[0].startswith("info.")])
    print("   档案 %-46s 指纹 %-17s die %-17s %s"
          % (name, a.get("fingerprint"), die, "⇐ 同一块 die" if die == fp_die else ""))
for name in sorted(os.listdir(os.path.join(ff, "plans"))):
    p = json.load(open(os.path.join(ff, "plans", name)))
    miss = [int(r["row"]) for r in p["burn"]
            if rows.get(int(r["row"]), 0) != int(r["record"], 16)]
    print("   档案计划 %-42s %2d 条（行 %s）未落地 %s"
          % (name, len(p["burn"]), [r["row"] for r in p["burn"]], miss or "无"))
print("   本计划要写的行：%s" % sorted(int(r["row"]) for r in plan["burn"]))
for r in sorted(int(x["row"]) for x in plan["burn"]):
    print("      row %-4d 快照读回 0x%08x %s" % (r, rows.get(r, 0xFFFFFFFF),
          "空白" if rows.get(r) == 0 else "非空白 ⇒ crosscheck 应已报错"))
PY

echo
echo "计划 $W/plan-mig.json ／ 快照 $W/card-dump.json"
echo "烧（操作员把授权到的行写进 YES，脚本只认这个范围内的行）："
echo "  PLAN=$W/plan-mig.json SNAP=$W/card-dump.json YES=312 bash bench/burn.sh"
