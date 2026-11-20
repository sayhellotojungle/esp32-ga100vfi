#!/bin/bash
# 一次、且仅一次 BAR0 读：问 FUSECTRL 还锁不锁。
# 红线 1 禁止的是在 poison 态下继续读熔丝块（CRC/DSOV/FF 区），那会挂死 PCIe 上游。
# FUSECTRL 本身我们已经读过几次并且设备正常应答，所以这一问的风险是有界的。
# 除了这一问，还有若干次纯 config space 的 pread（不碰 BAR0）。
set -uo pipefail
# 部署目录与目标卡都由环境给出（默认 $HOME/ga100vfi、单卡机器的 0000:01:00.0）：
# 台架脚本不写死任何一台机器的地址或路径。
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
cd "$GA_BASE/host" || exit 1
python3 - <<'PY'
import os, struct
from ga100vfi import bar0
from ga100vfi.classify import FUSECTRL

bdf = os.environ.get("BDF", "0000:01:00.0")

# 1) 纯 config：卡还在总线上吗（不碰 BAR0）
fd = os.open(f"/sys/bus/pci/devices/{bdf}/config", os.O_RDONLY)
vd = struct.unpack("<I", os.pread(fd, 4, 0))[0]
cmd = struct.unpack("<H", os.pread(fd, 2, 4))[0]
bar = struct.unpack("<I", os.pread(fd, 4, 0x10))[0]
os.close(fd)
print(f"config-only: id={vd & 0xFFFF:04x}:{(vd >> 16) & 0xFFFF:04x} cmd={cmd:#06x} bar0={bar:#010x}")
if vd == 0xFFFFFFFF:
    print("  → 配置空间全 1：设备层就不应答了，这不是 poison 的问题"); raise SystemExit

# 2) 只读 FUSECTRL 一次
if not bar0.restore(bdf, wait_s=1.0, settle_s=0.15):
    print("  restore 失败：卡不在 PCIe 上"); raise SystemExit
with bar0.Bar0(bdf) as bar:
    ctrl = bar.rd32(FUSECTRL)
print(f"  FUSECTRL = {ctrl:#010x}")
if (ctrl & 0xFFFF0000) == 0xBADF0000:
    print("  → 仍然 poison。此时读熔丝块（CRC/DSOV）就是文档红线 1，必须冷启；")
    print("     热重启不清它（3.3V 辅助电一直给着），PEX_RST 也只是重放熔丝链，不是 POR。")
elif (ctrl & 0xFFFF0000) == 0xE0040000:
    print("  → 已回 IDLE：控制器干净，不需要断电，可以直接继续打。")
else:
    print(f"  → 非常规值 {ctrl:#010x}：非 IDLE 也非 poison，按红线 2 不许继续操作，先冷启。")
PY
