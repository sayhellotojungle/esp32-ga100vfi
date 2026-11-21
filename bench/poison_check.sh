#!/bin/bash
# 发前闸门：**只看 config space**，一次 BAR0 都不花。
#
# 为什么改成 config-only（2026-11-21 的血账）：
#   今天判决机整机死了五次。前四次都记在"卡掉链期间访问它的 BAR0"上；第五次最刺眼 ——
#   板子日志里**一发都没打**（/api/log 只有上一轮的 vd=354），机器却死了。也就是说，把
#   机器带走的根本不需要波形：开火之前我们自己对卡做的探测就够把它打死。
#   而开火前唯一碰 BAR0 的动作就是这个脚本原来的做法：bar0.restore()（pwrite 卡的
#   COMMAND 打开 mem decode）+ mmap 读 FUSECTRL —— 而且它是**每一发之前**都做，做的时
#   候卡还在上一发的 devinit 余波里（每发 g 自带 PERST#）。
#
#   毒态判据本来就有纯 config 的版本，而且比 FUSECTRL 更宽：健康只有一个值 10de:20c2，
#   毒态我们实测到 0x2080 与 0x1061 两种。所以发前这一问完全不需要花 BAR0。
#   FUSECTRL 那一读（红线 1 的"判定只允许一次"）留给开火之后的判决会话去做 —— 那里本来
#   就要读 DSOV/CRC，不多这一次。
#
# 需要看 FUSECTRL 时显式说明：POISON_MODE=fuse bash bench/poison_check.sh
#   （人工确认熔丝前后状态用；连发流程永不用）
#
# 退出码：0 可以开火 / 3 不可开火（设备不在总线、非常规态）/ 4 毒态（红线 1，停）
set -uo pipefail
# 部署目录与目标卡都由环境给出（默认 $HOME/ga100vfi、单卡机器的 0000:01:00.0）：
# 台架脚本不写死任何一台机器的地址或路径。
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
MODE=${POISON_MODE:-config}
PROBE_LOG=${PROBE_LOG:-$GA_BASE/logs/probe.log}
mkdir -p "$(dirname "$PROBE_LOG")"
cd "$GA_BASE/host" || exit 1
python3 - "$MODE" "$PROBE_LOG" <<'PY'
import os, struct, sys, time

mode, probe_log = sys.argv[1], sys.argv[2]
bdf = os.environ.get("BDF", "0000:01:00.0")


def step(msg):
    """每一步都先落盘再动作。机器是硬挂死的：内核连一条 MCE 都留不下，能定位"死在
    哪一次访问之前"的就只有这种提前写好的脚印。05:13 那次就是靠它才能说清凶手是谁。"""
    stamp = time.strftime("%H:%M:%S")
    os.environ["GA_STEP"] = msg
    with open(probe_log, "a", encoding="utf-8") as f:
        f.write(f"{stamp} {os.getpid()} [{mode}] {msg}\n")
        f.flush()
        os.fsync(f.fileno())
    print(f"  step: {msg}", flush=True)


# 1) 纯 config：卡还在总线上吗、报的是健康 id 吗（一次 BAR0 都不花）
step("config pread vendor/device/command/bar0")
try:
    fd = os.open(f"/sys/bus/pci/devices/{bdf}/config", os.O_RDONLY)
except OSError as e:
    print(f"  → {bdf} 不在总线上（{type(e).__name__}）：不开火。用板子的 PEX_RST 拉一次。")
    raise SystemExit(3)
try:
    vd = struct.unpack("<I", os.pread(fd, 4, 0))[0]
    cmd = struct.unpack("<H", os.pread(fd, 2, 4))[0]
    bar = struct.unpack("<I", os.pread(fd, 4, 0x10))[0]
finally:
    os.close(fd)
dev, ven = (vd >> 16) & 0xFFFF, vd & 0xFFFF
print(f"config-only: id={ven:04x}:{dev:04x} cmd={cmd:#06x} bar0={bar:#010x}")

