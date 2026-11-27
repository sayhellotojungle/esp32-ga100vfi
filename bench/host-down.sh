#!/bin/bash
# 需要给卡断电时用它（判决机一停，槽位 12V 就没了；3V3_AUX 是否跟着掉要看主板，
# 所以下一步仍然是人来彻底断电/再开机）。2026-11-25 起这是本 bench 的标准断电动作：
# 判决方远程 shutdown，人来按电源，见 README"卡不见了"一节。
#
#   bash bench/host-down.sh "为什么断（会写进台账）"
#
# 只关机器、不碰卡：这台机器重启不会伤熔丝（OTP 在卡里），正在跑的那一批会丢。
set -uo pipefail
GA=/root/ga100vfi
M=/root/mig-card2
WHY="${1:-未说明}"
BDF=0000:01:00.0

echo "== 1. 停止开火（摘开关；本 bench 不挂 cron，机器回来由判决方手动起下一批）=="
rm -f "$M/FIRE"
pkill -f "$M/fire.sh" 2>/dev/null
pkill -f 'bench/crack.sh' 2>/dev/null
sleep 3
pkill -9 -f 'ga100vfi_age[nt]' 2>/dev/null
curl -s -m 5 "http://192.168.67.118/api/campaign?op=stop" >/dev/null 2>&1
echo "   残留：fire=$(pgrep -fc 'fire.sh' || true) crack=$(pgrep -fc 'bench/crack.sh' || true) agent=$(pgrep -fc ga100vfi_agent || true)"

echo "== 2. 留痕：卡此刻的状态 + 原因 =="
CFG=$(head -c 4 /sys/bus/pci/devices/$BDF/config 2>/dev/null | od -An -tx1 | tr -s ' ')
WIN=$( (cd $GA/host && python3 -m ga100vfi.burn --bdf $BDF status 2>&1 | grep -c 'goldilocks window: OPEN') || true)
{
  echo "$(date +%FT%T) host-down：$WHY"
  echo "   卡 config[0..3]=${CFG:-节点不在}  窗口 OPEN=${WIN:-读不到}  正在跑的批已停"
  echo "   下一步：人来（彻底断电 ≥30 s 再开机，然后告知判决方手动起下一批）。"
} | tee -a "$M/DECISIONS.txt"
if [ "${WIN:-0}" != "0" ]; then
  echo "[!!] 关机前窗口是 OPEN 的：那一发的 arm 会被这次断电冲掉，先想清楚"
fi

echo "== 3. 落盘并关机 =="
sync
shutdown -h now
echo "   已发出 shutdown，机器正在停。请彻底断电 >=30 s 后再开机。"
