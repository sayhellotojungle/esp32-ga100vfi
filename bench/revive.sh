#!/usr/bin/env bash
# 卡离链（config 读回全 1）之后，先花 10 秒试一次"板端复位脉冲 + 主机 rescan"再决定要不要断电。
#
# 为什么值得试：根源那篇（GA100VFI 原文）的循环是"每次尝试都要重来一次电源周期"，而它把这件事
# 做成 S3→S0，几秒就完；我们的电源周期是人肉断电 + 几分钟。今天实测过一次：卡离链之后
# **VRM 仍在答 PMBus**（`z` 回 cml=0x28 pwd=1 e1=0xc2ea），只是 PCIe 功能不见了 —— 也就是说
# "卡死了"与"PCIe 功能没起来"在我们唯一的观测面上本来就分不开，而后者有可能一次 PERST 脉冲就好。
# 试错的代价几乎为零：配方没动、熔丝没碰（熔丝烧写在窗口里才发生，而窗口是关的）、
# PERST 脉冲本来就是每一发都做的事（去掉坑）。请不回来再断电，一条都不亏。
#
# 一条硬前置：**战役不在跑**。板子正在打的时候拉 PERST 会把正在判决的那一发弄成假 `lost`，
# 那是往台账里注水，不是省时间。
set -uo pipefail
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
export BOARD=${BOARD:-}
BDF=${BDF:-0000:01:00.0}
CFG=/sys/bus/pci/devices/$BDF/config
WAIT=${REVIVE_WAIT:-6}          # 释放之后给卡跑 ROM/devinit 的时间（我们判决用的下限是 4 s，这里放宽）
CON=${CON:-$GA_BASE/bench/console.sh}

now() { head -c 4 "$CFG" 2>/dev/null | od -An -tx1 | tr -d ' \n'; }
echo "== 0. 现在在场吗 =="
id=$(now); echo "   config[0..3] 读到：${id:-（节点不在）}"
case "$id" in
  de10*) echo "   卡在场，不用救（这一步什么都不做）"; exit 0;;
  ffffff*) : ;;
  *) echo "   读不到身份（节点不在）：先 rescan 一次看是不是只是节点被摘了";;
esac

echo
echo "== 1. 战役必须在 idle（正在打的时候不许拉 PERST）=="
if [ -n "$BOARD" ]; then
  st=$(curl -s -m 8 "$BOARD/api/status" | python3 -c '
import json,sys
try: s=json.load(sys.stdin)
except Exception: print("unknown"); raise SystemExit
c=s.get("campaign",{})
print("running" if c.get("running") else ("phase=%s" % c.get("phase","?")))')
  echo "   板端战役：$st"
  [ "$st" = "running" ] && { echo "   [!!] 战役在跑：不救（会把手里的判决弄假）"; exit 3; }
else
  echo "   （没给 BOARD：跳过这项检查，风险自担）"
fi

echo
echo "== 2. 一次 PERST 脉冲（板上 'k'：拉低 100 ms 再放），然后等 ${WAIT}s =="
[ -f "$CON" ] || { echo "   [!!] 找不到 $CON（部署过吗）；不盲试"; exit 3; }
# 必须**看见** `released` 才算脉冲打进了板子。第一次跑这一步时我用 `timeout 20 bash console.sh k`
# 拿到退码 124 就下结论"叫不回"，而 124 只是我把超时设进了 console.sh 的三轮预算之内 —— 那次连
# 脉冲有没有落地都不知道。所以：判据 = 板子对 'k' 的答复；超时 = console.sh 自己的预算（同一个
# 公式：每轮 CON_WAIT 秒监听 + CON_REPEAT 次重发各 1 s，再加清残留的固定开销）外加一点余量。
export CON_GREP=released CON_TRIES=${CON_TRIES:-2} CON_WAIT=${CON_WAIT:-8} CON_REPEAT=${CON_REPEAT:-5}
BUDGET=$(( CON_TRIES * (CON_WAIT + CON_REPEAT + 1) + 6 ))
out=$( cd "$GA_BASE" && timeout "$BUDGET" bash "$CON" k 2>&1 )
echo "$out" | tail -3 | sed 's/^/   /'
case "$out" in
  *released*) : ;;
  *) echo "   [!!] 没看到板子回 released：脉冲是否落地**未知**，这一轮的\"叫不回\"不算证据"
     exit 5 ;;
esac
sleep "$WAIT"

echo
echo "== 3. rescan（只碰配置空间，不开 BAR0）=="
[ -e "/sys/bus/pci/devices/$BDF" ] && echo 1 > "/sys/bus/pci/devices/$BDF/remove" 2>/dev/null
echo 1 > /sys/bus/pci/rescan
sleep 2
id=$(now); echo "   config[0..3] 读到：${id:-（节点不在）}"
case "$id" in
  de10*) echo "   [ok] 请回来了。下一步只读一次熔丝控制器确认状态：bash bench/poison_check.sh"; exit 0;;
esac
echo "   没请回来：这次只能断电 >=30 s。今天量过一次同样的形状（VRM 活着、E1 已交回 PWM-VID 模式、"
echo "   rescan 无效），所以这不是我们漏了什么 —— 是这张卡的 PCIe 功能真的没了。"
exit 4