if vd == 0xFFFFFFFF:
    # 退出码 3 = "不可开火"（设备不在总线）。曾经这里是无参 SystemExit(=0)，于是
    # 闸门把"卡不在"当成"不是 poison、可以继续"，白打了一发在空气上（第 29 轮实测）。
    print("  → 配置空间全 1：设备层就不应答了。卡不在总线 → 不开火。")
    raise SystemExit(3)

if ven == 0x10DE and dev != 0x20C2:
    # 毒态在 config space 里就看得见：CMP 170HX 健康时是 10de:20c2；控制器一 trip 就不报
    # 20c2 —— 实测出现过 0x2080 与 0x1061 两种，所以判据是"不是 20c2"，不是"等于 0x2080"。
    #
    # 这里必须**直接退出**，config-only 模式下后面本来也没有 BAR0；而 POISON_MODE=fuse
    # 时更要退出：bar0.restore() 会写卡的 command 寄存器再映射 BAR0，毒态下动它的配置空间
    # 正是能把上游桥连带整台判决机一起带走的动作（2026-11-21：上一发判出毒态、device 还是
    # 0x1061 时我们照常走了闸门，下一发开火期间判决机整机死掉）。红线 1 说的"毒态之后不许
    # 再读 BAR0"要连 restore 一起禁才算真守住。
    print(f"  → device id 报 {dev:#06x} 而不是 0x20c2 —— 毒态指纹。红线 1：不开 BAR0、"
          f"不 restore，停在这里。")
    print("     恢复：板子把 PEX_RST 按住 2 s 再释放（实测 0xbadf510e → 0xe0040000，"
          "device id 回到 10de:20c2），然后重读本闸门。")
    raise SystemExit(4)

if ven != 0x10DE:
    # 认不出来的厂商：这台机器插的不是我们那块卡，或者内核把别的设备报到了这个 BDF。
    print(f"  → {ven:04x}:{dev:04x} 不是 NVIDIA 的卡，BDF 认错人了 → 不开火。")
    raise SystemExit(3)

if mode != "fuse":
    print("  → 发前闸门（config-only）通过：卡在场、id 健康。BAR0 一次没花。")
    print("     FUSECTRL 那一读留给开火后的判决会话（红线 1：判定只读一次）。")
    raise SystemExit(0)

# 2) 只有显式 POISON_MODE=fuse 才走到这里：人工确认控制器状态，连发流程不进来。
from ga100vfi import bar0                                # noqa: E402
from ga100vfi.classify import FUSECTRL                   # noqa: E402

step("bar0.restore()  <- 写卡 config 的 BAR 基址与 COMMAND.mem，风险动作")
if not bar0.restore(bdf, wait_s=1.0, settle_s=0.15):
    print("  restore 失败：卡不在 PCIe 上 → 不开火")
    raise SystemExit(3)
step("Bar0() mmap + rd32(FUSECTRL)  <- 全脚本唯一一次 MMIO")
with bar0.Bar0(bdf) as bar:
    ctrl = bar.rd32(FUSECTRL)
print(f"  FUSECTRL = {ctrl:#010x}")
if (ctrl & 0xFFFF0000) == 0xBADF0000:
    print("  → 仍然 poison。此时读熔丝块（CRC/DSOV/FF 区）就是文档红线 1，停在这里。")
    print("     恢复不需要断电：2026-11-21 实测，板子把 PEX_RST 按住 2 s 再释放，")
    print("     FUSECTRL 从 0xbadf510e 回到 0xe0040000，device id 也从 10de:2080 回到 10de:20c2。")
    raise SystemExit(4)
elif (ctrl & 0xFFFF0000) == 0xE0040000:
    print("  → 已回 IDLE：控制器干净，不需要断电，可以直接继续打。")
else:
    print(f"  → 非常规值 {ctrl:#010x}：非 IDLE 也非 poison，按红线 2 不许继续操作；")
    print("     同样先用板子的 PEX_RST（j → ≥2 s → J）拉一次，再重读本闸门。")
    raise SystemExit(3)
PY
