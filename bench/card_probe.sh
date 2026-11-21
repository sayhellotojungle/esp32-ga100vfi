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

echo "== 3. 配置空间到底答不答（`lspci` 那张表是枚举缓存，不答也能列着）=="
# 2026-11-21 的误判形状：卡烧成功之后冷启动，`lspci` 列着设备、BAR0 整片 `0xffffffff`，
# 差一步判成"卡死了"。真因是 Command 的 Memory Space Enable 从没被置起来（没有驱动 claim
# 这张卡）。所以先看 VID/DID 的原始字，再看 Command bit1，最后才轮到 BAR0。
BDF=${BDF:-0000:01:00.0}
VID=$(setpci -s $BDF 0.w 2>/dev/null); DID=$(setpci -s $BDF 2.w 2>/dev/null)
CMD=$(setpci -s $BDF 4.w 2>/dev/null)
printf '   %s  VID:DEV=%s:%s  Command=%s\n' "$BDF" "${VID:-无}" "${DID:-无}" "${CMD:-无}"
case "${VID:-ffff}" in
  ffff)
    echo "   [!!] 配置空间不答 → 掉卡或没电。按下面「两种死法」分形状；retrain/rescan 实测 4/4 无效"
    echo "        （板子拉 PERST# 能叫回来 = 那一发作废记 void；叫不回 = 断电，记 dead）"
    ;;
  *)
    echo "   [ok] 配置空间在答：卡活着。"
    if [ -n "${CMD:-}" ] && [ $((0x$CMD & 2)) -eq 0 ]; then
      echo "   [!!] Command 的 bit1(Memory Space Enable)=0 → **BAR0 会整片读回 0xffffffff，这不是死**"
      echo "        处置：cd \$HOME/ga100vfi/host && python3 -m ga100vfi.burn --bdf $BDF wake"
    else
      echo "   [ok] memory decode 已开：BAR0 可读（判 poison 仍要先看 device id，见 poison_check.sh）"
    fi
    ;;
esac

echo "== 4. 上游桥还在吗（卡不在时桥通常仍在，能区分「槽没了」与「设备没了」）=="
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

echo "== 5. dmesg 里最近的 PCIe / 显卡相关记录（只看，不解读）=="
dmesg 2>/dev/null | grep -iE 'pci|aer|pcie|nvidia|10de|hub' | tail -25 | sed 's/^/   /'

echo "== 6. 有没有残留的 nvidia 驱动绑着（必须为 0，驱动加载会关窗口）=="
cat /sys/bus/pci/devices/$BDF/driver 2>/dev/null >/dev/null \
  && echo "   [!!] 有驱动绑定" || echo "   [ok] 无驱动绑定"
lsmod 2>/dev/null | grep -iE '^nvidia' | sed 's/^/   loaded: /'
systemctl is-enabled nvidia-persistenced 2>/dev/null | sed 's/^/   persistenced: /'

echo "== 7. 内核看到的设备总数（rescan 前后各留一份档才有意义）=="
echo "   $(find /sys/bus/pci/devices -maxdepth 1 -type l | wc -l) 个设备节点"
echo
echo "本脚本不 rescan、不写配置空间（第 3 步只读）。rescan / 对桥发 Retrain 实测 4/4 叫不回卡，"
echo "并且两次把整机带走：它已从开火路径上禁用，只在你确认要拿它排除桥侧故障时手工做。"
echo "第 3 步那种 Command=0 的形状用 wake 就能解，不该动用 rescan："
echo "   echo 1 > /sys/bus/pci/rescan"
