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


def crosscheck(snapshot_rows: dict, plan: dict, resumed=()) -> list:
    """Cross-check a burn plan against an FF snapshot. Returns problem strings.

    Pure stdlib, offline: encoding recompute, IFF, clear-only rule, old values vs
    replay, target rows blank, plan uniqueness, override order, post-burn replay.
    For option-word analysis use `ffcalc verify` on top (recommended, not required).

    `resumed` lists the rows that **already hold this plan's own record** — an
    interrupted run that stopped between two records. FF is append-only with
    last-writer-wins, so those rows are already at the plan's end state, and the three
    checks that describe the state *before* the burn ("target row not blank", "old must
    equal the replay", "must override the holder") are false statements about them.
    Without this, burning the first row on its own makes the pre-burn snapshot contradict
    the plan, and the tool refuses to finish a burn it is exactly one record into —
    measured on the bench 2026-11-21 (row 301 burned first, rows 302..311 to go).
    """
    rows = {int(k): int(v, 0) if isinstance(v, str) else int(v)
            for k, v in snapshot_rows.items()}
    resumed = {int(r) for r in resumed}
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
        if row in resumed:
            # 声称"已落地"就必须真落地：读回不是本条记录的行不配走续烧通道（burn_plan 传进来
            # 的就是现场读回，所以这里是在防"把别人的行当自己人"）。
            if rows.get(row, 0) != record:
                problems.append(f"{tag}: listed as resumed but the snapshot reads "
                                f"0x{rows.get(row, 0):08x}")
        elif rows.get(row, 0) != 0:
            problems.append(f"{tag}: target row not blank in snapshot "
                            f"(reads 0x{rows[row]:08x})")
        cb = (rec["chain"], rec["block"])
        if cb in seen_cb:
            problems.append(f"{tag}: chain.block already used by row {seen_cb[cb]}")
        seen_cb[cb] = row
        burned[row] = record
    blocks, holders = replay(rows)
    for rec in plan["burn"]:
        row = int(rec["row"])
        if row in resumed:
            continue                      # old/holder checks describe the pre-burn state
        cb = (rec["chain"], rec["block"])
        cur = blocks.get(cb)
        if cur != int(rec["old"], 16):
            problems.append(f"row {row}: old={rec['old']} but snapshot replay "
                            f"of chain.block {cb} gives "
                            + ("no record" if cur is None else hex(cur)))
        elif cb in holders and row <= holders[cb]:
            problems.append(f"row {row}: does not override existing record at "
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
        self._wrote_wdata = False

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

    # 快照的**可信区间**：0..FF_ROW_MAX。IFF 区（487..511）以前也照抓照存，但 2026-11-21 与另一条
    # 独立采集路径（内核 ffprobe 的 CMD_READ pass）逐行对比时发现：FF 区 0/265 差异，而 IFF 区
    # 20/25 行对不上，且我们这边读回的是满值域的高熵数据、两边行号怎么错位都对不齐。也就是说
    # **我们这条路径读 IFF 读回来的是不可信的数据**，而不是"这张卡的 IFF 长这样"。
    # 为什么要把它写死在默认值里而不是留个注释：`replay()` 本来就只看 FF 区，所以这份数据错了不
    # 会当场出事 —— 它会被后来的人当成事实引用（我们差点拿它去论证"两张卡不是同一块 die"）。
    # 读不到就干脆别写进快照，与 ffcalc 的"行读不全 ⇒ 拒绝出计划"是同一条纪律。
    def capture_rows(self, first: int = 0, last: int = FF_ROW_MAX) -> dict:
        """逐行 CMD_READ。默认只覆盖到 FF 区末尾（见上面的可信区间注释）。"""
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
        self._wrote_wdata = False            # 从这一刻起，写口的非零值只可能是我们自己留的
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
        if self.rd(FUSEWDATA) != 0 and not self._wrote_wdata:
            # 只在**第一条记录之前**要求写口读回 0。它是写口，不是状态寄存器：我们给上一条
            # 记录写进去的值就停在那儿（`arm()` 之后我们就是唯一写过它的人），把它当成
            # "写口不干净"会让任何一次多于一条的烧写自己拒绝自己 —— 断点续烧（先试烧一行
            # 再烧其余）走的正是这条路。
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
        self._wrote_wdata = True        # 之后写口读回非零是我们自己留的，不算脏（见 armed_problems）
        self.wr(FUSECTRL, CMD_PROGRAM)
        if not self._wait_idle(PROGRAM_TIMEOUT_S):
            raise FuseError(f"PROGRAM at row {row} did not return to IDLE "
                            "(cold-boot; row may or may not have burned — re-read)")
        self.wr(FUSEADDR, orig)
        got = self.read_row(row)
        if got != record:
            raise FuseError(f"row {row} readback 0x{got:08x} != 0x{record:08x}")
        return got


def diff_snapshots(before: dict, after: dict, plan: dict = None,
                   first: int = FF_ROW_MIN, last: int = FF_ROW_MAX) -> dict:
    """Compare two FF snapshots taken around a burn; report whether the delta is *exactly*
    the plan.

    "烧成功了" 与 "只烧了该烧的行" 是两件事：前者只看回读的那几行，后者要求**其余每一行都没动**。
    这一条 2026-11-21 是手工做的（两次独立重读 512 行逐行对），所以它必须是个函数：靠人当场
    数差异，数错一次就是一张已经烧进去 11 行的卡。

    只比较 FF 区（`first..last`，默认 222..486）：regular 区被驱动/devinit 动过不算熔丝，
    IFF 区（487..511）我们这条路径读不准（见 `capture_rows` 的注释），拿它做判据就是拿噪声
    当证据。
    """
    def norm(d):
        return {int(k): int(v, 0) if isinstance(v, str) else int(v) for k, v in d.items()}

    a, b = norm(before), norm(after)
    keys = set(a) | set(b)
    changed = sorted(r for r in keys if first <= r <= last and a.get(r, 0) != b.get(r, 0))
    out = {"changed": changed, "expected": [], "unexpected": [], "missing": [],
           "mismatch": []}
    if plan is None:
        return out
    want = {int(r["row"]): int(r["record"], 16) for r in plan["burn"]}
    out["expected"] = sorted(r for r in changed if r in want and b.get(r) == want[r])
    out["unexpected"] = sorted(r for r in changed if r not in want)
    out["mismatch"] = sorted(r for r in changed if r in want and b.get(r) != want[r])
    # 计划里"本来就该是这个值"的行（续烧）不算 missing：它们没变化正是因为已经落地。
    out["missing"] = sorted(r for r in want
                            if r not in changed and b.get(r, 0) != want[r]
                            and a.get(r, 0) != want[r])
    return out


def burn_plan(macro, plan: dict, snapshot_rows: dict, rows=None, log=print) -> int:
    """Run the whole burn procedure inside an open window. Returns a process exit code.

    顺序就是 2026-11-21 手工走的那一遍，区别是每一步现在都会自己拒绝：

      1. **先现场回读计划涉及的每一行**（顺带证明 BAR0 真的可读），把"已是本条记录"的行认成
         续烧、把"是别的值"的行当成事故直接拒绝 —— 往一个非零非目标值的行里 PROGRAM 是唯一
         一种救不回来的操作；
      2. 离线 cross-check（含续烧行的豁免，见 `crosscheck()`）；
      3. 窗口判据 → `CMD_SENSE`（`DSOV` 必须仍为 0）→ `arm` + 七项断言；
      4. 逐条：写前再读一次、`CMD_PROGRAM`、等 IDLE、回读必须等于记录值；
      5. 收尾打印 `CRC`/`FUSEDEBUG`/`DSOV`，让日志里留下"最后一发之后控制器没受伤"。
    """
    wanted = None if not rows else {int(x) for x in rows}
    all_rows = {int(r["row"]): int(r["record"], 16) for r in plan["burn"]}
    if wanted:
        unknown = wanted - set(all_rows)
        if unknown:
            log("REFUSED: rows %s not in plan" % sorted(unknown))
            return 1
    records = [r for r in plan["burn"] if wanted is None or int(r["row"]) in wanted]

    live = {row: macro.read_row(row) for row in sorted(all_rows)}
    resumed = sorted(row for row, rec in all_rows.items() if live[row] == rec)
    foreign = sorted((row, val) for row, val in live.items()
                     if val not in (0, all_rows[row]))
    log("现场回读 %d 行：%d 行已是本计划记录（跳过），%d 行空白"
        % (len(live), len(resumed), len(live) - len(resumed)))
    if foreign:
        for row, val in foreign:
            log("REFUSED: row %d 读回 0x%08x，既不是空白也不是本计划记录 —— "
                "这块 die 的目标行被人动过，先查清再烧" % (row, val))
        return 1

    problems = crosscheck(snapshot_rows, plan, resumed)
    for p in problems:
        log("FAIL: " + p)
    if problems:
        log("cross-check failed — refusing to burn")
        return 1

    if not macro.window_ok():
        log("no goldilocks window, aborting: %s" % (macro.status(),))
        return 2
    macro.sense()
    macro.arm()
    log("armed; burning %d record(s)..." % len(records))
    for rec in records:
        row = int(rec["row"])
        if row in resumed:
            log("skip row %3d 已是 0x%08x（续烧）" % (row, live[row]))
            continue
        got = macro.burn(row, int(rec["record"], 16))
        log("OK row %3d 0x%08x fields=%s" % (row, got, rec.get("fields")))
    st = macro.status()
    log("post: CRC=0x%x DSOV=%d FUSEDEBUG=0x%x FUSECTRL=0x%08x"
        % (st["crc"], st["dsov"], st["fusedebug"], st["fusectrl"]))
    log("done — cold-boot required for the new FF chain to take effect")
    return 0


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

    # 两个纯离线/纯配置空间的子命令。它们存在的理由都是"这件事以前是人肉做的"：
    # `diffrows` 是烧完之后逐行对两次快照（今天手工做的最后一步）；`wake` 是
    # "BAR0 读回全 0xff" 的第一处置 —— 冷启动后没有驱动 claim 这张卡时 Command=0x0000，
    # 每个 BAR0 读都是 0xffffffff，与掉卡长得一模一样。
    p_diff = sub.add_parser("diffrows",
                            help="offline: two FF snapshots vs the plan (only planned rows?)")
    p_diff.add_argument("before", help="snapshot before the burn (rows.txt or dump json)")
    p_diff.add_argument("after", help="snapshot after the burn / after cold boot")
    p_diff.add_argument("--plan", help="burn plan json; without it only the delta is shown")

    sub.add_parser("wake", help="set Memory Space Enable so BAR0 stops reading all-ones")

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

    if args.cmd == "wake":
        from .bar0 import ensure_memory_enable
        r = ensure_memory_enable(args.bdf)
        if r is None:
            print(f"{args.bdf}: 配置空间读不到（全 0xff 或节点不存在）—— 这是掉卡/没电，"
                  "不是 memory enable 没开；先按 bench/README「卡不见了」那节分形状")
            return 3
        print(f"{args.bdf}: Command {r['before']:#06x} -> {r['after']:#06x}"
              + ("（原本已开）" if not r["changed"] else "（本次打开 memory decode）")
              + f"；BAR0 现在{'可读' if r['bar0_ok'] else '仍读回 0xffffffff'}")
        return 0 if r["after"] & 0x2 else 1

    if args.cmd == "diffrows":
        before, after = _load_rows(args.before), _load_rows(args.after)
        plan = json.load(open(args.plan)) if args.plan else None
        d = diff_snapshots(before, after, plan)
        print("变化 %d 行：%s" % (len(d["changed"]), d["changed"] or "无"))
        if plan:
            print("   与计划记录相符：%s" % (d["expected"] or "无"))
            print("   计划内但没落地：%s" % (d["missing"] or "无"))
            print("   计划外的改动：%s" % (d["unexpected"] or "无"))
            print("   变化了却不等于计划记录：%s" % (d["mismatch"] or "无"))
            bad = d["missing"] + d["unexpected"] + d["mismatch"]
            print("结论：只有计划内的行按记录改变" if not bad
                  else "结论：差异与计划不一致 → 停下来查，别再烧")
            return 1 if bad else 0
        return 0

    if args.cmd == "burn":
        rows = _load_rows(args.snapshot)
        plan = json.load(open(args.plan))
        if not args.yes:
            n = len(plan["burn"]) if not args.rows else len(
                [r for r in plan["burn"] if int(r["row"]) in
                 {int(x) for x in args.rows.split(",")}])
            input(f"burn {n} record(s) irreversibly? Enter to continue: ")
        macro, bar = FuseMacro.for_bdf(args.bdf)
        try:
            return burn_plan(macro, plan, rows,
                             rows=args.rows.split(",") if args.rows else None)
        except FuseError as e:
            print("STOP:", e)
            return 2
        finally:
            bar.close()

    return 2


if __name__ == "__main__":
    sys.exit(main())
