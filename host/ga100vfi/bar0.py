"""BAR0 access and post-reset PCIe restore for a GA100 function."""
from __future__ import annotations

import mmap
import os
import struct
import time

DEFAULT_BDF = "0000:1a:00.0"
DEFAULT_SIZE = 16 << 20  # 16 MiB, the GA100 BAR0 window
# The upstream port this card sits behind. Read-only use only (see link_state): the
# bench must never WRITE a bridge register — two bridge writes hung the whole judgment
# host on 2026-11-21 (retrain + rescan, 4/4 useless, and it took the box down twice).
DEFAULT_BRIDGE = os.environ.get("GA_BRIDGE", "0000:00:01.0")
HEALTHY_DEVICE_ID = 0x20C2         # CMP 170HX as-built; a tripped controller reports otherwise
PCI_CAP_ID_PCIE = 0x10
PCIEXP_LINK_STATUS = 0x12


def _probe(msg: str) -> None:
    """Write-ahead trace for every risky reach at the card, flushed before we do it.

    The judgment host died five whole-host hangs on 2026-11-21 and the kernel did not
    get a single line out -- no MCE, no hung_task, nothing. The only way to learn which
    access killed it is to leave the footprint on disk *before* performing it, so the
    last line of the log names the action that never came back. Cheap (one small append
    per phase) and it is what turned "the card dies on its own" into "we die inside
    restore()". Set GA_PROBE_LOG=off to silence.
    """
    path = os.environ.get("GA_PROBE_LOG") or os.path.expandvars("$HOME/ga100vfi/logs/probe.log")
    if path == "off":
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {os.getpid()} bar0: {msg}\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass                                       # tracing must never break a verdict


def link_state(bridge: str = DEFAULT_BRIDGE) -> dict:
    """Read-only look at the upstream bridge's LinkStatus, so we can tell a live link
    from a dead one before anyone touches the card.

    Returns {} when the bridge cannot be read (dev boxes, tests). Never writes anything:
    the two host hangs on 2026-11-21 both started with writing this bridge.
    """
    path = f"/sys/bus/pci/devices/{bridge}/config"
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return {}
    try:
        ptr = struct.unpack("<B", os.pread(fd, 1, 0x34))[0]
        for _ in range(48):                      # bounded walk; never trust the chain
            if ptr < 0x40 or ptr == 0xFF:
                break
            hdr = os.pread(fd, 2, ptr)
            if len(hdr) < 2:
                break
            if hdr[0] == PCI_CAP_ID_PCIE:
                sta = struct.unpack("<H", os.pread(fd, 2, ptr + PCIEXP_LINK_STATUS))[0]
                return {"cap": ptr, "status": sta,
                        "speed": sta & 0xF, "width": (sta >> 4) & 0x3F,
                        "training": (sta >> 11) & 1, "dll_active": (sta >> 13) & 1}
            ptr = hdr[1]
        return {}
    finally:
        os.close(fd)


class Bar0Unavailable(RuntimeError):
    """The card is not in a state where mapping BAR0 is safe; the caller must not retry."""


def device_ids(bdf: str = DEFAULT_BDF):
    """(vendor, device) from read-only config space, or None when the card is gone.

    Split out of require_mapped() so the gate can be unit-tested without a card: the
    whole point of this function is that the bench refuses to touch BAR0 on evidence
    gathered from config space alone.
    """
    try:
        fd = os.open(f"/sys/bus/pci/devices/{bdf}/config", os.O_RDONLY)
    except OSError:
        return None
    try:
        vd = struct.unpack("<I", os.pread(fd, 4, 0x00))[0]
    finally:
        os.close(fd)
    if vd == 0xFFFFFFFF:
        return None
    return vd & 0xFFFF, (vd >> 16) & 0xFFFF


def link_evidence(bridge: str = DEFAULT_BRIDGE) -> dict:
    """The bridge's LinkStatus, as a *diagnostic string only* -- it must never veto a read.

    Measured on the judgment host 2026-11-21 with the card healthy (config answers
    10de:20c2, link running Gen1 x8 as the kernel itself reports): LnkSta reads 0x1081,
    i.e. speed and width sane while DLLLA (bit 13) and Link Training (bit 11) sit at 0.
    This Intel root port simply does not implement those two bits. The first version of
    the gate refused BAR0 when dll_active==0, which on this machine means it would have
    refused **every shot ever taken** -- and "no shot ever produces a verdict" is a much
    quieter, much more expensive failure than the one it was written to prevent.
    """
    return link_state(bridge)


