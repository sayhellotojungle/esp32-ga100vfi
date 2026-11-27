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
fire again. See fusectrl_state() and docs/UNLOCK_AND_POISON.md §四.
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

    One read of one register. classify() below sweeps the whole fuse block and — only on a
    candidate hit — writes DSOV_SET, and that is exactly the sequence that must not happen
    against a tripped controller. Since 2026-11-21 the write is off the table unless DSOV
    actually read 0, so a routine `under` verdict costs reads only, no writes.
    """
    return fusectrl_state(bar.rd32(FUSECTRL)) == STATE_POISONED


def require_usable_ctrl(ctrl: int) -> int:
    """Judge a FUSECTRL value that was already read (the agent records the number)."""
    state = fusectrl_state(ctrl)
    if state == STATE_POISONED:
        raise Poisoned(
            f"FUSECTRL reads {ctrl:#010x}: the fuse controller is poisoned. Every further "
            "BAR0 read risks hanging the PCIe upstream (logs/02). Recovery starts with the "
            "glitch board itself: send `j` (hold PEX_RST low), wait >=2s, send `J`, then "
            "re-read FUSECTRL once -- measured 2026-11-21, a poison produced by a real "
            "shot (wfail=0) came back to IDLE that way, no power cycle and no host reboot. "
            "Only if that fails: reboot the judging host (POST pulls PERST#), then remove "
            "12V and 3V3 for >=30s."
        )
    if state == STATE_UNKNOWN:
        raise ControllerUnknown(
            f"FUSECTRL reads {ctrl:#010x}: neither IDLE ({FUSE_IDLE:#010x}) nor the "
            "0xBADFxxxx trip tag. This is the third state measured on 2026-11-20 (vd=309): "
            "the same voltage tripped the controller on the next shot and dropped the card "
            "off PCIe. Do not interpret it and do not fire again -- pulse PEX_RST from the "
            "board (`j` -> >=2s -> `J`, measured to clear this state), re-read FUSECTRL "
            "before any further BAR0 access, and only then continue."
        )
    return ctrl


def require_usable(bar) -> int:
    """Ask once, then the rest of the module may proceed. Returns the raw FUSECTRL.

    The recovery order in the message is the measured one, twice over: the cheapest PERST#
    we control is the glitch board's own `j`/`J` pair (2026-11-21: a real-shot poison came
    back to IDLE from a 2 s hold, no power cycle and no host reboot), and a warm reboot is a
    second PERST# -- POST pulls the same line, taking the card *and* its fuse controller
    through POR. Pulling 12V/3V3 is the fallback, not the first move.
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
    # 只有真的写过才叫"探过"。非命中的一发我们根本不发那一笔写（见 classify 的 docstring），
    # 所以这里必须是 False —— 台账里 `dsov_writeback=0` 与 `writeback_probed=False` 是两件事：
    # 前者是"写了没生效"，后者是"我们没写，因为那一发的 DSOV 就不是 0，写了也不会变判决"。
    writeback_probed: bool = True

    @property
    def checks(self) -> dict[str, bool]:
        return {
            "dsov_zero": self.dsov == 0,
            "fpf_readable": self.fpf_row != UNREADABLE,
            "dsov_settable": self.writeback_probed and self.dsov_writeback == 1,
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
            "writeback_probed": self.writeback_probed,
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


def classify(bar, baseline: dict[str, int] | None = None, *,
             probe_writeback: bool = False) -> Verdict:
    """Run checks 3-8. Leaves DSOV set to 1 if check 5 succeeds.

    Raises FuseControllerStop (Poisoned / ControllerUnknown) rather than proceed: this
    function performs six BAR0 reads and a DSOV_SET write, and against a controller that
    is not in its documented IDLE state that sequence takes the machine down.

    那一笔 DSOV_SET 写默认**不发**。2026-11-21 判决机连着两次整机死机（不是掉卡：网络、USB、
    键盘一起没），两次都发生在打完一发之后的判决路上；而"对已经半死的卡写 BAR0 会把整机挂掉"
    本来就是这个仓库记在 logs/02 里的失效模式。写回试验的唯一用途是证明 shadow 可写，而那只在
    `dsov == 0` 的候选命中上才有意义 —— `attempt_outcome()` 在 `dsov != 0` 时无论写回结果如何
    都判 `under`，所以其它发数上这一笔写既不改判决、又白担一次挂整机的风险。
    需要旧行为（每发都探）时显式传 `probe_writeback=True`；`dsov == 0` 时无论如何都会探，
    因为 hit 必须带着可写证据才算数。
    """
    require_usable(bar)
    dsov = bar.rd32(DSOV)
    fpf = bar.rd32(FPF_ROW)
    writeback, probed = 0, False
    if dsov == 0 or probe_writeback:
        bar.wr32(DSOV_SET, 1)
        writeback, probed = bar.rd32(DSOV_SET), True
    priv = bar.rd32(PRIV_MASK)
    crc = bar.rd32(CRC_STATUS)
    shadow = snapshot(bar)
    diff = {}
    if baseline:
        diff = {k: (baseline.get(k), v) for k, v in shadow.items() if baseline.get(k) != v}
    return Verdict(dsov, fpf, writeback, priv, crc, shadow, diff, writeback_probed=probed)
