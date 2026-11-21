#!/bin/bash
# FF 烧写的唯一入口。2026-11-21 那一遍是人肉走的（采集 → 交叉验证 → 试烧一行 → 独立重读 →
# 续烧其余 → 再独立重读），每步都靠人记得；这里把它变成脚本，因为**记得**这件事在窗口里
# 不值钱：窗口里最稀缺的就是注意力，而每一步的判据都比人可靠。
#
# 用法（判决机上，插着目标卡；PLAN 由 ffcalc gen 或人写，格式见 host/ga100vfi/burn.py）：
#   PLAN=plan.json bash bench/burn.sh                    # 现场采一份快照，先试烧计划的第一条
#   PLAN=plan.json SNAP=snapshot.json bash bench/burn.sh # 用已有快照（窗口里不再多读一遍）
#   PLAN=plan.json ALL=1 bash bench/burn.sh              # 不试烧，一次烧完整个计划
#   PLAN=plan.json ROWS=302,303 bash bench/burn.sh       # 只烧指定行（断点续烧）
#
# 三条不许让步的地方：
#   * 每一步真写之前都停一次手等人确认（熔丝不可逆；脚本不做"顺手把剩下的也烧了"）；
#   * `SNAP` 一旦生成就不再换：续烧时旧快照让计划"自相矛盾"是**正常现象**，
#     `crosscheck(resumed=…)` 认这个形状（见 host/ga100vfi/burn.py 的 ResumeTests）；
#   * 每一阶段之后都做一次独立重读 + `diffrows`：要的是"只有计划内的行变了"，
#     不是"我烧的那几行变了"。
set -uo pipefail
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
HOST=$GA_BASE/host
BDF=${BDF:-0000:01:00.0}
[ -n "${PLAN:-}" ] || { echo "[!!] 必须给 PLAN=…（烧写计划 json）"; exit 2; }
[ -f "$PLAN" ] || { echo "[!!] 计划文件不存在：$PLAN"; exit 2; }
cd "$HOST" || exit 1
TS=$(date -u +%Y%m%d-%H%M%S)
OUT=$GA_BASE/logs/$TS-burn-$(basename "$PLAN" .json)
mkdir -p "$OUT"
cp "$PLAN" "$OUT/"

# ---- 0. memory decode：BAR0 读回全 0xff 的第一处置是 wake，不是判卡已死 ----
# 冷启动后没有驱动 claim 这张卡时 Command=0x0000，每个 BAR0 读都是 0xffffffff，与掉卡长得
# 一模一样（2026-11-21：11 条记录烧成功之后差点被记成"卡死了"）。配置空间答不答才是分岔点，
# 所以这里只调 wake —— 它读配置空间，不答就退出，绝不往下走。
echo "== 0. 打开 memory decode（只写卡自己的 COMMAND 寄存器）=="
python3 -m ga100vfi.burn --bdf "$BDF" wake 2>&1 | tee "$OUT/wake.txt"
[ "${PIPESTATUS[0]}" -eq 0 ] || { echo "[!!] 配置空间不答 → 按 bench/README『卡不见了』分形状，别烧"; exit 3; }

echo "== 1. 窗口判据（纯读）=="
python3 -m ga100vfi.burn --bdf "$BDF" status 2>&1 | tee "$OUT/status-before.txt"
grep -q 'goldilocks window: OPEN' "$OUT/status-before.txt" || {
  echo "[!!] 不在窗口里。烧 FF 必须在命中之后、且没碰过 r / 驱动 / 热重启（docs/UNLOCK_AND_POISON §2.5）"; exit 3; }

if [ -z "${SNAP:-}" ]; then
  SNAP=$OUT/snapshot-before.json
  echo "== 2. 现场快照（512 行里 FF 区 222..486，逐行 CMD_READ）=="
  python3 -m ga100vfi.burn --bdf "$BDF" capture -o "$SNAP" 2>&1 | tee "$OUT/capture.txt"
else
  echo "== 2. 沿用已有快照：$SNAP（续烧时就该用烧前那一份）=="
fi
cp "$SNAP" "$OUT/" 2>/dev/null