def card_answers_config(bdf: str = DEFAULT_BDF) -> tuple:
    """The one hard, cheap, read-only proof that this card may be touched.

    Raises Bar0Unavailable (a 'dead'/'poison' verdict, never retryable in-process) when
    * config reads all-ones or the device is off the bus: its link is gone, and MMIO to
      it takes the root complex down with it -- all four whole-host hangs on 2026-11-21
      started exactly that way;
    * the device id is not the healthy 0x20c2: a poisoned fuse controller still answers
      config space (0x2080 and 0x1061 were both measured today), and reading its fuse
      block while poisoned is 安全红线 1.

    Config reads are safe by construction: they are answered by the root port, which is
    why we could read ffff:ffff repeatedly today without ever hanging the machine.
    """
    ids = device_ids(bdf)
    if ids is None:
        _probe(f"gate REFUSE {bdf}: config all-ones / off the bus")
        raise Bar0Unavailable(f"{bdf} does not answer config space: link is gone; no BAR0 session")
    ven, dev = ids
    _probe(f"gate {bdf} config id={ven:04x}:{dev:04x}")
    if ven == 0x10DE and dev != HEALTHY_DEVICE_ID:
        _probe(f"gate REFUSE {bdf}: poisoned device id {dev:#06x}")
        raise Bar0Unavailable(
            f"{bdf} reads {ven:04x}:{dev:04x}, not 10de:{HEALTHY_DEVICE_ID:#06x} "
            f"-- that is the poisoned-controller config fingerprint (安全红线 1: no BAR0 "
            f"reads after a trip). Clear it with the board's PEX_RST, then re-check.")
    return ids


def require_mapped(bdf: str = DEFAULT_BDF, bridge: str = DEFAULT_BRIDGE) -> None:
    """Gate every BAR0 use (both config writes in restore() and the mmap itself).

    The refusal comes from card_answers_config(); link_state() is attached to the
    message but deliberately cannot refuse anything (see link_evidence for why).

    A shot can also kill the card *asynchronously* -- today one verdict came back clean
    and the card vanished from the bus four seconds later -- so this is not the only
    check: Bar0.rd32 re-runs it before every single MMIO read.

    Set GA_SKIP_LINK_GATE=1 to bypass, for offline tooling on a machine with no card.
    """
    if os.environ.get("GA_SKIP_LINK_GATE"):
        return
    try:
        card_answers_config(bdf)
    except Bar0Unavailable as exc:
        sta = link_evidence(bridge)
        raise Bar0Unavailable(f"{exc} | {bridge} LinkStatus="
                              f"{sta.get('status', 0):#06x} dll_active={sta.get('dll_active', 0)}"
                              if sta else str(exc)) from None


def _resource0_start(bdf: str) -> int:
    path = f"/sys/bus/pci/devices/{bdf}/resource"
    try:
        with open(path) as f:
            return int(f.readline().split()[0], 16)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"PCI device {bdf} not found ({path})") from e


def command_reg(bdf: str = DEFAULT_BDF):
    """The Command register (config offset 0x04), or None when config is unreachable.

    Read-only and answered by the root port, so it is safe on a card that is mid-death.
    """
    try:
        fd = os.open(f"/sys/bus/pci/devices/{bdf}/config", os.O_RDONLY)
    except OSError:
        return None
    try:
        return struct.unpack("<H", os.pread(fd, 2, 0x04))[0]
    except OSError:
        return None
    finally:
        os.close(fd)


def bar0_first_word(bdf: str = DEFAULT_BDF):
    """The first dword of BAR0 (PMC_BOOT_0) via a plain pread, or None.

    Used only as a *readability* check: 0xffffffff means the window is not decoding, and
    on this bench that has exactly two causes — memory decode off (fixable in place) or
    the card off the bus (not fixable without power).
    """
    try:
        fd = os.open(f"/sys/bus/pci/devices/{bdf}/resource0", os.O_RDONLY)
    except OSError:
        return None
    try:
        return struct.unpack("<I", os.pread(fd, 4, 0))[0]
    except OSError:
        return None
    finally:
        os.close(fd)


def _pwrite(fd: int, data: bytes, offset: int) -> int:
    """`os.pwrite` on the Linux bench; `lseek`+`write` where pwrite doesn't exist.

    The dev box is Windows and has no `os.pwrite`, and this module's unit tests must run
    there too —— 只能在一个平台上测的配置空间写，等于没有测试的配置空间写。
    """
    try:
        return os.pwrite(fd, data, offset)
    except AttributeError:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.write(fd, data)


def ensure_memory_enable(bdf: str = DEFAULT_BDF):
    """Make BAR0 readable: config-space Memory Space Enable (Command bit 1) must be set.

    Why this exists as a named step: after a cold boot with no driver claiming the device,
    Command reads 0x0000 and **every BAR0 read returns 0xffffffff** — indistinguishable
    from a card that fell off the bus. Measured 2026-11-21 on a card whose 11 FF records
    had just burned successfully: `lspci` still listed it (enumeration cache), BAR0 was
    all-ones, and the burn was one step from being recorded as a dead card. Config space
    answered 10de:20c2 the whole time, which is the discriminator.

    Returns {"before", "after", "changed", "bar0_ok"}, or None when config space is
    unreachable (that shape is a link/power problem, not a decode problem).
    """
    cmd = command_reg(bdf)
    if cmd is None or cmd == 0xFFFF:
        return None
    card_answers_config(bdf)                    # never write config for a poisoned device
    before_cmd = cmd
    if not cmd & 0x0002:
        _probe(f"wake {bdf}: COMMAND {cmd:#06x} -> {cmd | 0x0002:#06x} (mem decode on)")
        fd = os.open(f"/sys/bus/pci/devices/{bdf}/config", os.O_RDWR)
        try:
            _pwrite(fd, struct.pack("<H", cmd | 0x0002), 0x04)
        finally:
            os.close(fd)
    after = command_reg(bdf) or 0
    word = bar0_first_word(bdf)
    return {"before": before_cmd, "after": after, "changed": after != before_cmd,
            "bar0_ok": word not in (None, 0xFFFFFFFF)}


