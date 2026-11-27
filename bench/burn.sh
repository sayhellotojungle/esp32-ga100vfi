#!/bin/bash
# FF 烧写的唯一入口。2026-11-21 那一遍是人肉走的（采集 → 交叉验证 → 试烧一行 → 独立重读 →
# 续烧其余 → 再独立重读），每步都靠人记得；这里把它变成脚本，因为**记得**这件事在窗口里
# 不值钱：窗口里最稀缺的就是注意力，而每一步的判据都比人可靠。
#
# 用法（判决机上，插着目标卡；PLAN 由 bench/planmig.sh 或 ffcalc gen 生成，格式见 host/ga100vfi/burn.py）：
#   PLAN=plan.json bash bench/burn.sh                    # 现场采一份快照，先试烧计划的第一条
#   PLAN=plan.json SNAP=snapshot.json bash bench/burn.sh # 用已有快照（窗口里不再多读一遍）
#   PLAN=plan.json ALL=1 bash bench/burn.sh              # 不试烧，一次烧完整个计划
#   PLAN=plan.json ROWS=302,303 bash bench/burn.sh       # 断点续烧：ROWS = 已经落地的行
#   PLAN=plan.json YES=302,303 bash bench/burn.sh        # 操作员预先授权到哪几行（无终端时用；
#                                                        #  授权范围之外的行照旧停下来问）
#
# 三条不许让步的地方：
#   * 每一步真写之前都停一次手等人确认（熔丝不可逆；脚本不做顺手把剩下的也烧了）；
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
  echo "[!!] 不在窗口里。烧 FF 必须在命中之后、且没碰过 r / 驱动 / 热重启（docs/UNLOCK_AND_POISON.md §五）"; exit 3; }

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

# 确认。两种走法：人在终端前 → 打 YES；人已经把授权写到调用里（YES=312,313）→ 只放行这个范围内的
# 行。为什么要认 YES 环境变量：没有终端的会话（SSH 一条命令）里 read 会一直挂着，而操作员授权到哪几行
# 本来就是这次调用应该带进来的信息 —— 授权范围之外的行照旧停下来问，不顺手多烧。
confirm() {
  local prompt=$1 rows=${2:-} r ok=1
  if [ -n "${YES:-}" ] && [ -n "$rows" ]; then
    local IFS=,
    for r in $rows; do
      case ",${YES}," in *",$r,"*) ;; *) ok=0 ;; esac
    done
    if [ "$ok" = 1 ]; then
      echo "$prompt 操作员预先授权（YES=$YES）→ 继续"
      return 0
    fi
    echo "$prompt 授权范围 YES=$YES 不含 $rows → 放弃（已烧进去的不回撤）"
    exit 0
  fi
  printf '%s [输入 YES 继续，其它任意键放弃] ' "$prompt"
  read -r ans
  [ "$ans" = "YES" ] || { echo "放弃。已经烧进去的不回撤。"; exit 0; }
}

FIRST=$(python3 -c 'import json,sys;print(min(int(r["row"]) for r in json.load(open(sys.argv[1]))["burn"]))' "$PLAN")
DONE=${ROWS:-}            # 到目前为止真正落地的行；续烧时调用者用 ROWS= 把它告诉脚本

# 独立重读 + 逐行核对，判据是【到目前为止烧掉的那几行，且只有它们】。
# 为什么要按行裁一份计划再对：diffrows 拿整份计划时，"还没轮到烧的行"会被算成 missing ⇒ 判成
# 差异不一致，于是任何多行计划在试烧第一条之后就再也走不到续烧（11-21 那一遍是人肉做的，没暴露）。
snapdiff() {              # $1 = 快照输出文件；$2 = 已落地的行（逗号分隔）；$3 = 标签
  local out=$1 done_rows=$2 tag=$3
  python3 -m ga100vfi.burn --bdf "$BDF" capture -o "$out" >/dev/null
  python3 - "$PLAN" "$done_rows" "$OUT/plan-done-$tag.json" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1]))
