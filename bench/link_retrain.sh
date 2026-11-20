#!/bin/bash
# 链路 x0 的最后两个软件判据/手段，然后才轮到断电。
# 边界：本脚本对「卡」完全只读；唯一的写是对「上游桥」的 LnkCtl Retrain Link 位，
# 那张卡此刻根本不在总线上，写桥不会伤到它。
set -uo pipefail
RP=${RP:-0000:00:1c.0}
BDF=${BDF:-0000:01:00.0}

echo "== 1. 卡到底有没有电：PMBus 问一次（板子侧，只读）=="
CON=/tmp/.console.sh
if [ ! -f "$CON" ]; then
  echo "   现场重建串口助手"
  cat > "$CON" <<'CON_EOF'
#!/bin/bash
set -uo pipefail
PORT=${PORT:-/dev/ttyUSB0}
OUT=/tmp/con.gen.log
systemctl stop ModemManager 2>/dev/null || true
fuser -k "$PORT" 2>/dev/null || true
sleep 0.5
stty -F "$PORT" 115200 cs8 -cstopb -parenb raw -echo -hupcl 2>/dev/null || exit 1
( timeout "$((3 + 3 * $#))" cat "$PORT" > "$OUT" 2>/dev/null ) & L=$!
sleep 0.4
for c in "$@"; do printf '%s\r\n' "$c" > "$PORT"; sleep 1.2; done
wait "$L" 2>/dev/null
tr -d '\r' < "$OUT" | grep -vE '^ERR (invalid or overlong|unknown) command$' | tail -20
CON_EOF
fi
Z=$(bash "$CON" z 2>&1)
echo "$Z" | grep -m2 -E 'cml=|ERR' | sed 's/^/   /'
case "$Z" in
  *"cml="*) echo "   → VRM 应答：卡有电。PCIe 训练失败与供电无关（指向 PHY 未 POR）";;
  *)        echo "   → VRM 不应答：卡可能整片没电，先查 12V/3V3 线";;
esac

echo
echo "== 2. 上游桥当前的 LnkCtl/LnkSta（写之前留档）=="
LNKCTL=$(setpci -s "$RP" CAP_EXP+0x10.w 2>/dev/null)
LNKSTA=$(setpci -s "$RP" CAP_EXP+0x12.w 2>/dev/null)
echo "   $RP LnkCtl=$LNKCTL LnkSta=$LNKSTA"

echo
echo "== 3. 对桥下发生链路重训练（Retrain Link = LnkCtl bit5）=="
# 卡不在总线上，这一步只可能让链路起来，不可能改动卡里的任何东西。
[ -n "$LNKCTL" ] || { echo "   setpci 读不到桥，跳过（不要硬来）"; exit 1; }
setpci -s "$RP" CAP_EXP+0x10.w=$(printf '%04x' $(( 0x$LNKCTL | 0x20 ))) 2>&1 | sed 's/^/   /'
sleep 2
echo "   重训练后：LnkSta=$(setpci -s "$RP" CAP_EXP+0x12.w 2>/dev/null)"
echo "   桥的 DLLLA(bit13)：$([ $(( 0x$(setpci -s "$RP" CAP_EXP+0x12.w 2>/dev/null) & 0x2000 )) -ne 0 ] && echo 已建立 || echo 仍未建立)"

echo
echo "== 4. 内核侧再试一次枚举（卡若已应答就会被看见）=="
echo 1 > /sys/bus/pci/rescan 2>/dev/null
sleep 2
if [ -d "/sys/bus/pci/devices/$BDF" ]; then
  VD=$(setpci -s "$BDF" 0.w 2>/dev/null)
  echo "   [ok] $BDF 回来了：vendor:device 原始字=0x$VD"
  lspci -nn -d 10de: | sed 's/^/   /'
  echo "   下一步：只读一次 FUSECTRL 判 poison（bench/poison_check.sh），不要直接 classify"
else
  echo "   [!!] rescan 之后仍然没有 $BDF：软件手段用尽"
  echo "        结论：卡有电、板子没按住复位、桥发不出链路 = PHY 没有 POR"
  echo "        需要真正断电：拔 12V 与 3V3/VDD33 ≥30 s（热重启不清它）"
fi
