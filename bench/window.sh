#!/bin/bash
# 命中窗口：抓证据。全部只读（README 端到端流程 §2 + §5 红线 1）。
# 什么都不烧：烧写是另一个必须显式确认的动作。
#
# 红线 1（事故 logs/02）：控制器 poison（FUSECTRL 读到 0xBADFxxxx）之后继续读 BAR0 会
# 挂死 PCIe 上游、整机复位。所以这里第一件事是「只读一个寄存器问一次」，问出 poison
# 就立刻停手，绝不 classify/capture 往上冲。
set -uo pipefail
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
# export 不是顺手：下面的取证块是 python heredoc，只能从环境里取 BDF，否则你传的
# BDF=… 会被忽略而悄悄打到默认槽位上。
export BDF=${BDF:-0000:01:00.0}
DIR=${DIR:-$GA_BASE/logs/$(date +%Y%m%d-%H%M%S)-window}
HOST=$GA_BASE/host
mkdir -p "$DIR"
cd "$HOST" || exit 1

echo "== 0. 先问一次 FUSECTRL（只读一个寄存器）=="
python3 - <<'PY' || { echo "[!!] 停在这里：不要读 BAR0，先重启判决机（POST 拉 PERST#），仍不可达再断电 ≥30 s"; exit 4; }
import os, sys
from ga100vfi import bar0
from ga100vfi.classify import STATE_IDLE, STATE_POISONED, STATE_UNKNOWN, fusectrl_state
bdf = os.environ.get("BDF", "0000:01:00.0")
if not bar0.restore(bdf, wait_s=1.0, settle_s=0.15):
    print("   [!!] 配置空间不可达：卡不在 PCIe 上"); sys.exit(1)
with bar0.Bar0(bdf) as bar:
    ctrl = bar.rd32(0x00820000)
state = fusectrl_state(ctrl)
if state == STATE_POISONED:
    print(f"   [!!] FUSECTRL={ctrl:#010x} = poison。红线 1：不再读 BAR0。"); sys.exit(1)
if state == STATE_UNKNOWN:
    # 2026-11-20 实测的第三种态：0x00000000（vd=309）。同一电压的下一发就 poison 并掉链路。
    print(f"   [!!] FUSECTRL={ctrl:#010x} 既非 IDLE 也非 poison —— 不判决、不取证。"); sys.exit(1)
if state != STATE_IDLE:
    print(f"   [!!] FUSECTRL={ctrl:#010x} = {state}（卡不在总线上）"); sys.exit(1)
print(f"   [ok] FUSECTRL={ctrl:#010x}（IDLE），可以继续只读检查")
PY

echo "== 1. 命中证据（BAR0 只读；classify 内部也有同样的 poison 防护）=="
python3 -m ga100vfi classify --bdf "$BDF" 2>&1 | tee "$DIR/classify.txt"
python3 -m ga100vfi baseline --bdf "$BDF" 2>&1 | tee "$DIR/baseline.txt"

echo "== 2. 熔丝控制器状态（只读）=="
python3 -m ga100vfi.burn --bdf "$BDF" status 2>&1 | tee "$DIR/fuse-status.txt"
if grep -qiE '0xbadf|poison' "$DIR/fuse-status.txt"; then
  echo "[!!] status 里看到 poison：到此为止，不再读 BAR0，不做任何烧写动作。"
  exit 4
fi

echo "== 3. 抓 512 行 FF（CMD_READ，只读）=="
python3 -m ga100vfi.burn --bdf "$BDF" capture -o "$DIR/snapshot.json" 2>&1 | tee "$DIR/capture.log"
[ -s "$DIR/snapshot.json" ] || { echo "[!!] 快照为空，窗口可能已关"; exit 5; }

echo "== 4. 抓完再判一次命中是否仍然成立（窗口会被任何 PEX_RST / 驱动加载关掉）=="
python3 -m ga100vfi classify --bdf "$BDF" 2>&1 | tail -6
python3 - <<'PY'
import json
d = json.load(open("/tmp/win_final.json")) if __import__("os").path.exists("/tmp/win_final.json") else {}
if d:
    c = d.get("campaign", {})
    print(f"   板子自述：hit={c.get('hit')} best_delay={c.get('best_delay')} best_v_dip={c.get('best_v_dip')}")
PY

echo
echo "== 证据已存 $DIR =="
ls -l "$DIR" | tr -s ' '
echo "下一步（必须你明确确认才做）：生成 burn 计划 → crosscheck 必须 0 问题 → 才谈 sense/arm/burn。"
echo "窗口是易失的：别在这期间发 r、别加载驱动、别重启。"