want = set(int(x) for x in sys.argv[2].split(",") if x.strip())
plan["burn"] = [r for r in plan["burn"] if int(r["row"]) in want]
json.dump(plan, open(sys.argv[3], "w"), indent=1)
PY
  python3 -m ga100vfi.burn diffrows "$SNAP" "$out" --plan "$OUT/plan-done-$tag.json" \
    2>&1 | tee "$OUT/diff-$tag.txt"
  grep -q '结论：只有计划内的行按记录改变' "$OUT/diff-$tag.txt"
}

if [ -z "${ROWS:-}" ] && [ -z "${ALL:-}" ]; then
  echo "== 4. 试烧第一条：row $FIRST =="
  confirm "试烧 row $FIRST（不可逆）：" "$FIRST"
  python3 -m ga100vfi.burn --bdf "$BDF" burn "$SNAP" "$PLAN" --rows "$FIRST" --yes \
    2>&1 | tee "$OUT/burn-first.txt"
  rc=${PIPESTATUS[0]}
  [ $rc -eq 0 ] || { echo "[!!] 试烧失败 rc=$rc → 停下来看 $OUT/burn-first.txt，别烧其余"; exit $rc; }
  DONE=$FIRST
  TRIED=1

  echo "== 5. 独立重读 + 逐行核对（已落地：$DONE）=="
  snapdiff "$OUT/snapshot-after.json" "$DONE" first || {
    echo "[!!] 差异与计划不一致 → 先查清，别再烧下一批"; exit 1; }
fi

if [ -n "${ALL:-}" ] || [ -n "${ROWS:-}" ] || [ -n "${TRIED:-}" ]; then
  REST=$(python3 -c '
import json,sys
p=json.load(open(sys.argv[1]))
rows=sorted(int(r["row"]) for r in p["burn"])
skip=set(int(x) for x in (sys.argv[2].split(",") if sys.argv[2] else []))
print(",".join(str(r) for r in rows if r not in skip))' "$PLAN" "$DONE")
  if [ -n "$REST" ]; then
    echo "== 6. 续烧其余：$REST =="
    echo "   （$DONE 已落地：burn_plan 会现场认出它并跳过，不重烧、也不要求快照里它是空白）"
    [ -n "${ALL:-}" ] || confirm "续烧其余 $(echo "$REST" | tr ',' '\n' | wc -l | tr -d ' ') 条（不可逆）：" "$REST"
    # 续烧的 crosscheck 基线必须是【已落地之后】的现场：crosscheck 要求"声称续烧的行"在快照里就读作
    # 本条记录，而烧前那份快照里它是空白 —— 拿旧快照续烧会被它自己拒掉（2026-11-26 实测：
    # row 312 已落地，`FAIL: row 312 listed as resumed but the snapshot reads 0x00000000`）。
    # $SNAP 不换：它是 diff 的起点，"只有计划内的行变了"要对着烧前那一版算。
    python3 -m ga100vfi.burn --bdf "$BDF" capture -o "$OUT/snapshot-mid.json" >/dev/null
    python3 -m ga100vfi.burn --bdf "$BDF" burn "$OUT/snapshot-mid.json" "$PLAN" --rows "$REST" --yes \
      2>&1 | tee "$OUT/burn-rest.txt" || { echo "[!!] 续烧失败 → 现场停在半程，拔电前先 diff 一次"; exit 1; }
    snapdiff "$OUT/snapshot-final.json" "$DONE,$REST" final || {
      echo "[!!] 收尾核对不一致 → 看 $OUT/diff-final.txt"; exit 1; }
  fi
fi

echo
echo "下一步：**拔电冷启动**（新熔丝只在 boot 时被 devinit 重算，热重启/PEX_RST 不算）。"
echo "起来之后跑：PLAN=$PLAN SNAP=$SNAP bash bench/postboot.sh"
echo "证据目录：$OUT"
