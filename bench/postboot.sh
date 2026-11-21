#!/bin/bash
# 冷启动之后的正式验证。为什么要脚本：2026-11-21 那一遍是人肉做的，而且**当场判错过一次** ——
# BAR0 整片读回 0xffffffff，看起来和"卡死了"完全一样，真因是冷启动后没有驱动 claim 这张卡、
# Command 寄存器的 Memory Space Enable 一直是 0。判据写成脚本之后，这种形状先过 `wake`，
# 再谈生死。
#
# 用法（判决机上）：
#   PLAN=plan.json SNAP=snapshot-before.json bash bench/postboot.sh
#   两个都可省：没有 PLAN 就只报现场状态，没有 SNAP 就跳过"熔丝保住了没"那条核对。
#
# 三条判据，全部只取这张卡自己的读数：
#   A 熔丝链 boot 后仍然有效：CRC=0x55、FUSEDEBUG=0、FUSECTRL 在 IDLE、没有 0xBADF；
#   B 重读 FF 区与烧前快照逐行比，差异必须**恰好**是计划内的那些行（熔丝保住了）；
#   C 影子寄存器（devinit 在 boot 时按熔丝重算）与计划覆盖面一致 —— 这是"熔丝真生效"的
#     唯一无驱动证据；`CMD_SENSE` 不刷新它们（§6.3 第 1 条），所以只能靠 boot。
set -uo pipefail
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
HOST=$GA_BASE/host
BDF=${BDF:-0000:01:00.0}
TS=$(date -u +%Y%m%d-%H%M%S)
OUT=$GA_BASE/logs/$TS-postboot
mkdir -p "$OUT"
cd "$HOST" || exit 1

echo "== 0. 配置空间答不答（不答就到此为止，不碰 BAR0）=="
python3 -m ga100vfi.burn --bdf "$BDF" wake 2>&1 | tee "$OUT/wake.txt"
case "${PIPESTATUS[0]}" in
  0) ;;
  3) echo "[!!] 配置空间不答：这是掉卡/没电，不是 decode 没开 → bench/card_probe.sh 分形状，然后断电"; exit 3 ;;
  *) echo "[!!] wake 之后 BAR0 仍读回全 1：往掉卡那侧查（桥的 LnkSta / 插槽供电）"; exit 3 ;;
esac

echo "== A. 控制器状态（boot 后窗口应当已经关闭，这是正常结局，不是事故）=="
python3 -m ga100vfi.burn --bdf "$BDF" status 2>&1 | tee "$OUT/status.txt"
python3 - "$OUT/status.txt" <<'PY'
import re, sys
t = open(sys.argv[1], encoding="utf-8").read()


def g(k):
    m = re.search(r"^%s\s+0x([0-9a-f]+)$" % k, t, re.M)
    return None if m is None else int(m.group(1), 16)


crc, dbg, ctrl, dsov = (g(k) for k in ("crc", "fusedebug", "fusectrl", "dsov"))
fmt = lambda v: "?" if v is None else "0x%08x" % v
print("   CRC=%s（必须 0x55）  FUSEDEBUG=%s（必须 0）" % (fmt(crc), fmt(dbg)))
print("   FUSECTRL=%s  DSOV=%s" % (fmt(ctrl), fmt(dsov)))
bad = []
if crc != 0x55:
    bad.append("CRC 不是 0x55：熔丝链 boot 后校验不过，先别再烧，按 §2.3 恢复顺序处理")
if dbg != 0:
    bad.append("FUSEDEBUG 非 0：熔丝块自己报了错")
if ctrl is not None and (ctrl & 0xFFFF0000) == 0xBADF0000:
    bad.append("FUSECTRL 是 poison：冷启动没清掉它 → 用板子按住 PEX_RST 再试")
if dsov == 0:
    print("   [note] DSOV 仍为 0：窗口还没关（没走过 PEX_RST/驱动），可以就地补烧")
print("   " + ("A 通过" if not bad else "[!!] A 不通过：" + "；".join(bad)))
raise SystemExit(1 if bad else 0)
PY
rcA=$?

if [ -n "${SNAP:-}" ] && [ -f "$SNAP" ]; then
  echo "== B. 重读 FF 区，与烧前快照逐行比（熔丝保住了没）=="
  python3 -m ga100vfi.burn --bdf "$BDF" capture -o "$OUT/snapshot-boot.json" >/dev/null
  cp "$SNAP" "$OUT/"
  python3 -m ga100vfi.burn diffrows "$SNAP" "$OUT/snapshot-boot.json" ${PLAN:+--plan "$PLAN"} \
    2>&1 | tee "$OUT/diff.txt"
  [ -n "${PLAN:-}" ] && grep -q '结论：只有计划内的行按记录改变' "$OUT/diff.txt" \
    && echo "   B 通过：冷启动后逐行仍然是「只烧了计划内的那些行」"
fi

echo "== C. 影子寄存器（devinit 按熔丝重算的结果；本次只读，不做 DSOV 回写探针）=="
GA_BDF="$BDF" python3 - "$OUT/shadow.json" ${SHADOW_BEFORE:+diff:$SHADOW_BEFORE} <<'PY' | tee "$OUT/shadow.txt"
import json, os, sys
from ga100vfi.bar0 import Bar0
from ga100vfi.classify import SHADOW
out = sys.argv[1]
base = None
for a in sys.argv[2:]:
    if a.startswith("diff:"):
        base = json.load(open(a[5:], encoding="utf-8"))
bar = Bar0(os.environ.get("GA_BDF"))      # 必须显式给 BDF：bar0 的默认值是另一块槽位
now = {name: bar.rd32(off) for name, off in SHADOW.items()}
bar.close()
json.dump(now, open(out, "w"), indent=1)
for name in sorted(now):
    b = (base or {}).get(name)
    tag = "" if b is None else ("" if b == now[name] else f"   boot 前={b:#010x} -> 现在={now[name]:#010x}  [变了]")
    print(f"   {name:<28}= {now[name]:#010x}{tag}")
if base:
    same = sum(1 for k in now if base.get(k) == now[k])
    print(f"   与烧前那份影子共 {len(now)} 项：{len(now) - same} 项变化，{same} 项不变")
    print("   变化项应当**只**落在计划涉及的字段上；没涉及的字段（fbp/fbpa/rop_l2 等）"
          "必须逐位不变 —— 它们变了说明 devinit 看到的比计划多。")
else:
    print("   （没给 SHADOW_BEFORE=烧前那份 shadow.json，只能看现场值，看不到覆盖面）")
print("   [已知未闭合] 影子口径的 SM 数与 ffcalc analysis 的 sm_count 相差 4（缺陷位口径不同），"
      "引用 SM 数时必须写清是哪一个。")
PY
echo
echo "证据目录：$OUT   A 的退出码：$rcA"
echo "提示：boot 后窗口按预期关闭；要再打毛刺重新开一役（bench/crack.sh），不是在已烧完的卡上重试。"
exit $rcA