echo "== 3. 离线交叉验证 + 目标行现场回读 =="
python3 -m ga100vfi.burn --bdf "$BDF" crosscheck "$SNAP" "$PLAN" 2>&1 | tee "$OUT/crosscheck.txt"
[ "${PIPESTATUS[0]}" -eq 0 ] || { echo "[!!] 交叉验证不过 → 一发不烧"; exit 1; }

confirm() {
  printf '%s [输入 YES 继续，其它任意键放弃] ' "$1"
  read -r ans
  [ "$ans" = "YES" ] || { echo "放弃。已经烧进去的不回撤。"; exit 0; }
}

FIRST=$(python3 -c 'import json,sys;print(min(int(r["row"]) for r in json.load(open(sys.argv[1]))["burn"]))' "$PLAN")
if [ -z "${ROWS:-}" ] && [ -z "${ALL:-}" ]; then
  echo "== 4. 试烧第一条：row $FIRST =="
  confirm "试烧 row $FIRST（不可逆）："
  python3 -m ga100vfi.burn --bdf "$BDF" burn "$SNAP" "$PLAN" --rows "$FIRST" --yes \
    2>&1 | tee "$OUT/burn-first.txt"
  rc=${PIPESTATUS[0]}
  [ $rc -eq 0 ] || { echo "[!!] 试烧失败 rc=$rc → 停下来看 $OUT/burn-first.txt，别烧其余"; exit $rc; }
  ROWS=$FIRST
fi

echo "== 5. 独立重读 + 逐行核对（$ROWS）=="
python3 -m ga100vfi.burn --bdf "$BDF" capture -o "$OUT/snapshot-after.json" >/dev/null
python3 -m ga100vfi.burn diffrows "$SNAP" "$OUT/snapshot-after.json" --plan "$PLAN" \
  2>&1 | tee "$OUT/diff.txt"
grep -q '结论：只有计划内的行按记录改变' "$OUT/diff.txt" || {
  echo "[!!] 差异与计划不一致 → 先查清，别再烧下一批"; exit 1; }

if [ -n "${ALL:-}" ] || [ "${ROWS:-}" = "$FIRST" ]; then
  REST=$(python3 -c '
import json,sys
p=json.load(open(sys.argv[1]))
rows=sorted(int(r["row"]) for r in p["burn"])
skip=set(int(x) for x in (sys.argv[2].split(",") if sys.argv[2] else []))
print(",".join(str(r) for r in rows if r not in skip))' "$PLAN" "${ROWS:-}")
  if [ -n "$REST" ]; then
    echo "== 6. 续烧其余：$REST =="
    echo "   （row $FIRST 已落地：burn_plan 会现场认出它并跳过，不重烧、也不要求快照里它是空白）"
    [ -n "${ALL:-}" ] || confirm "续烧其余 $(echo "$REST" | tr ',' '\n' | wc -l | tr -d ' ') 条（不可逆）："
    python3 -m ga100vfi.burn --bdf "$BDF" burn "$SNAP" "$PLAN" --rows "$REST" --yes \
      2>&1 | tee "$OUT/burn-rest.txt" || { echo "[!!] 续烧失败 → 现场停在半程，拔电前先 diff 一次"; exit 1; }
    python3 -m ga100vfi.burn --bdf "$BDF" capture -o "$OUT/snapshot-final.json" >/dev/null
    python3 -m ga100vfi.burn diffrows "$SNAP" "$OUT/snapshot-final.json" --plan "$PLAN" \
      2>&1 | tee "$OUT/diff-final.txt"
    grep -q '结论：只有计划内的行按记录改变' "$OUT/diff-final.txt" \
      || { echo "[!!] 收尾核对不一致 → 看 $OUT/diff-final.txt"; exit 1; }
  fi
fi

echo
echo "下一步：**拔电冷启动**（新熔丝只在 boot 时被 devinit 重算，热重启/PEX_RST 不算）。"
echo "起来之后跑：PLAN=$PLAN SNAP=$SNAP bash bench/postboot.sh"
echo "证据目录：$OUT"
