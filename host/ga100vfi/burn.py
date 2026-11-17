"""GA100 fuse macro: window status, row capture, burn-plan cross-check, arm & burn.

Verified on a CMP 170HX (10de:20c2): CMD_READ=1 loads FUSEDATA, CMD_PROGRAM=2 passes
through WRITE_PROGRAM (FUSECTRL=0xE00C0000) back to IDLE within ~1 ms, SENSE_CTRL=3
re-senses the control fuses while DSOV stays 0. Blank OTP rows read 0x00000000.
FUSECTRL going 0xBADFxxxx means the controller is poisoned: stop touching BAR0 and
cold-boot the machine.

Everything hardware-facing is a (rd, wr) callable so the logic is testable offline
(see host/tests/test_burn.py). CLI usage in README "烧写工具" section.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

FUSECTRL = 0x00820000
FUSEADDR = 0x00820004
FUSEDATA = 0x00820008          # read port (valid after CMD_READ)
FUSEWDATA = 0x0082000C         # write port (record source for CMD_PROGRAM)
FUSEDEBUG = 0x00820060
BOOT0 = 0x00000000
CRC_STATUS = 0x00820070
EFP_STATUS = 0x00820078         # OPT_ENABLE_FUSE_PROGRAM
DSOV_STATUS = 0x00820084        # OPT_DISABLE_SW_OVERRIDE
SBFE_SHADOW = 0x00821108        # OPT_SECURED_BURN_FEATURE_ENABLE
WDATA_WR_SECURE = 0x0082048C
PLM_FUSE = 0x008200F4

CMD_READ = 1
CMD_PROGRAM = 2
CMD_SENSE = 3
IDLE_CTRL = 0xE0040000
FF_ROW_MIN, FF_ROW_MAX = 222, 486
ROW_COUNT = 512
PROGRAM_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 0.5


def encode(chain: int, block: int, data: int) -> int:
    return (data & 0xFFFF) << 16 | (block & 0x3FF) << 6 | (chain & 0x1F)


def decode(record: int) -> dict:
    if (record >> 5) & 1:
        raise ValueError("not an FF record (IFF bit set): 0x%08x" % record)
    return {"chain": record & 0x1F,
            "block": (record >> 6) & 0x3FF,
            "data": (record >> 16) & 0xFFFF}


def replay(rows: dict) -> tuple:
    """rows: {row: value32} -> ({(chain, block): data}, {(chain, block): holder_row})."""
    blocks, holders = {}, {}
    for row in sorted(int(r) for r in rows):
        value = int(rows[row] if not isinstance(rows[row], str)
                    else int(rows[row], 0))
        if not FF_ROW_MIN <= row <= FF_ROW_MAX or value == 0:
            continue
        try:
            d = decode(value)
        except ValueError:
            continue  # IFF/unparsed rows are not FF records
        blocks[(d["chain"], d["block"])] = d["data"]
        holders[(d["chain"], d["block"])] = row
    return blocks, holders


def crosscheck(snapshot_rows: dict, plan: dict) -> list:
    """Cross-check a burn plan against an FF snapshot. Returns problem strings.

    Pure stdlib, offline: encoding recompute, IFF, clear-only rule, old values vs
    replay, target rows blank, plan uniqueness, override order, post-burn replay.
    For option-word analysis use `ffcalc verify` on top (recommended, not required).
    """
    rows = {int(k): int(v, 0) if isinstance(v, str) else int(v)
            for k, v in snapshot_rows.items()}
    problems = []
    burned, seen_cb = {}, {}
    for rec in plan["burn"]:
        row, old, new = int(rec["row"]), int(rec["old"], 16), int(rec["new"], 16)
        record = int(rec["record"], 16)
        tag = f"row {row} ({rec['record']})"
        try:
            d = decode(record)
            if d != {"chain": rec["chain"], "block": rec["block"], "data": new}:
                problems.append(f"{tag}: record decodes to {d}, plan says "
                                f"chain {rec['chain']} block {rec['block']} new {rec['new']}")
        except ValueError as e:
            problems.append(f"{tag}: {e}")
            continue
        if encode(rec["chain"], rec["block"], new) != record:
            problems.append(f"{tag}: re-encode mismatch")
        if new & ~old:
            problems.append(f"{tag}: clear-only violated (new & ~old = 0x%x)" % (new & ~old))
        if rows.get(row, 0) != 0:
            problems.append(f"{tag}: target row not blank in snapshot "
                            f"(reads 0x{rows[row]:08x})")
        cb = (rec["chain"], rec["block"])
        if cb in seen_cb:
            problems.append(f"{tag}: chain.block already used by row {seen_cb[cb]}")
        seen_cb[cb] = row
        burned[row] = record
    blocks, holders = replay(rows)
    for rec in plan["burn"]:
        cb = (rec["chain"], rec["block"])
        cur = blocks.get(cb)
        if cur != int(rec["old"], 16):
            problems.append(f"row {rec['row']}: old={rec['old']} but snapshot replay "
                            f"of chain.block {cb} gives "
                            + ("no record" if cur is None else hex(cur)))
        elif cb in holders and int(rec["row"]) <= holders[cb]:
            problems.append(f"row {rec['row']}: does not override existing record at "
                            f"row {holders[cb]} for chain.block {cb}")
    merged = dict(rows)
    merged.update(burned)
    after, _ = replay(merged)
    for rec in plan["burn"]:
        got = after.get((rec["chain"], rec["block"]))
        if got != int(rec["new"], 16):
            problems.append(f"post-burn replay of chain.block {(rec['chain'], rec['block'])} "
                            f"= {'none' if got is None else hex(got)}, expected {rec['new']}")
    return problems


class FuseError(RuntimeError):
    pass


class FuseMacro:
    """Fuse macro driver over an arbitrary BAR0 (rd32, wr32) pair."""

    def __init__(self, rd, wr, sleep=time.sleep, clock=time.monotonic):
        self.rd, self.wr, self.sleep, self.clock = rd, wr, sleep, clock

    @classmethod
    def for_bdf(cls, bdf: str) -> "FuseMacro":
        from .bar0 import Bar0, restore
        if not restore(bdf, wait_s=1.0, settle_s=0.2):
            raise FuseError(f"{bdf}: config space unreachable (0xffffffff)")
        bar = Bar0(bdf)
        return cls(bar.rd32, bar.wr32), bar

    @staticmethod
    def is_idle(ctrl: int) -> bool:
        return ((ctrl >> 16) & 31) == 4 and (ctrl & 3) == 0

    @property
    def poisoned(self) -> bool:
        return (self.rd(FUSECTRL) & 0xFFFF0000) == 0xBADF0000

    def _wait_idle(self, timeout: float) -> bool:
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            ctrl = self.rd(FUSECTRL)
            if self.is_idle(ctrl):
                return True
            if (ctrl & 0xFFFF0000) == 0xBADF0000:
                raise FuseError("fuse controller poisoned (FUSECTRL=0x%08x): "
                                "stop BAR0 reads, cold-boot required" % ctrl)
            self.sleep(0.001)
        return False

    def status(self) -> dict:
        return {"boot0": self.rd(BOOT0), "fusectrl": self.rd(FUSECTRL),
                "fusedebug": self.rd(FUSEDEBUG), "crc": self.rd(CRC_STATUS),
                "efp": self.rd(EFP_STATUS), "dsov": self.rd(DSOV_STATUS),
                "sbfe": self.rd(SBFE_SHADOW), "wdata_wr_secure": self.rd(WDATA_WR_SECURE),
                "plm_fuse": self.rd(PLM_FUSE)}

    def window_ok(self) -> bool:
        st = self.status()
        return (st["boot0"] not in (0, 0xFFFFFFFF)
                and st["crc"] == 0x55 and st["dsov"] == 0
                and self.is_idle(st["fusectrl"]) and st["fusedebug"] == 0)

    def read_row(self, row: int) -> int:
        if not self._wait_idle(READ_TIMEOUT_S):
            raise FuseError(f"controller not idle before reading row {row}")
        orig = self.rd(FUSEADDR)
        self.wr(FUSEADDR, row)
        if self.rd(FUSEADDR) != row:
            raise FuseError(f"FUSEADDR did not latch row {row}")
        self.wr(FUSECTRL, CMD_READ)
        if not self._wait_idle(READ_TIMEOUT_S):
            raise FuseError(f"CMD_READ timeout at row {row}")
        value = self.rd(FUSEDATA)
        self.wr(FUSEADDR, orig)
        return value

    def capture_rows(self, first: int = 0, last: int = ROW_COUNT - 1) -> dict:
        rows = {}
        for row in range(first, last + 1):
            rows[row] = self.read_row(row)
        return rows

    def sense(self) -> None:
        """SENSE_CTRL: re-sense control fuses. DSOV must survive; EFP/SBFE go to 1."""
        if not self._wait_idle(READ_TIMEOUT_S):
            raise FuseError("not idle before SENSE_CTRL")
        self.wr(FUSECTRL, CMD_SENSE)
        if not self._wait_idle(2.0):
            raise FuseError("SENSE_CTRL did not return to IDLE")
        if self.rd(DSOV_STATUS) != 0:
            raise FuseError("DSOV is not 0 after SENSE_CTRL: not a usable window")

    def arm(self) -> None:
        """Shadow SBFE=0 and open the write PLM, then assert the full armed state."""
        self.wr(SBFE_SHADOW, 0)
        self.wr(WDATA_WR_SECURE, 0)
        problems = self.armed_problems()
        if problems:
            raise FuseError("armed assertions failed: " + "; ".join(problems))

    def armed_problems(self) -> list:
        st = self.status()
        problems = []
        if st["fusectrl"] != IDLE_CTRL:
            problems.append(f"FUSECTRL=0x{st['fusectrl']:08x} != IDLE")
        if st["fusedebug"] != 0:
            problems.append(f"FUSEDEBUG=0x{st['fusedebug']:08x} != 0")
        if st["crc"] != 0x55:
            problems.append(f"CRC=0x{st['crc']:08x} != 0x55")
        if st["efp"] != 1:
            problems.append(f"EFP={st['efp']} != 1")
        if st["dsov"] != 0:
            problems.append(f"DSOV={st['dsov']} != 0")
        if st["sbfe"] != 0:
            problems.append(f"SBFE shadow={st['sbfe']} != 0")
        if st["wdata_wr_secure"] != 0:
            problems.append(f"WDATA_WR_SECURE={st['wdata_wr_secure']} != 0")
        if self.rd(FUSEWDATA) != 0:
            problems.append("FUSEWDATA write port not readable-as-zero")
        return problems

    def burn(self, row: int, record: int, blank_check: bool = True) -> int:
        """Program one record into a blank FF row; returns the verified readback."""
        problems = self.armed_problems()
        if problems:
            raise FuseError("armed assertions failed: " + "; ".join(problems))
        if blank_check and self.read_row(row) != 0:
            raise FuseError(f"target row {row} is not blank; refusing to burn")
        orig = self.rd(FUSEADDR)
        self.wr(FUSEADDR, row)
        if self.rd(FUSEADDR) != row:
            raise FuseError(f"FUSEADDR did not latch row {row}")
        self.wr(FUSEWDATA, record)
        self.wr(FUSECTRL, CMD_PROGRAM)
        if not self._wait_idle(PROGRAM_TIMEOUT_S):
            raise FuseError(f"PROGRAM at row {row} did not return to IDLE "
                            "(cold-boot; row may or may not have burned — re-read)")
        self.wr(FUSEADDR, orig)
        got = self.read_row(row)
        if got != record:
            raise FuseError(f"row {row} readback 0x{got:08x} != 0x{record:08x}")
        return got


def _load_rows(path: str) -> dict:
    """Rows from a rows.txt ('<row> <value>' per line) or a JSON dump with ff_rows."""
    if path.endswith(".json"):
        dump = json.load(open(path))
        return {int(k): v for k, v in dump["ff_rows"].items()}
    rows = {}
    for line in open(path):
        line = line.split("#", 1)[0].strip()
        if line:
            a, b = line.split()
            rows[int(a)] = int(b, 0)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ga100vfi.burn",
                                 description="GA100 fuse macro burn tool (see README)")
    ap.add_argument("--bdf", default="0000:01:00.0")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="print fuse-controller status (read-only)")
    p_cap = sub.add_parser("capture", help="capture 512 FF rows (CMD_READ, read-only)")
    p_cap.add_argument("-o", "--out", required=True, help="rows.txt or .json dump")

    p_cc = sub.add_parser("crosscheck",
                          help="offline plan cross-check + live blank-row re-read")
    p_cc.add_argument("snapshot", help="rows.txt or ffcalc dump json")
    p_cc.add_argument("plan", help="burn plan json")
    p_cc.add_argument("--no-live", action="store_true",
                      help="skip re-reading target rows live")

    p_burn = sub.add_parser("burn", help="sense, arm, burn plan rows, verify")
    p_burn.add_argument("snapshot", help="rows.txt or dump json (cross-check source)")
    p_burn.add_argument("plan", help="burn plan json")
    p_burn.add_argument("--rows", help="comma-separated row subset (e.g. test row first)")
    p_burn.add_argument("--yes", action="store_true", help="skip interactive confirm")

    args = ap.parse_args(argv)

    if args.cmd == "capture":
        macro, bar = FuseMacro.for_bdf(args.bdf)
        try:
            rows = macro.capture_rows()
        finally:
            bar.close()
        if args.out.endswith(".json"):
            json.dump({"ff_rows": {str(k): v for k, v in rows.items()},
                       "bdf": args.bdf, "source": "ga100vfi.burn"},
                      open(args.out, "w"), indent=1)
        else:
            with open(args.out, "w") as f:
                for row in sorted(rows):
                    f.write("%d 0x%08x\n" % (row, rows[row]))
        print(f"captured {len(rows)} rows -> {args.out}")
        return 0

    if args.cmd == "crosscheck":
        rows = _load_rows(args.snapshot)
        plan = json.load(open(args.plan))
        problems = crosscheck(rows, plan)
        if not args.no_live:
            macro, bar = FuseMacro.for_bdf(args.bdf)
            try:
                for rec in plan["burn"]:
                    live = macro.read_row(int(rec["row"]))
                    if live != 0:
                        problems.append(f"row {rec['row']}: live re-read is "
                                        f"0x{live:08x}, not blank")
            finally:
                bar.close()
        for p in problems:
            print("FAIL:", p)
        print("OK: plan cross-checks (%d records, %d problems)"
              % (len(plan["burn"]), len(problems)))
        return 1 if problems else 0

    if args.cmd == "status":
        macro, bar = FuseMacro.for_bdf(args.bdf)
        try:
            st = macro.status()
            for k, v in st.items():
                print(f"{k:18s} 0x{v:08x}")
            print("goldilocks window:", "OPEN" if macro.window_ok() else "closed")
        finally:
            bar.close()
        return 0

    if args.cmd == "burn":
        rows = _load_rows(args.snapshot)
        plan = json.load(open(args.plan))
        wanted = None
        if args.rows:
            wanted = {int(x) for x in args.rows.split(",")}
        records = [r for r in plan["burn"]
                   if wanted is None or int(r["row"]) in wanted]
        problems = crosscheck(rows, plan)
        for p in problems:
            print("FAIL:", p)
        if problems:
            print("cross-check failed — refusing to burn")
            return 1
        if not args.yes:
            input(f"burn {len(records)} record(s) irreversibly? Enter to continue: ")
        macro, bar = FuseMacro.for_bdf(args.bdf)
        try:
            if not macro.window_ok():
                print("no goldilocks window (status shown below); aborting")
                print(macro.status())
                return 2
            macro.sense()
            macro.arm()
            print("armed, burning %d record(s)..." % len(records))
            for rec in records:
                got = macro.burn(int(rec["row"]), int(rec["record"], 16))
                print(f"OK row {rec['row']:3d} 0x{got:08x} fields={rec.get('fields')}")
            st = macro.status()
            print("post: CRC=0x%x DSOV=%d FUSEDEBUG=0x%x"
                  % (st["crc"], st["dsov"], st["fusedebug"]))
            print("done — cold-boot required for the new FF chain to take effect")
        except FuseError as e:
            print("STOP:", e)
            return 2
        finally:
            bar.close()
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
