"""The five per-attempt checks, in the order the method prescribes.

1. glitch (issued elsewhere)
2. sleep 200 ms
3. read DSOV (0x00820084); continue unless it reads 0
4. read an FPF row (0x00824314); continue if it reads 0xffffffff
5. write DSOV = 1 (0x00820040), read back; continue unless it reads 1
6. dump the shadow fuse registers and compare with the baseline (logged only)
7. read NV_FUSE_OPT_PRIV_LEVEL_MASK (0x008200fc); Goldilocks reads 0xffffffff
   (both PLMs wide open, so the shadow option words are writable)
8. read NV_FUSE_CRC_STATUS (0x00820070); continue unless it reads 0x55 (both
   fuse hash regions H0/H1 parsed cleanly). 0x00 == block not CRC-validated,
   a "problematic" Goldilocks that otherwise looks correct.

Before any of that, one question to the fuse controller (安全红线 1). It has four answers,
not two: IDLE may proceed, all-FF is "the card left PCIe" (a `lost`, keep the ladder
running), 0xBADFxxxx is a trip, and anything else -- the 0x00000000 measured at vd=309, a
transient state caught mid-command -- is a state we cannot name, so we neither judge nor
fire again. See fusectrl_state() and docs/UNLOCK_AND_POISON.md §己.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DSOV = 0x00820084
FPF_ROW = 0x00824314
DSOV_SET = 0x00820040
PRIV_MASK = 0x008200fc
CRC_STATUS = 0x00820070
CRC_GOOD = 0x55

# Shadow registers that move when the fuse block is misread.
SHADOW = {
    "STATUS_OPT_GPC": 0x00820C1C,
    "NV_FUSE_OPT_GPC_DEFECTIVE": 0x008205C4,
    "STATUS_OPT_FBP": 0x00820D38,
    "FBP_DEFECTIVE": 0x008205CC,
    "FBPA_DEFECTIVE": 0x008205D0,
}

UNREADABLE = 0xFFFFFFFF

FUSECTRL = 0x00820000          # same offset burn.py uses
POISON_TAG = 0xBADF0000        # high half of FUSECTRL when the controller has tripped
FUSE_IDLE = 0xE0040000         # what a clean, ready-to-serve controller reads

# 三种以上状态，只认两种：IDLE 和 0xBADF。2026-11-20 实测到第三种 ——
# `FUSECTRL=0x00000000`（vd=309 那一发），它既不是 IDLE 也不是 poison，
# 而在同一电压上下一次尝试立刻变成 0xBADF510E 并把卡打掉 PCIe。
# 所以判据补成：**非 IDLE、非 poison、也不是"卡不在"(全 FF) 的一律不判决、立即停火**。
STATE_IDLE = "idle"
STATE_POISONED = "poisoned"
STATE_UNREACHABLE = "unreachable"   # 全 FF：卡不在总线上，这是 lost，不是控制器故障
STATE_UNKNOWN = "unknown"


class FuseControllerStop(RuntimeError):
    """BAR0 must not be touched again on this card until it is power-cycled."""


class Poisoned(FuseControllerStop):
    """The fuse controller tripped: FUSECTRL reads 0xBADFxxxx.

    安全红线 1 (README §5, from the real incident in logs/02): reading BAR0 while the
    controller is poisoned hangs the PCIe upstream and resets the whole machine, which is
    what we have kept mislabelling as "the glitch killed the card". There is no retry for
    this state -- so callers must stop touching BAR0 entirely, and must not "read once
    more to be sure".
    """


class ControllerUnknown(FuseControllerStop):
    """FUSECTRL is neither IDLE nor 0xBADFxxxx (measured: 0x00000000 at vd=309).

    A well-formed-but-unrecognised state is not something to interpret: the one time it
    was seen, the next shot at the same voltage tripped the controller to 0xBADF510E and
    took the card off PCIe. Treat it like a stop, but do not call it a poison -- the two
    need different follow-ups.
    """


def fusectrl_state(ctrl: int) -> str:
    """Classify a FUSECTRL reading. Order matters: all-FF also has state bits 31.

    ALL-FF is "the card stopped answering", which is an ordinary `lost` and must keep the
    ladder running; only the 0xBADF tag means the controller itself has tripped.
    """
    if ctrl == UNREADABLE:
        return STATE_UNREACHABLE
    if (ctrl & 0xFFFF0000) == POISON_TAG:
        return STATE_POISONED
    if ctrl == FUSE_IDLE:
        return STATE_IDLE
    return STATE_UNKNOWN


def poisoned(bar) -> bool:
    """The cheapest question that decides whether BAR0 may be touched at all.

    One read of one register. classify() below reads six and writes one, which is exactly
    the sequence that must not happen against a tripped controller.
    """
    return fusectrl_state(bar.rd32(FUSECTRL)) == STATE_POISONED


def require_usable_ctrl(ctrl: int) -> int:
    """Judge a FUSECTRL value that was already read (the agent records the number)."""
    state = fusectrl_state(ctrl)
    if state == STATE_POISONED:
        raise Poisoned(
            f"FUSECTRL reads {ctrl:#010x}: the fuse controller is poisoned. Every further "
            "BAR0 read risks hanging the PCIe upstream (logs/02). Reboot the judging host "
            "first -- POST pulls PERST# and the controller goes through POR; only if the "
            "card is still unreachable afterwards remove 12V and 3V3 for >=30s."
        )
    if state == STATE_UNKNOWN:
        raise ControllerUnknown(
            f"FUSECTRL reads {ctrl:#010x}: neither IDLE ({FUSE_IDLE:#010x}) nor the "
            "0xBADFxxxx trip tag. This is the third state measured on 2026-11-20 (vd=309): "
            "the same voltage tripped the controller on the next shot and dropped the card "
            "off PCIe. Do not interpret it and do not fire again -- stop the campaign, "
            "reboot the judging host (POST pulls PERST#) and re-read FUSECTRL before any "
            "further BAR0 access."
        )
    return ctrl


def require_usable(bar) -> int:
    """Ask once, then the rest of the module may proceed. Returns the raw FUSECTRL.

    The recovery order in the message is the measured one, not the documented one: a warm
    reboot goes through POST, and POST pulls PERST# once, which takes the card *and* its
    fuse controller through POR. Pulling 12V/3V3 is the fallback, not the first move.
    """
    return require_usable_ctrl(bar.rd32(FUSECTRL))


# 旧名字（0.2.x 树里到处在用），语义已扩到"非 IDLE 也停"。
require_unpoisoned = require_usable


@dataclass
class Verdict:
    dsov: int
    fpf_row: int
    dsov_writeback: int
    priv_mask: int
    crc_status: int
    shadow: dict[str, int] = field(default_factory=dict)
    shadow_diff: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def checks(self) -> dict[str, bool]:
        return {
            "dsov_zero": self.dsov == 0,
            "fpf_readable": self.fpf_row != UNREADABLE,
            "dsov_settable": self.dsov_writeback == 1,
            "priv_mask_open": self.priv_mask == 0xFFFFFFFF,
            "crc_valid": self.crc_status == CRC_GOOD,
        }

    @property
    def passed(self) -> bool:
        return all(self.checks.values())

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "checks": self.checks,
            "dsov": self.dsov,
            "fpf_row": self.fpf_row,
            "dsov_writeback": self.dsov_writeback,
            "priv_mask": self.priv_mask,
            "crc_status": self.crc_status,
            "shadow": self.shadow,
            "shadow_diff": self.shadow_diff,
        }


def snapshot(bar) -> dict[str, int]:
    require_usable(bar)              # six fuse-block reads must never start from a trip
    return {name: bar.rd32(off) for name, off in SHADOW.items()}


def attempt_outcome(v: Verdict) -> str:
    """Common outcome rules for the serial campaign and HTTP verdict agent."""
    if v.dsov == UNREADABLE:
        return "lost"
    if v.passed:
        return "goldilocks"
    if v.dsov != 0:
        return "under"
    return "hit" if v.crc_status == CRC_GOOD else "over"


def classify(bar, baseline: dict[str, int] | None = None) -> Verdict:
    """Run checks 3-8. Leaves DSOV set to 1 if check 5 succeeds.

    Raises FuseControllerStop (Poisoned / ControllerUnknown) rather than proceed: this
    function performs six BAR0 reads and a DSOV_SET write, and against a controller that
    is not in its documented IDLE state that sequence takes the machine down.
    """
    require_usable(bar)
    dsov = bar.rd32(DSOV)
    fpf = bar.rd32(FPF_ROW)
    bar.wr32(DSOV_SET, 1)
    writeback = bar.rd32(DSOV_SET)
    priv = bar.rd32(PRIV_MASK)
    crc = bar.rd32(CRC_STATUS)
    shadow = snapshot(bar)
    diff = {}
    if baseline:
        diff = {k: (baseline.get(k), v) for k, v in shadow.items() if baseline.get(k) != v}
    return Verdict(dsov, fpf, writeback, priv, crc, shadow, diff)
