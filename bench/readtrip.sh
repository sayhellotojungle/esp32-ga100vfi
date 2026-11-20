#!/bin/bash
# 零发实验：一个脉冲都不打，只重复判决链自己会做的那些 BAR0 访问，看控制器会不会被
# **我们自己的访问**打进 poison（FUSECTRL=0xBADFxxxx）。
#
# 为什么要做这个：两轮实测形态完全一样 —— 第 1 发 under，第 2 发 poison，换 delay 点
# 也一样（logs/20261120-011021、20261120-012522）。"电压太深"应该是概率性的、跟着参数
# 走的；跟着"第几次"走的确定性形态，更像是我们这边有状态在累积。嫌疑人有两个：
#
#   (A) 第二发脉冲本身（电压/能量太深）；
#   (B) 判决链自己的动作 —— classify() 读 6 个熔丝寄存器，并且**写 DSOV_SET=1**。
#
# README 安全红线第 7 条早就说过「分类本身会写 DSOV」，所以 B 完全值得单独测一次。
# 这个实验花 0 发脉冲：如果一次脉冲都不打、只重复 classify 就能打出 0xBADF，那罪不在
# 脉冲参数，而在判决路径 —— 接下来的打法要整个改掉（少读、能不写 DSOV_SET 就不写）。
#
# 全程遵守红线 1：每一次 BAR0 访问之后**只**读一次 FUSECTRL 问一句，问到 poison 立刻
# 停手并报告是哪一步造成的，绝不继续读。
#
# 用法：bash bench/readtrip.sh          # 上电后先跑这个，再决定要不要开火
set -uo pipefail
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
BDF=${BDF:-0000:01:00.0}
cd "$GA_BASE/host" || exit 1

python3 - "$BDF" <<'PY'
import sys
from ga100vfi import bar0
from ga100vfi.classify import (CRC_STATUS, DSOV, DSOV_SET, FPF_ROW, FUSECTRL,
                               PRIV_MASK, SHADOW, UNREADABLE)

bdf = sys.argv[1]
if not bar0.restore(bdf, wait_s=1.0, settle_s=0.2):
    print("   [!!] 配置空间不可达：卡不在 PCIe 上"); sys.exit(2)

STOP = object()

def step(bar, label, fn, writes=False):
    """Do one access, then ask the single question. A trip names its own culprit."""
    try:
        value = fn()
    except OSError as exc:
        print(f"   {label:<46} 访问出错：{exc}")
        return STOP
    ctrl = bar.rd32(FUSECTRL)
    trip = (ctrl & 0xFFFF0000) == 0xBADF0000
    shown = "?" if value is None else (
        f"0x{value:08x}" if isinstance(value, int) else str(value))
    print(f"   {label:<46} -> {shown:<12} FUSECTRL=0x{ctrl:08x}"
          f"{'   <== 就这一步把它打进 poison 了' if trip else ''}"
          f"{'   [含写]' if writes else ''}")
    return STOP if trip else None

with bar0.Bar0(bdf) as bar:
    ctrl = bar.rd32(FUSECTRL)
    print(f"起点：FUSECTRL=0x{ctrl:08x}")
    if (ctrl & 0xFFFF0000) == 0xBADF0000:
        print("   [!!] 还没动就已经 poison —— 这次实验没有起点，先重启判决机（POST 拉 PERST#）")
        sys.exit(3)

    print("\n== 阶段 1：反复只读 FUSECTRL（对照组，证明「问一句」本身无害）==")
    for i in range(5):
        if step(bar, f"#{i+1} 读 FUSECTRL", lambda: bar.rd32(FUSECTRL)) is STOP:
            print("\n结论：连 FUSECTRL 自己的重复读都能触发 —— 那不是我们的动作，是硬件状态。")
            sys.exit(4)

    print("\n== 阶段 2：读判决要用的熔丝寄存器（只读）==")
    for name, off in (("DSOV", DSOV), ("FPF_ROW", FPF_ROW), ("PRIV_MASK", PRIV_MASK),
                      ("CRC_STATUS", CRC_STATUS)):
        if step(bar, f"读 {name}@{off:#010x}", lambda off=off: bar.rd32(off)) is STOP:
            print(f"\n结论：读到 {name} 就 poison。判决链不能再用它 —— 改用只读子集。")
            sys.exit(4)
    for name, off in SHADOW.items():
        if step(bar, f"读 {name}@{off:#010x}", lambda off=off: bar.rd32(off)) is STOP:
            print(f"\n结论：shadow 寄存器 {name} 是触发点。")
            sys.exit(4)

    print("\n== 阶段 3：classify() 唯一的写 —— DSOV_SET=1（红线 7 说的那个写）==")
    for i in range(3):
        if step(bar, f"#{i+1} 写 DSOV_SET=1", lambda: bar.wr32(DSOV_SET, 1), writes=True) is STOP:
            print("\n结论：**判决链自己把控制器写进 poison** —— 罪不在脉冲参数。")
            print("      接下来该改的是判决路径（能不写 DSOV_SET 就不写），不是电压配方。")
            sys.exit(4)
        if step(bar, f"#{i+1} 回读 DSOV_SET", lambda: bar.rd32(DSOV_SET)) is STOP:
            print("\n结论：回读 DSOV_SET 触发。")
            sys.exit(4)

    print("\n== 全部通过：我们的访问序列是清白的 ==")
    print("   → 「第 2 发必 poison」的嫌疑落回脉冲本身（第二发的累积能量/故障闩锁），")
    print("     下一轮开火时看 agent 打印的每发 FUSECTRL/DSOV/CRC 就能分开。")
PY