def restore(bdf: str = DEFAULT_BDF, wait_s: float = 0.25,
            settle_s: float = 0.15) -> bool:
    """Wait for PCIe link after PEX_RST, reprogram BAR0, and enable mem decode.

    Returns False if the GPU never came back on PCIe config space within wait_s
    (normal on a failed glitch shot; caller should issue the next reset).
    """
    if settle_s > 0:
        time.sleep(min(settle_s, wait_s))
    start = _resource0_start(bdf)
    cfg_path = f"/sys/bus/pci/devices/{bdf}/config"
    deadline = time.monotonic() + max(0.0, wait_s - settle_s)
    _probe(f"restore {bdf}: open {cfg_path} O_RDWR")     # first write to the card's config
    try:
        fd = os.open(cfg_path, os.O_RDWR)
    except PermissionError as e:
        raise PermissionError(f"cannot open {cfg_path} (run with sudo)") from e
    except FileNotFoundError as e:
        raise FileNotFoundError(f"PCI device {bdf} not found ({cfg_path})") from e
    try:
        while True:
            vd = struct.unpack("<I", os.pread(fd, 4, 0x00))[0]
            if vd != 0xFFFFFFFF:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        bar0 = struct.unpack("<I", os.pread(fd, 4, 0x10))[0]
        # Gate right here, after config space answers and before we write a single byte
        # to it or map anything: a card that answers config while its link is flaky is
        # the exact case that took the host down.
        require_mapped(bdf)
        if (bar0 & 0xFFFFFFF0) != (start & 0xFFFFFFF0):
            os.pwrite(fd, struct.pack("<I", start & 0xFFFFFFFF), 0x10)
    finally:
        os.close(fd)
    # COMMAND/MSE 的写只有一处实现（ensure_memory_enable），restore 不自己摸偏移 4：
    # "谁来打开 memory decode"这件事有两个写点的话，就会有一个人在其中一个里漏掉
    # 为什么先闸门后写（见 ensure_memory_enable 的来由）。
    return ensure_memory_enable(bdf) is not None

class Bar0:
    """mmap of /sys/bus/pci/devices/<bdf>/resource0."""

    def __init__(self, bdf: str = DEFAULT_BDF, size: int = DEFAULT_SIZE):
        self.bdf = bdf
        self.path = f"/sys/bus/pci/devices/{bdf}/resource0"
        require_mapped(bdf)          # last gate before the mmap: see require_mapped()
        self.fd = os.open(self.path, os.O_RDWR | os.O_SYNC)
        try:
            self.mm = mmap.mmap(self.fd, size, mmap.MAP_SHARED,
                                mmap.PROT_READ | mmap.PROT_WRITE)
        except Exception:
            os.close(self.fd)
            raise
        # The card may die *while we hold this mapping*. One verdict is a handful of
        # reads over ~1 s, and the async deaths we measured land inside that window, so
        # a pre-mapping check alone leaves a several-hundred-ms hang window open.
        self.guard_reads = not os.environ.get("GA_SKIP_LINK_GATE")
        _probe(f"mmap {self.path} size={size} guard_reads={self.guard_reads}")
        self._first_access = True                      # the first MMIO is the one worth logging

    def _alive(self) -> None:
        """Config-space heartbeat in front of every MMIO access.

        A sysfs pread is answered by the root port and is therefore safe even when the
        card is mid-death; the MMIO that follows is not. Cost is tens of microseconds
        against a register read we take for a verdict, and it shrinks the window where a
        card can vanish underneath us from a whole verdict to one register.
        """
        if self.guard_reads:
            card_answers_config(self.bdf)

    def rd32(self, off: int) -> int:
        self._alive()
        if self._first_access:
            self._first_access = False
            _probe(f"first MMIO read {self.bdf} off={off:#x}")
        return struct.unpack_from("<I", self.mm, off)[0]

    def wr32(self, off: int, value: int) -> None:
        self._alive()
        if self._first_access:
            self._first_access = False
            _probe(f"first MMIO write {self.bdf} off={off:#x} value={value:#010x}")
        struct.pack_into("<I", self.mm, off, value & 0xFFFFFFFF)

    def close(self) -> None:
        self.mm.close()
        os.close(self.fd)

    def __enter__(self) -> "Bar0":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
