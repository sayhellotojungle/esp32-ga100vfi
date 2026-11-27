#!/usr/bin/env bash
# 现场只读勘查：SSH 掉线/重启之后回答"卡死没死、批跑完没、窗口什么状态"。
# 一条功能：只看不碰——不开战役、不发 op=start、不写任何状态。
# 2026-11-26 由 temp/r_aftermath.sh + temp/r_precheck.sh 合并而来（temp/ 白名单测试要求固化进 bench/）。
set -uo pipefail
GA=${GA_BASE:-/root/ga100vfi}
BOARD=${BOARD:-http://192.168.67.117}
BDF=${BDF:-0000:01:00.0}
echo "== 机器与地址 =="
hostname; uptime
ip -br a | grep -E 'enp2s0|lo '
echo "== 有没有 orphan 的 crack/代理在跑 =="
pgrep -af 'crack.sh' | head -4
pgrep -af 'ga100vfi_age[nt]' | head -4
echo "== 最新一批的目录与逐发判决 =="
R=$(ls -1dt "$GA"/logs/*-crack-* 2>/dev/null | head -1)
echo "   run=$R"
tail -40 /root/mig-card2/manual-batch.log 2>/dev/null
grep -a -e 'attempt' -e '-> ' "$R/agent.log" 2>/dev/null | tail -20
echo "== 板子自报（boot=1 表示没被咬）=="
curl -s -m 8 "$BOARD/api/status" | head -c 600; echo
echo "== 板端原文（最新 run 的 board.log 尾部 + campaign.csv：核对板子实际执行的 delay）=="
grep -a -e 'glitch done' -e 'pulses=' "$R/board.log" 2>/dev/null | tail -6
tail -4 "$R/campaign.csv" 2>/dev/null
echo "== 板子自报收尾（final.json campaign 段：session/attempt/tries 是坐标的独立证据）=="
if [ -f "$R/final.json" ]; then
  python3 - "$R/final.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))["campaign"]
keys = ("session", "attempt", "tries", "delay", "v_dip", "v_code",
        "under", "lost", "goldilocks", "phase")
print("  ", {k: c.get(k) for k in keys})
PY
else
  echo "   no-final.json（批被重启/中断掐掉，坐标只能信 board.log 残窗）"
fi
echo "== 台账 gold 行认身份（ledger.txt 带 MM-DD 与 run 名，认得出是哪一批）=="
grep -a goldilocks "$GA/logs/ledger.txt" 2>/dev/null | tail -6
grep -a goldilocks /root/logs/verdicts.tsv 2>/dev/null | tail -3
echo "== 卡还在不在（只读 config，不开 BAR0）=="
head -c 4 /sys/bus/pci/devices/$BDF/config 2>/dev/null | od -An -tx1 | tr -s ' ' | sed 's/^/   config[0..3]: /'
echo "== burn status（只读：identity/DSOV/CRC/window）=="
( cd "$GA/host" && python3 -m ga100vfi.burn --bdf "$BDF" status )
