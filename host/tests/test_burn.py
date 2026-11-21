"""Offline unit tests for ga100vfi.burn (fake fuse controller, no hardware)."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ga100vfi.burn import (  # noqa: E402
    CMD_PROGRAM, CMD_READ, CMD_SENSE, FUSEADDR, FUSECTRL, FUSEDATA, FUSEDEBUG,
    FUSEWDATA, CRC_STATUS, DSOV_STATUS, EFP_STATUS, SBFE_SHADOW,
    WDATA_WR_SECURE, PLM_FUSE, IDLE_CTRL,
    FuseError, FuseMacro, burn_plan, crosscheck, decode, diff_snapshots, encode, replay,
)


def record(chain, block, data):
    return encode(chain, block, data)


class FakeController:
    def __init__(self, rows=None, dsov=0, efp=1, sbfe=1, crc=0x55, poison_on=None):
        self.rows = dict(rows or {})
        self.reg = {0x0: 0x170000A1,  # PMC_BOOT_0
                    FUSECTRL: IDLE_CTRL, FUSEADDR: 0, FUSEDATA: 0,
                    FUSEDEBUG: 0, CRC_STATUS: crc, DSOV_STATUS: dsov,
                    EFP_STATUS: efp, SBFE_SHADOW: sbfe,
                    WDATA_WR_SECURE: 1, PLM_FUSE: 0xFFFFFFFF}
        self.programs = []
        self.senses = 0
        self.poison_on = poison_on  # row number that triggers poison on PROGRAM
        self.busy_ticks = 0
        self.now = 0.0

    def rd(self, off):
        if off == FUSECTRL and (self.reg[FUSECTRL] & 0x1F) and self.busy_ticks == 0:
            if self.reg.get("_pending"):
                self.reg[FUSECTRL] = IDLE_CTRL
                self.busy_ticks = 0
        return self.reg.get(off, 0)

    def wr(self, off, value):
        if off == FUSECTRL:
            cmd = value & 0x1F
            if cmd == CMD_READ:
                self.reg[FUSEDATA] = self.rows.get(self.reg[FUSEADDR], 0)
                self.reg[FUSECTRL] = IDLE_CTRL
            elif cmd == CMD_SENSE:
                self.senses += 1
                self.reg[SBFE_SHADOW] = 1
                self.reg[EFP_STATUS] = 1
                self.reg[FUSECTRL] = IDLE_CTRL
            elif cmd == CMD_PROGRAM:
                row = self.reg[FUSEADDR]
                if self.poison_on == row:
                    self.reg[FUSECTRL] = 0xBADF510E
                    return
                data = self.reg[FUSEWDATA]
                self.programs.append((row, data))
                self.rows[row] = data
                self.reg[FUSECTRL] = IDLE_CTRL
            else:
                self.reg[FUSECTRL] = IDLE_CTRL
        else:
            self.reg[off] = value

    def clock(self):
        return self.now

    def sleep(self, dt):
        self.now += dt


PLAN = {
    "burn": [
        {"row": 304, "record": "0x80070141", "chain": 1, "block": 5,
         "old": "0x9867", "new": "0x8007", "fields": ["fbio"]},
        {"row": 315, "record": "0x420004c1", "chain": 1, "block": 19,
         "old": "0x4290", "new": "0x4200", "fields": ["fbp"]},
    ]
}
SNAP = {279: record(1, 5, 0x9867), 290: record(1, 19, 0x4290),
        222: 0xC0006210, 488: 0x53557C3D}


class EncodeTests(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(decode(record(1, 19, 0x4200)),
                         {"chain": 1, "block": 19, "data": 0x4200})
        self.assertEqual(record(1, 19, 0x4200), 0x420004C1)

    def test_iff_rejected(self):
        with self.assertRaises(ValueError):
            decode(0x53557C3D)

    def test_replay_last_wins_and_holders(self):
        blocks, holders = replay({279: record(1, 5, 0x9867),
                                  304: record(1, 5, 0x8007)})
        self.assertEqual(blocks[(1, 5)], 0x8007)
        self.assertEqual(holders[(1, 5)], 304)


class CrosscheckTests(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(crosscheck(SNAP, PLAN), [])

    def test_clear_only_violation(self):
        plan = {"burn": [dict(PLAN["burn"][1], old="0x4200", new="0x4210",
                              record="0x421004C1")]}
        probs = crosscheck(SNAP, plan)
        self.assertTrue(any("clear-only" in p for p in probs))

    def test_old_must_match_replay(self):
        plan = {"burn": [dict(PLAN["burn"][0], old="0x9866")]}
        probs = crosscheck(SNAP, plan)
        self.assertTrue(any("old=" in p and "replay" in p for p in probs))

    def test_target_must_be_blank(self):
        snap = {**SNAP, 304: 0x1234}
        probs = crosscheck(snap, PLAN)
        self.assertTrue(any("not blank" in p for p in probs))

    def test_override_order(self):
        plan = {"burn": [dict(PLAN["burn"][0], row=278)]}
        probs = crosscheck(SNAP, plan)
        self.assertTrue(any("override existing" in p for p in probs))

    def test_duplicate_chain_block(self):
        plan = {"burn": [PLAN["burn"][0],
                         dict(PLAN["burn"][0], row=305, record="0x80070141")]}
        probs = crosscheck(SNAP | {305: 0}, plan)
        self.assertTrue(any("already used" in p for p in probs))


class MacroTests(unittest.TestCase):
    def setUp(self):
        self.dev = FakeController(rows=dict(SNAP))
        self.m = FuseMacro(self.dev.rd, self.dev.wr, sleep=self.dev.sleep,
                           clock=self.dev.clock)

    def test_capture_rows(self):
        rows = self.m.capture_rows(222, 224)
        self.assertEqual(rows[222], 0xC0006210)
        self.assertEqual(rows[223], 0)

    def test_read_row_restores_addr_and_loads_data(self):
        self.dev.reg[FUSEADDR] = 0x1FB
        self.assertEqual(self.m.read_row(279), record(1, 5, 0x9867))
        self.assertEqual(self.dev.reg[FUSEADDR], 0x1FB)

    def test_sense_requires_dsov_zero(self):
        self.dev.reg[DSOV_STATUS] = 1
        with self.assertRaises(FuseError):
            self.m.sense()

    def test_arm_and_burn_flow(self):
        self.m.sense()
        self.m.arm()
        self.assertEqual(self.dev.senses, 1)
        got = self.m.burn(315, 0x420004C1)
        self.assertEqual(got, 0x420004C1)
        self.assertEqual(self.dev.programs, [(315, 0x420004C1)])

    def test_burn_refuses_nonblank_row(self):
        self.m.sense()
        self.m.arm()
        with self.assertRaises(FuseError):
            self.m.burn(290, 0x420004C1)   # row 290 already holds a record
        self.assertEqual(self.dev.programs, [])

    def test_arm_detects_missing_efp(self):
        self.dev.reg[EFP_STATUS] = 0
        self.m.sense()
        self.dev.reg[EFP_STATUS] = 0
        with self.assertRaises(FuseError) as ctx:
            self.m.arm()
        self.assertIn("EFP", str(ctx.exception))

    def test_poison_detected_on_program(self):
        dev = FakeController(rows=dict(SNAP), poison_on=315)
        m = FuseMacro(dev.rd, dev.wr, sleep=dev.sleep, clock=dev.clock)
        m.sense()
        m.arm()
        with self.assertRaises(FuseError) as ctx:
            m.burn(315, 0x420004C1)
        self.assertIn("poisoned", str(ctx.exception))

    def test_busy_timeout_raises(self):
        self.dev.reg[FUSECTRL] = 0xE0080001  # non-idle, never resolves
        with self.assertRaises(FuseError):
            self.m.read_row(279)

    def test_window_ok(self):
        self.dev.reg[DSOV_STATUS] = 0
        self.assertTrue(self.m.window_ok())
        self.dev.reg[CRC_STATUS] = 0x00
        self.assertFalse(self.m.window_ok())


class ResumeTests(unittest.TestCase):
    """断点续烧：烧完第一条之后，**烧前那份快照**会让计划自相矛盾。

    FF 是追加 + 后写覆盖，所以"目标行必须空白"与"old 必须等于回放值"描述的都是烧之前的状态；
    第一条落地后它们就成了假话。2026-11-21 现场：row 301 单行试烧成功，要续烧 302..311，而
    `--dump` 按规程仍是烧前那份快照（换快照等于换个现场）。没有续烧通道的工具会拒绝继续执行
    它自己刚做了一半的规程。
    """

    def test_crosscheck_accepts_a_row_that_already_holds_the_plan_record(self):
        snap = {**SNAP, 304: 0x80070141}
        self.assertEqual(crosscheck(snap, PLAN, resumed=[304]), [])

    def test_resumed_flag_on_a_row_holding_something_else_still_fails(self):
        """声称"已落地"就得真落地：拿别人的行当自己人，等于把覆盖写放行。"""
        snap = {**SNAP, 304: 0xDEADBEEF}
        probs = crosscheck(snap, PLAN, resumed=[304])
        self.assertTrue(any("resumed" in p for p in probs), probs)

    def test_without_resume_the_same_snapshot_is_refused(self):
        """反向断言：豁免不是默认放宽。不传 resumed 时，同一份快照必须照样报"非空白"。"""
        probs = crosscheck({**SNAP, 304: 0x80070141}, PLAN)
        self.assertTrue(any("not blank" in p for p in probs), probs)


class BurnPlanTests(unittest.TestCase):
    """整条烧写规程的闸序：拒绝必须发生在**任何 PROGRAM 之前**。"""

    def setUp(self):
        self.dev = FakeController(rows=dict(SNAP))
        self.m = FuseMacro(self.dev.rd, self.dev.wr, sleep=self.dev.sleep,
                           clock=self.dev.clock)
        self.out = []

    def run_plan(self, plan=PLAN, snap=None, rows=None):
        return burn_plan(self.m, plan, snap if snap is not None else dict(SNAP),
                         rows=rows, log=self.out.append)

    def test_burns_every_row_and_reports_post_state(self):
        self.assertEqual(self.run_plan(), 0)
        self.assertEqual(self.dev.programs, [(304, 0x80070141), (315, 0x420004C1)])
        self.assertIn("post: CRC=0x55 DSOV=0 FUSEDEBUG=0x0", "\n".join(self.out))

    def test_resume_skips_the_landed_row_and_burns_the_rest(self):
        self.dev.rows[304] = 0x80070141              # 第一条已在卡上（试烧过）
        self.assertEqual(self.run_plan(snap={**SNAP, 304: 0x80070141}), 0)
        self.assertEqual(self.dev.programs, [(315, 0x420004C1)], "已落地的行不许再 PROGRAM")
        self.assertIn("skip row 304", "\n".join(self.out))

    def test_a_foreign_value_in_a_target_row_refuses_before_any_program(self):
        self.dev.rows[315] = 0x11223344              # 既非空白也非本计划记录
        self.assertEqual(self.run_plan(), 1)
        self.assertEqual(self.dev.programs, [])
        self.assertIn("既不是空白也不是本计划记录", "\n".join(self.out))
        self.assertEqual(self.dev.senses, 0, "拒绝必须发生在 sense 之前")

    def test_row_not_in_plan_is_refused(self):
        self.assertEqual(self.run_plan(rows=["999"]), 1)
        self.assertEqual(self.dev.programs, [])

    def test_subset_only_programs_the_named_rows(self):
        self.assertEqual(self.run_plan(rows=["304"]), 0)
        self.assertEqual(self.dev.programs, [(304, 0x80070141)])

    def test_no_window_means_no_sense_no_program(self):
        self.dev.reg[DSOV_STATUS] = 1                # DSOV=1 ⇒ 不在窗口里（健康态/烧完之后）
        self.assertEqual(self.run_plan(), 2)
        self.assertEqual((self.dev.senses, self.dev.programs), (0, []))


class DiffTests(unittest.TestCase):
    """"只烧了该烧的行"必须是函数算出来的，不是人当场数出来的。"""

    def test_only_planned_rows_changed(self):
        after = {**SNAP, 304: 0x80070141, 315: 0x420004C1}
        d = diff_snapshots(SNAP, after, PLAN)
        self.assertEqual(d["expected"], [304, 315])
        self.assertEqual((d["missing"], d["unexpected"], d["mismatch"]), ([], [], []))

    def test_an_extra_changed_row_is_caught(self):
        after = {**SNAP, 304: 0x80070141, 315: 0x420004C1, 330: 0x1}
        self.assertEqual(diff_snapshots(SNAP, after, PLAN)["unexpected"], [330])

    def test_a_planned_row_that_did_not_land_is_missing(self):
        self.assertEqual(diff_snapshots(SNAP, {**SNAP, 304: 0x80070141}, PLAN)["missing"],
                         [315])

    def test_changed_but_wrong_is_mismatch_not_expected(self):
        d = diff_snapshots(SNAP, {**SNAP, 304: 0x80070141, 315: 0x420004C0}, PLAN)
        self.assertEqual((d["expected"], d["mismatch"]), ([304], [315]))

    def test_a_resumed_row_counts_as_neither_missing_nor_unexpected(self):
        before = {**SNAP, 304: 0x80070141}           # 试烧之后的快照：304 已是记录值
        after = {**before, 315: 0x420004C1}
        d = diff_snapshots(before, after, PLAN)
        self.assertEqual((d["expected"], d["missing"], d["unexpected"]), ([315], [], []))

    def test_iff_region_noise_is_not_judged(self):
        """IFF 区（487..511）我们这条路径读不准，变化了也不许当"计划外的改动"—— 否则每次烧写
        都会被自己的读数噪声判失败（判据里有噪声，就等于没有判据）。"""
        after = {**SNAP, 304: 0x80070141, 315: 0x420004C1, 488: 0x12345678}
        self.assertEqual(diff_snapshots(SNAP, after, PLAN)["unexpected"], [])


class GoldenRegression(unittest.TestCase):
    """Real bench-card data (ffcalc dump + its ffcalc-generated plan):
    crosscheck must pass and the plan must look exactly like the July 2026
    burn session of the sibling card (17 records, rows after the last used
    row, every target row blank in the snapshot)."""

    BASE = Path(__file__).resolve().parents[1] / "samples"

    @staticmethod
    def _h(v):
        return int(v, 0) if isinstance(v, str) else int(v)

    def test_bench_card_plan_crosschecks_clean(self):
        dump = json.loads(
            (self.BASE / "bench-card-85b20be7.json").read_text(encoding="utf-8"))
        plan = json.loads(
            (self.BASE / "bench-card-85b20be7.burn.json").read_text(encoding="utf-8"))
        rows = {int(k, 0): self._h(v) for k, v in dump["ff_rows"].items()}
        self.assertEqual(crosscheck(rows, plan), [])
        burn_rows = sorted(rec["row"] for rec in plan["burn"])
        self.assertEqual(len(burn_rows), 17)
        self.assertEqual(burn_rows, list(range(burn_rows[0], burn_rows[0] + 17)))
        used_ff = [r for r in rows
                   if 222 <= r <= 486 and self._h(rows[r])]
        self.assertTrue(all(r < burn_rows[0] for r in used_ff),
                        "plan must start after the last used FF row")


if __name__ == "__main__":
    unittest.main()
