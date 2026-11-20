#!/bin/bash
# 只读体检：卡重启后到底在不在。全程不碰 BAR0，不写任何寄存器，不 load 驱动。
set -uo pipefail

echo "== 1. PCI 总桥上有哪些 NVIDIA 设备（含非 20c2 的兜底设备）=="
lspci -nn -d 10de: 2>&1 | sed 's/^/   /'
[ -n "$(lspci -nn -d 10de: 2>/dev/null)" ] || echo "   （一个都没有）"

echo "== 2. 期望地址与备选地址的设备节点是否存在 =="
# 候选是通用槽位号（单卡机器的常见枚举位置），不是某台机器的地址；多卡/别的槽位用
# CARD_CANDIDATES="…" 覆盖。
CARD_CANDIDATES=${CARD_CANDIDATES:-"0000:01:00.0 0000:02:00.0 0000:01:00.1"}
for bdf in $CARD_CANDIDATES; do
  d=/sys/bus/pci/devices/$bdf
  if [ -d "$d" ]; then
    printf '   %s 存在: vendor=%s device=%s class=%s\n' "$bdf" \
      "$(cat $d/vendor)" "$(cat $d/device)" "$(cat $d/class)"
  else
    printf '   %s 不存在\n' "$bdf"
  fi
done

echo "== 3. 上游桥还在吗（卡不在时桥通常仍在，能区分「槽没了」与「设备没了」）=="
for bridge in /sys/bus/pci/devices/0000:00:*/ ; do
  b=${bridge%/}; bdf=${b##*/}
  class=$(cat "$b/class" 2>/dev/null)
  case "$class" in
    0x0604*)
      printf '   %s bridge  status=%s  speed=%s width=%s\n' "$bdf" \
        "$(cat $b/status 2>/dev/null)" \
        "$(cat $b/current_link_speed 2>/dev/null)" \
        "$(cat $b/current_link_width 2>/dev/null)"
      ;;
  esac
done

echo "== 4. dmesg 里最近的 PCIe / 显卡相关记录（只看，不解读）=="
dmesg 2>/dev/null | grep -iE 'pci|aer|pcie|nvidia|10de|hub' | tail -25 | sed 's/^/   /'

echo "== 5. 有没有残留的 nvidia 驱动绑着（必须为 0，驱动加载会关窗口）=="
BDF=${BDF:-0000:01:00.0}
cat /sys/bus/pci/devices/$BDF/driver 2>/dev/null >/dev/null \
  && echo "   [!!] 有驱动绑定" || echo "   [ok] 无驱动绑定"
lsmod 2>/dev/null | grep -iE '^nvidia' | sed 's/^/   loaded: /'
systemctl is-enabled nvidia-persistenced 2>/dev/null | sed 's/^/   persistenced: /'

echo "== 6. rescan 之前先留档：内核看到的设备总数 =="
echo "   $(find /sys/bus/pci/devices -maxdepth 1 -type l | wc -l) 个设备节点"
echo
echo "本脚本不做 rescan、不写配置空间。要不要 rescan 由人决定（下面的命令备用）："
echo "   echo 1 > /sys/bus/pci/rescan"
