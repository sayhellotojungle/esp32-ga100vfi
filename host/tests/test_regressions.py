"""Behavioral regressions for framing, failure accounting and verdict delivery."""
import contextlib
import inspect
import io
import json
import re
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ga100vfi import classify as classify_module, cli, glitch, shot_policy
from ga100vfi.classify import UNREADABLE, Verdict, attempt_outcome
import ga100vfi_agent as agent_module
from ga100vfi_agent import VerdictAgent, awaiting_token


class ChunkSerial:
    def __init__(self, chunks, read_seconds=0.1):
        self.chunks = iter(chunks)
        self.now = 0.0
        self.read_seconds = read_seconds
        self.written = b''
        self.reads = 0

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.written += data

    def flush(self):
        pass

    def read(self, _size):
        self.now += self.read_seconds
        self.reads += 1
        return next(self.chunks, b'')


def chunk_card(chunks, read_seconds=0.1):
    card = glitch.Card.__new__(glitch.Card)
    card.s = ChunkSerial(chunks, read_seconds)
    return card


class SerialFraming(unittest.TestCase):
    def test_completion_prefix_split_from_fields(self):
        card = chunk_card([b'wifi: connected\n', b'glitch do', b'ne', b'',
                           b' pwd=1 e1=0xc2fa writes_failed=0', b'\n'])
        with patch.object(glitch.time, 'monotonic', lambda: card.s.now):
            report = card.attempt(glitch.Attempt())
        self.assertTrue(report.pwd)
        self.assertEqual(report.writes_failed, 0)
        self.assertEqual(card.s.reads, 6)

    def test_optional_diagnostic_and_actual_timestamps(self):
        card = chunk_card([b'glitch done pwd=1 e1=0xc2fa writes_failed=0 '
                           b'slip=54 reset_slip=-2 diag_ok=0\n'])
        with patch.object(glitch.time, 'monotonic', lambda: card.s.now):
            report = card.attempt(glitch.Attempt())
        self.assertFalse(report.diag_ok)
        self.assertEqual((report.slip_us, report.reset_slip_us), (54, -2))

    def test_malformed_diagnostic_flag_is_not_assumed_good(self):
        card = chunk_card([b'glitch done pwd=1 e1=0xc2fa writes_failed=0 diag_ok=2\n'])
        with patch.object(glitch.time, 'monotonic', lambda: card.s.now):
            with self.assertRaisesRegex(IOError, 'invalid diag_ok'):
                card.attempt(glitch.Attempt())

    def test_waits_through_silence_until_deadline(self):
        card = chunk_card([b'glitch prep\n', b'', b'', b'',
                           b'glitch done pwd=1 e1=0xc2fa writes_failed=0\n'], 0.5)
        with patch.object(glitch.time, 'monotonic', lambda: card.s.now):
            self.assertEqual(card.attempt(glitch.Attempt()).e1, 0xc2fa)

    def test_rejects_unterminated_completion_line(self):
        card = chunk_card([b'glitch done pwd=1 e1=0xc2fa writes_failed=0'], 5)
        with patch.object(glitch.time, 'monotonic', lambda: card.s.now):
            with self.assertRaises(TimeoutError):
                card.attempt(glitch.Attempt())

    def test_error_line_does_not_wait_for_done(self):
        card = chunk_card([b'glitch ERR invalid timing\n'])
        with patch.object(glitch.time, 'monotonic', lambda: card.s.now):
            with self.assertRaisesRegex(IOError, 'invalid timing'):
                card.attempt(glitch.Attempt())
        self.assertEqual(card.s.reads, 1)

    def test_i2c_reply_ignores_unsolicited_network_log(self):
        card = chunk_card([b'wifi: connected\nI2C OK 12', b' ab\n'])
        with patch.object(glitch.time, 'monotonic', lambda: card.s.now):
            self.assertEqual(card.read(0x25, 0x7e, 2), bytes([0x12, 0xab]))

    def test_revert_reports_failed_restore(self):
        card = chunk_card([b'revert vout=1 mode=0\n'])
        with patch.object(glitch.time, 'monotonic', lambda: card.s.now):
            with self.assertRaises(IOError):
                card.revert()


class HostCli(unittest.TestCase):
    def run_cli(self, argv, effects, restored=False):
        card = Mock()
        card.attempt.side_effect = effects
        context = Mock()
        context.__enter__ = Mock(return_value=card)
        context.__exit__ = Mock(return_value=False)
        output = io.StringIO()
        with patch.object(cli, 'Card', return_value=context), \
             patch.object(cli, 'restore', return_value=restored), \
             patch.object(cli.time, 'sleep'), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(argv)
        return code, output.getvalue(), card

    def test_attempt_defaults_exist_and_reach_card(self):
        report = glitch.Report(True, 0xc2fa, 0, '')
        code, _out, card = self.run_cli(['attempt', '--no-unlock'], [report])
        self.assertEqual(code, 0)
        args = card.attempt.call_args.args[0]
        self.assertEqual((args.fast, args.hold_ms), (1, 0))
        card.lock_status.assert_not_called()

    def test_attempt_nonzero_exit_on_failed_writes(self):
        report = glitch.Report(True, 0xc2fa, 1, '')
        code, _out, _card = self.run_cli(['attempt', '--no-unlock'], [report])
        self.assertEqual(code, 1)

    def test_invalid_voltage_rejected_before_opening_serial(self):
        with patch.object(cli, 'Card') as card, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                cli.main(['attempt', '--v-dip', '-1'])
        self.assertEqual(error.exception.code, 2)
        card.assert_not_called()

    def campaign(self, effects, batch=1):
        code, text, card = self.run_cli(
            ['campaign', '--adaptive', '--no-unlock', '--no-revert',
             '--max-attempts', str(len(effects)), '--batch', str(batch)], effects)
        self.assertEqual(code, 1)
        return json.loads(text), card

    def test_first_serial_error_counts_without_crash(self):
        result, _card = self.campaign([IOError('injected write error')])
        self.assertEqual(result['nack'], 1)
        self.assertEqual(result['rounds'][0]['outcomes']['nack'], 1)

    def test_first_failed_write_counts_without_crash(self):
        result, _card = self.campaign([glitch.Report(True, 0xc2fa, 1, '')])
        self.assertEqual(result['nack'], 1)
        self.assertEqual(result['rounds'][0]['outcomes']['nack'], 1)

    def test_failed_postread_diagnostics_count_as_transport_failure(self):
        failed = glitch.Report(True, 0xc2fa, 0, '', diag_ok=False)
        result, _card = self.campaign([failed])
        self.assertEqual(result['nack'], 1)
        self.assertEqual(result['lost'], 0)
        self.assertEqual(result['valid'], 0)
        self.assertEqual(result['rounds'][0]['outcomes']['nack'], 1)

    def test_lost_attempts_finish_batch_and_adjust_voltage(self):
        good = glitch.Report(True, 0xc2fa, 0, '')
        result, _card = self.campaign([good, good], batch=2)
        self.assertEqual(result['lost'], 2)
        self.assertEqual(result['rounds'][0]['outcomes']['lost'], 2)
        self.assertEqual(result['v_dip'], 360)

    def test_new_batch_does_not_reuse_old_failure_counts(self):
        good = glitch.Report(True, 0xc2fa, 0, '')
        result, _card = self.campaign([IOError('NACK'), good])
        self.assertEqual(len(result['rounds']), 2)
        self.assertEqual(result['rounds'][0]['outcomes']['nack'], 1)
        self.assertEqual(result['rounds'][1]['outcomes']['nack'], 0)
        self.assertEqual(result['rounds'][1]['outcomes']['lost'], 1)


class FakeBar:
    """BAR0 stand-in that records the *order* of accesses.

    The point is ordering, not values: reading the fuse block is what hangs the machine
    when the controller is poisoned, so the guard has to happen before the first other
    read, and no write may reach DSOV_SET at all.
    """

    def __init__(self, regs=None):
        self.regs = regs or {}
        self.touched = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def rd32(self, offset):
        self.touched.append(("r", offset))
        return self.regs.get(offset, 0)

    def wr32(self, offset, value):
        self.touched.append(("w", offset, value))


class PoisonGuard(unittest.TestCase):
    """README 红线 1: the controller is asked before anything touches the fuse block."""

    def test_trip_stops_classify_before_any_other_access(self):
        bar = FakeBar({classify_module.FUSECTRL: 0xBADF510E})
        with self.assertRaises(classify_module.Poisoned):
            classify_module.classify(bar)
        self.assertEqual(bar.touched, [("r", classify_module.FUSECTRL)])

    def test_trip_stops_snapshot_too(self):
        bar = FakeBar({classify_module.FUSECTRL: 0xBADF510E})
        with self.assertRaises(classify_module.Poisoned):
            classify_module.snapshot(bar)
        self.assertEqual(bar.touched, [("r", classify_module.FUSECTRL)])

    def test_all_ones_is_the_card_gone_not_a_trip(self):
        # Conflating these two is what made every lost verdict look like a tripped
        # controller. All-FF means the device stopped answering: that is "lost", and the
        # ladder must keep running. The 0xBADF tag is a different animal.
        bar = FakeBar({classify_module.FUSECTRL: UNREADABLE,
                       classify_module.DSOV: UNREADABLE})
        self.assertEqual(attempt_outcome(classify_module.classify(bar)), "lost")

    def test_unknown_controller_state_stops_before_any_other_access(self):
        # 2026-11-20 measured a third state: FUSECTRL=0x00000000 at vd=309. It is not IDLE
        # and not the 0xBADF tag, and the next shot at the same voltage tripped the
        # controller (0xBADF510E) and dropped the card off PCIe. So a value we cannot name
        # must be judged *not at all* -- same one-read budget as the trip guard.
        for ctrl in (0x00000000, 0x00000001, 0xE0000000, 0xE00C0000, 0xFFFFFFFF - 1):
            with self.subTest(ctrl=hex(ctrl)):
                bar = FakeBar({classify_module.FUSECTRL: ctrl,
                               classify_module.DSOV: 0})
                with self.assertRaises(classify_module.ControllerUnknown):
                    classify_module.classify(bar)
                self.assertEqual(bar.touched, [("r", classify_module.FUSECTRL)])

    def test_unknown_state_stops_snapshot_too(self):
        bar = FakeBar({classify_module.FUSECTRL: 0x00000000})
        with self.assertRaises(classify_module.ControllerUnknown):
            classify_module.snapshot(bar)
        self.assertEqual(bar.touched, [("r", classify_module.FUSECTRL)])

    def test_both_stops_share_one_base_class(self):
        # The agent stops the campaign on either; keeping them under one base class is what
        # makes that a single `except`, while `isinstance` still lets the log tell them apart.
        self.assertTrue(issubclass(classify_module.Poisoned,
                                   classify_module.FuseControllerStop))
        self.assertTrue(issubclass(classify_module.ControllerUnknown,
                                   classify_module.FuseControllerStop))
        self.assertFalse(issubclass(classify_module.ControllerUnknown,
                                    classify_module.Poisoned))

    def test_state_classifier_names_every_observed_value(self):
        c = classify_module
        self.assertEqual(c.fusectrl_state(c.FUSE_IDLE), c.STATE_IDLE)
        self.assertEqual(c.fusectrl_state(0xBADF510E), c.STATE_POISONED)
        self.assertEqual(c.fusectrl_state(UNREADABLE), c.STATE_UNREACHABLE)
        self.assertEqual(c.fusectrl_state(0x00000000), c.STATE_UNKNOWN)

    def test_idle_controller_is_not_a_trip(self):
        bar = FakeBar({classify_module.FUSECTRL: classify_module.FUSE_IDLE,
                       classify_module.DSOV: UNREADABLE})
        self.assertEqual(attempt_outcome(classify_module.classify(bar)), "lost")

    def test_no_bar0_write_on_a_shot_that_did_not_flip_dsov(self):
        """2026-11-21: the judging host hard-hung twice, both times on the read path right
        after a shot. Writing BAR0 of a half-dead card is the failure mode this repo already
        documents (logs/02), and on a non-hit the DSOV_SET write cannot change the verdict:
        attempt_outcome() returns "under" for any dsov != 0 whatever the writeback says. So
        the write must not be issued at all -- every routine shot stops carrying the risk.
        """
        bar = FakeBar({classify_module.FUSECTRL: classify_module.FUSE_IDLE,
                       classify_module.DSOV: 1,                     # 没翻转：我们是 under
                       classify_module.CRC_STATUS: classify_module.CRC_GOOD})
        v = classify_module.classify(bar)
        self.assertEqual(attempt_outcome(v), "under")
        self.assertEqual([t for t in bar.touched if t[0] == "w"], [],
                         "非命中的一发一笔 BAR0 写都不许发出去: %r" % (bar.touched,))
        self.assertFalse(v.writeback_probed)
        self.assertFalse(v.checks["dsov_settable"])

    def test_candidate_hit_still_proves_the_shadow_is_writable(self):
        """Skipping the write must not cost us the evidence a burn needs: when DSOV reads 0
        the write-back is mandatory, otherwise a "hit" could be recorded without proof."""
        bar = FakeBar({classify_module.FUSECTRL: classify_module.FUSE_IDLE,
                       classify_module.DSOV: 0,
                       classify_module.DSOV_SET: 1,
                       classify_module.FPF_ROW: 0,
                       classify_module.PRIV_MASK: 0xFFFFFFFF,
                       classify_module.CRC_STATUS: classify_module.CRC_GOOD})
        v = classify_module.classify(bar)
        self.assertEqual([t for t in bar.touched if t[0] == "w"],
                         [("w", classify_module.DSOV_SET, 1)])
        self.assertTrue(v.writeback_probed)
        self.assertEqual(attempt_outcome(v), "goldilocks")

    def test_probe_writeback_flag_is_for_explicit_full_audits_only(self):
        # bench/readtrip.sh 那种"逐项审一遍"的路径要旧行为：即使没翻转也照样探可写性。
        bar = FakeBar({classify_module.FUSECTRL: classify_module.FUSE_IDLE,
                       classify_module.DSOV: 1})
        classify_module.classify(bar, probe_writeback=True)
        self.assertIn(("w", classify_module.DSOV_SET, 1), bar.touched)

    def test_agent_asks_the_controller_before_the_presence_probe(self):
        # gpu_verdict() reads BAR0 offset 0 to prove presence, which is a fuse-block read
        # as far as a hung upstream is concerned. The guard must precede it, and a trip must
        # stop the agent after exactly one read.
        idle = FakeBar({classify_module.FUSECTRL: classify_module.FUSE_IDLE,
                        classify_module.DSOV: UNREADABLE})
        agent = VerdictAgent("http://example.invalid", "0000:01:00.0")
        with patch.object(agent_module, "restore", lambda *a, **k: True), \
                patch.object(agent_module, "Bar0", lambda bdf: idle):
            self.assertEqual(agent.gpu_verdict(), "lost")
        self.assertEqual(idle.touched[0], ("r", classify_module.FUSECTRL))

        tripped = FakeBar({classify_module.FUSECTRL: 0xBADF510E})
        agent = VerdictAgent("http://example.invalid", "0000:01:00.0")
        with patch.object(agent_module, "restore", lambda *a, **k: True), \
                patch.object(agent_module, "Bar0", lambda bdf: tripped):
            with self.assertRaises(classify_module.Poisoned):
                agent.gpu_verdict()
        self.assertEqual(tripped.touched, [("r", classify_module.FUSECTRL)])

    def test_agent_stops_on_the_unknown_state_too(self):
        # The agent is the thing that keeps firing, so the third state has to stop *it*,
        # not just classify(): same one-read budget, and the campaign gets told to stop.
        weird = FakeBar({classify_module.FUSECTRL: 0x00000000})
        agent = VerdictAgent("http://example.invalid", "0000:01:00.0")
        with patch.object(agent_module, "restore", lambda *a, **k: True), \
                patch.object(agent_module, "Bar0", lambda bdf: weird):
            with self.assertRaises(classify_module.ControllerUnknown):
                agent.gpu_verdict()
        self.assertEqual(weird.touched, [("r", classify_module.FUSECTRL)])

        stopped = []
        agent = VerdictAgent("http://example.invalid", "0000:01:00.0")
        agent.stop_board = lambda: stopped.append(True)
        agent.step = Mock(side_effect=classify_module.ControllerUnknown("third state"))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(agent_module.run_forever(agent, poll=0, max_lost=0), 3)
        self.assertEqual(stopped, [True])
        self.assertIn("第三种态", out.getvalue())

    def test_agent_poison_path_still_says_poison(self):
        # Same handler, different tag: the log line is what tells the two states apart
        # afterwards, so the branch must stay distinguishable.
        stopped = []
        agent = VerdictAgent("http://example.invalid", "0000:01:00.0")
        agent.stop_board = lambda: stopped.append(True)
        agent.step = Mock(side_effect=classify_module.Poisoned("0xBADF510E"))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(agent_module.run_forever(agent, poll=0, max_lost=0), 3)
        self.assertEqual(stopped, [True])
        self.assertIn("POISON", out.getvalue())

    def test_stop_board_asks_the_campaign_to_stop(self):
        agent = VerdictAgent("http://example.invalid", "0000:01:00.0")
        agent.get = Mock(return_value="ok")
        agent.stop_board()
        self.assertIn("/api/campaign?op=stop", agent.get.call_args[0][0])

    def test_stop_board_survives_a_silent_device(self):
        # A brownout can take the board with it; the stop attempt must not turn into a
        # crash that hides the reason the run stopped.
        agent = VerdictAgent("http://example.invalid", "0000:01:00.0")
        agent.get = Mock(side_effect=OSError("connection reset"))
        with contextlib.redirect_stdout(io.StringIO()):
            agent.stop_board()


class ClassificationRules(unittest.TestCase):
    def test_unreadable_dsov_is_lost(self):
        self.assertEqual(attempt_outcome(Verdict(0xffffffff, 0, 1, 0xffffffff, 0x55)), 'lost')

    def test_crc_upper_bits_are_not_silently_discarded(self):
        self.assertEqual(attempt_outcome(Verdict(0, 0, 1, 0xffffffff, 0x10055)), 'over')


def waiting(session=11, attempt=1, active=True, awaiting=True):
    """A /api/status reply in which the board is waiting for this shot's verdict.

    0.4.1 removed the board's `mode` field along with the on-board SMBPBI oracle:
    after the settle interval a shot is always waiting for the host, so the shapes
    the agent must ignore are "not running", "not awaiting" and a missing token.
    """
    return {'campaign': {'running': active,
                         'awaiting_verdict': active and awaiting,
                         'session': session, 'attempt': attempt}}


class AgentDelivery(unittest.TestCase):
    def agent(self, state=None):
        # first_read_delay=0：这个量是给真卡留的启动静默，测试里不该付那 2 秒（整份套件本来
        # 0.2 s 跑完）。它自己的那条测试把它设回 2.0 来验证真的会等。
        # max_bar0_sessions=2：这一组测的是【盖章判决】那条通路 —— 复核一次就要再花一次会话
        # （第一次读 + 一次复读），预算 1 的代理根本走不到复核，测试会退化成"永远未盖章"。
        # hunt 阶段导出 1 是【省发数】的有意例外，由 test_找区间阶段每发必须真读一次 与下面那条
        # 单会话不盖章的测试单独钉住。
        agent = VerdictAgent('http://example.invalid', '0000:01:00.0', first_read_delay=0,
                             max_bar0_sessions=2)
        agent.status = Mock(return_value=state or waiting())
        agent.gpu_verdict = Mock(return_value='hit')
        agent.feed = Mock()
        return agent

    # —— 卡不在总线上时一次 BAR0 都不许开（2026-11-20 实锤缺陷）——
    #
    # 旧实现的 evaluate() 是"for index in range(15): gpu_verdict(); sleep(2)"，而 gpu_verdict()
    # 每次都会 restore(bdf)（以 RDWR 打开 config，可能 pwrite BAR0 基址与 COMMAND 的 MEM enable）
    # 再开一次 BAR0 映射。一发 lost 的 waiting 段实测 08:36:19→08:36:47 = 28 s ≈ 15×2 s，也就是
    # 卡正在离开总线的那 28 秒里我们对它开了 15 次 BAR0 会话 —— logs/02 记的正是这件事会把
    # 上游桥一起拖死。红线 1（"熔丝块读会干扰启动，只读一次"）被我们自己的代理违反了 15 遍。

    def test_no_bar0_session_while_the_card_is_off_the_bus(self):
        agent = self.agent(waiting(session=11, attempt=9))
        agent.budget_s, agent.poll_s = 0.05, 0.001
        agent.present = Mock(return_value=False)
        agent.gpu_verdict = Mock(return_value=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent.evaluate((11, 9)), 'lost')
        agent.gpu_verdict.assert_not_called()      # 卡不在 = 一次都不许开

    def test_bar0_sessions_are_capped_per_shot(self):
        # 判不出来也不许拿重读去赌：旧代码这里是 15 次。
        agent = self.agent(waiting(session=11, attempt=7))
        agent.budget_s, agent.poll_s = 0.05, 0.001
        agent.present = Mock(return_value=True)
        agent.gpu_verdict = Mock(return_value=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent.evaluate((11, 7)), 'lost')
        self.assertGreaterEqual(agent.gpu_verdict.call_count, 1)
        self.assertLessEqual(agent.gpu_verdict.call_count, agent.max_bar0_sessions)

    def test_present_maps_all_ones_to_absent(self):
        # presence 判定的本体：只有 config 读回全 1 才算不在；连节点都打不开（测试台/非 Linux）
        # 必须算在场 —— 这道闸门要是反过来判"不在"，代理就一发都判不了了。
        agent = self.agent()
        agent.config_id = Mock(return_value=UNREADABLE)
        self.assertFalse(agent.present())
        self.assertIn("没有打开 BAR0", agent.last_fail)
        agent.config_id = Mock(return_value=0x000020c2)
        self.assertTrue(agent.present())             # 10de:20c2 在场
        agent.config_id = Mock(return_value=None)
        self.assertTrue(agent.present())

    def test_first_read_delay_lets_the_card_boot_before_we_read(self):
        # 操作员 2026-11-20 的要求：打完过一会儿再读。板子的 settle 已经等过 restore_wait_ms，
        # 这一段是给卡自己的启动窗留的额外余量（熔丝块读会干扰启动 = 红线 1）。
        # 0.5.5 把这一次长睡换成 0.25 s 一跳的谓词轮询（下限不变），所以这里断言的不再是"睡过
        # 一次 2.0"，而是它真正要保证的那件事：**下限之内一枪 BAR0 都不许开**。
        agent = self.agent(waiting(session=11, attempt=8))
        agent.first_read_delay = 2.0
        clock = {"t": 0.0}
        slept, reads_at = [], []

        def fake_sleep(s):
            slept.append(s)
            clock["t"] += s

        def fake_verdict():
            reads_at.append(clock["t"])
            # 健康基线读数：按 _needs_confirm 的规矩，基线不追加第二次会话
            agent.last_regs = {"FUSECTRL": 0xE0040000, "DSOV": 1, "CRC": 0x55,
                               "PRIV": 0x1E, "FPF": 0xFFFFFFFF}
            return "under"

        agent.gpu_verdict = Mock(side_effect=fake_verdict)
        with patch.object(agent_module.time, "sleep", fake_sleep), \
             patch.object(agent_module.time, "monotonic", lambda: clock["t"]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent.evaluate((11, 8)), 'under')
        self.assertGreaterEqual(sum(slept), 2.0)              # 静默下限等满了
        self.assertGreaterEqual(reads_at[0], 2.0)             # 下限之内没有开读
        self.assertEqual(len(reads_at), 1)                     # 基线不复核
        self.assertEqual(agent.reads, 1)
        self.assertTrue(agent.stable)                          # 基线：按规矩不查也算盖了章

    def test_a_moving_reading_is_re_read_and_the_alarm_wins(self):
        """脏读与真反应长得一样，只有一次复核能分开它们；不一致时取更报警的那份。

        11-25 那 40 发里约 1/3 的 CRC 读成 0x57/0x75（该是 0x55）。单发单读时"我们读早了"与
        "卡真的动了"完全同形，而这两个判决的处置相反（前者继续打、后者立刻停火并冻坐标）。
        """
        agent = self.agent(waiting(session=11, attempt=12))
        agent.budget_s, agent.poll_s, agent.stable_s = 0.05, 0.001, 0
        agent.present = Mock(return_value=True)
        readings = [{"FUSECTRL": 0xE0040000, "DSOV": 1, "CRC": 0x57, "PRIV": 0x1E,
                     "FPF": 0xFFFFFFFF},        # 第一次：CRC 脏
                    {"FUSECTRL": 0xE0040000, "DSOV": 0, "CRC": 0x55, "PRIV": 0x1E,
                     "FPF": 0xFFFFFFFF}]        # 第二次：DSOV 离开基线 = 反应
        agent.gpu_verdict = Mock(side_effect=lambda: (
            agent.last_regs.update(readings[min(agent.gpu_verdict.call_count - 1, 1)]),
            "under" if agent.gpu_verdict.call_count == 1 else "over")[1])
        with contextlib.redirect_stdout(io.StringIO()):
            verdict = agent.evaluate((11, 12))
        self.assertEqual(agent.gpu_verdict.call_count, 2)      # 非基线 → 复核一次
        self.assertFalse(agent.stable)
        self.assertEqual(verdict, "over")                      # 报警的那一份胜出
        self.assertEqual(agent.last_regs["DSOV"], 0)

    def test_a_clean_repeat_is_stamped_stable(self):
        agent = self.agent(waiting(session=11, attempt=13))
        agent.budget_s, agent.poll_s, agent.stable_s = 0.05, 0.001, 0
        agent.present = Mock(return_value=True)
        regs = {"FUSECTRL": 0xE0040000, "DSOV": 1, "CRC": 0x57, "PRIV": 0x1E,
                "FPF": 0xFFFFFFFF}
        agent.gpu_verdict = Mock(side_effect=lambda: (
            agent.last_regs.update(regs), "under")[1])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent.evaluate((11, 13)), "under")
        self.assertEqual(agent.gpu_verdict.call_count, 2)
        self.assertTrue(agent.stable)

    def test_单会话预算下脏读只能判不能盖章(self):
        """hunt 每发的设置是【一次会话、一次读】(BAR0_SESSIONS=1 / CONFIRM_READS=1)：够分 under/over，
        但**天生盖不了章** —— 这正是"命中判据必须交给批末那次单独读"的代码落点。

        2026-11-26 把 bar0_sessions 压到 1 是为了多买几发（每碰一次卡一次，死亡时刻就往前挪一次），
        代价必须写死在测试里：脏读（CRC=0x57 ≠ 健康的 0x55）也不许偷偷开第二次会话去复核 —— 那正是
        2026-11-20 一发 lost 里 15 次会话把整机拖死的形状（红线 1）。它只能明着承认"这一发没盖章"。
        """
        agent = self.agent(waiting(session=11, attempt=15))
        agent.max_bar0_sessions, agent.confirm_reads = 1, 1
        agent.budget_s, agent.poll_s, agent.stable_s = 0.05, 0.001, 0
        agent.present = Mock(return_value=True)
        regs = {"FUSECTRL": 0xE0040000, "DSOV": 1, "CRC": 0x57, "PRIV": 0x1E, "FPF": 0xFFFFFFFF}
        agent.gpu_verdict = Mock(side_effect=lambda: (agent.last_regs.update(regs), "under")[1])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent.evaluate((11, 15)), "under")
        self.assertEqual(agent.gpu_verdict.call_count, 1, "会话预算 1 却开出第二次会话 = 红线 1")
        self.assertFalse(agent.stable, "hunt 的 1/1 设置下这发就是未盖章：说成盖章会骗我们去找不存在的章")

    def test_a_dying_shot_never_gets_a_second_bar0_session(self):
        # 复核是给"读到了东西而且不像基线"准备的；卡正在离开总线时追加 BAR0，正是 2026-11-20
        # 那一发 lost 里 15 次会话把整机拖死的形状（红线 1）。宁可这一发没有判决。
        agent = self.agent(waiting(session=11, attempt=14))
        agent.budget_s, agent.poll_s = 0.05, 0.001
        agent.present = Mock(return_value=True)
        agent.gpu_verdict = Mock(return_value='lost')          # 熔丝块没读到
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent.evaluate((11, 14)), 'lost')
        self.assertEqual(agent.gpu_verdict.call_count, 1)
        self.assertIsNone(agent.stable)                        # 没盖章，也没假称盖过

    def test_shot_tags_carry_the_physical_code(self):
        # 台账按 mV 索引，而 6.25 mV 才是 LSB：372 与 375 是同一个码 60。判决行里没有 code 时,
        # 两批"不同的电压"其实可能是同一发 —— 11-21 那条 2 mV 阶梯就是这么打了三发同一坐标。
        agent = self.agent()
        self.assertIn("code=60(375mV)", agent.shot_tags({"v_dip": 372}))
        self.assertIn("code=60", agent.shot_tags({"v_dip": 375, "v_code": 60}))
        self.assertNotIn("(375mV)", agent.shot_tags({"v_dip": 375, "v_code": 60}))
        self.assertIn("codes=14", agent.shot_tags({"v_dip": 372, "v_code": 60,
                                                   "codes_tried": 14}))
        agent.t_cfg_s, agent.t_bar0_s, agent.stable = 1.4, 4.0, False
        tags = agent.shot_tags({"v_dip": 372})
        self.assertIn("t_cfg=1.4s", tags)
        self.assertIn("t_bar0=4.0s", tags)
        self.assertIn("stable=NO", tags)                       # 没盖章的那几行必须自己说明

    def test_read_gating_defaults_are_the_safe_ones(self):
        # 数值本身不再在这里钉 —— 它只有一个出处（shot_policy，见 ShotPolicy 那组）。这里钉的是这条
        # 数**不许退化成 0**：2 s 时本卡同一族 55 发里有 31 发把 CRC 读脏，静默下限是本台的保护量。
        agent = VerdictAgent('http://example.invalid', '0000:00:00.0')
        self.assertEqual(agent.first_read_delay, shot_policy.get('first_read_delay_s'),
                         "代理又有了自己的第二个默认值")
        self.assertGreaterEqual(agent.first_read_delay, 2.0,
                                "首次读的静默下限被调到保护量以下（红线 1）")
        self.assertGreaterEqual(agent.max_bar0_sessions, 0)
        self.assertLess(agent.budget_s, 120)        # 必须短于固件的 MANUAL_TIMEOUT_US(120 s)

    def test_retry_keeps_result_and_does_not_reclassify(self):
        agent = self.agent()
        agent.feed.side_effect = [OSError('temporary HTTP error'), None]
        with self.assertRaises(OSError):
            agent.step()
        self.assertIsNone(agent.accepted)
        self.assertEqual(agent.pending, ((11, 1), 'hit'))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(agent.step())
        self.assertEqual(agent.gpu_verdict.call_count, 1)
        self.assertEqual(agent.feed.call_count, 2)
        self.assertEqual(agent.accepted, (11, 1))

    # —— 判决行的读数只许属于它自己那一发（2026-11-20 现场缺陷）——
    #
    # session 2235437716：attempt 1 -> under 带着 DSOV=0x00000001 CRC=0x00000075；attempt 2
    # -> lost，同一行却印着一模一样的五个寄存器。真相是第 2 发一个寄存器都没读到（卡已不在
    # 总线上，gpu_verdict() 在 restore() 那一步就 return None），而 last_regs 是上一发的遗留。
    # 那行读起来像"卡掉链路时控制器还是 IDLE" —— 而这恰好是我们决定 POST 还是拔电最依赖的
    # 证据。gpu_verdict() 有好几条不写 last_regs 的早退路，打印处必须把"没读到"表示出来。

    def test_lost_line_never_inherits_the_previous_shots_registers(self):
        agent = self.agent()
        cur = {"t": (11, 1)}
        agent.status = Mock(side_effect=lambda: waiting(session=cur["t"][0], attempt=cur["t"][1]))

        # 第 1 发：真读到五个寄存器（gpu_verdict() 的成功路径就是这样写 last_regs 的）
        def read_full():
            agent.last_regs, agent.last_fail = {"FUSECTRL": 0xE0040000, "DSOV": 1, "CRC": 0x75}, ""
            return "under"

        agent.present = Mock(return_value=True)
        agent.gpu_verdict = Mock(side_effect=read_full)
        self.assertEqual(agent.evaluate((11, 1)), "under")
        self.assertIn("DSOV=0x00000001", agent.regs_line())

        # 第 2 发：卡已经不在总线上。这里必须让 gpu_verdict() **根本不碰** last_regs —— 现场
        # 就是这个形状：restore() 那一步 return None，谁也没写过 last_regs，而打印处照打，
        # 于是 lost 那一行印着上一发的 FUSECTRL/DSOV/CRC。把它写成"清空 last_regs"就什么也
        # 测不到（变异"不按发归零"能活下来就是因为踩过这个坑）。
        cur["t"] = (11, 2)
        agent.present = Mock(return_value=False)
        agent.gpu_verdict = Mock(return_value=None)
        agent.budget_s, agent.poll_s = 0.05, 0.001
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent.evaluate((11, 2)), "lost")
        line = agent.regs_line()
        self.assertIn("读数=无", line)
        self.assertNotIn("DSOV", line)          # 上一发的数，一个字都不许带过来
        self.assertNotIn("e0040000", line.lower())
        agent.gpu_verdict.assert_not_called()   # 卡不在 = 一次 BAR0 都没开

    def test_partial_read_says_what_is_missing(self):
        # FUSECTRL 读到了、presence probe 失败：这一发确实读过控制器，那部分证据要留下，
        # 同时必须写明熔丝块没读到 —— 否则下次翻台账会把"只读到控制器"当成"整套都干净"。
        agent = self.agent()
        cur = {"t": (11, 4)}
        agent.status = Mock(side_effect=lambda: waiting(session=cur["t"][0], attempt=cur["t"][1]))

        def go():
            agent.last_regs, agent.last_fail = {"FUSECTRL": 0xE0040000}, "bar0-unreadable"
            return "lost"

        agent.gpu_verdict = Mock(side_effect=go)
        self.assertEqual(agent.evaluate((11, 4)), "lost")
        line = agent.regs_line()
        self.assertIn("FUSECTRL=0xe0040000", line)
        self.assertIn("bar0-unreadable", line)

    def test_same_token_retried_keeps_its_own_reads(self):
        # 归零按"发"（token），不按调用：网络错误会让 step() 对同一个 token 再调一次
        # evaluate，按调用归零就把这一发已经读到的寄存器丢掉了，判决行会变成"读数=无"。
        agent = self.agent()
        cur = {"t": (11, 5)}
        agent.status = Mock(side_effect=lambda: waiting(session=cur["t"][0], attempt=cur["t"][1]))
        seen = []

        def go():
            seen.append(1)
            if len(seen) == 1:
                agent.last_regs, agent.last_fail = {"DSOV": 1, "CRC": 0x55}, ""
            return "under"

        agent.gpu_verdict = Mock(side_effect=go)
        self.assertEqual(agent.evaluate((11, 5)), "under")
        self.assertEqual(agent.evaluate((11, 5)), "under")    # 重试这一拍什么都没读
        self.assertIn("DSOV=0x00000001", agent.regs_line())

    def test_does_not_observe_nonwaiting_campaign(self):
        """0.4.1：板子只有一个判决来源，所以"该不该去判"只看运行/等待/令牌三件事。

        以前这里还要看 `mode`：`mode=0` 是板载 SMBPBI 自判，代理不许插手。那个模式已
        被删除（本台架四次 SMBPBI 读全部无回执），继续留着 `mode` 只会让人以为存在一条
        "板子自己会判"的退路。
        """
        cases = {
            "没在跑": waiting(active=False),
            "还在 settle": waiting(awaiting=False),
            "令牌无效": waiting(session=0),
            "字段缺失": {'campaign': {'running': True}},
        }
        for label, state in cases.items():
            with self.subTest(case=label):
                agent = self.agent(state)
                self.assertFalse(agent.step())
                agent.gpu_verdict.assert_not_called()
                agent.feed.assert_not_called()

    def test_discards_result_if_campaign_changes_before_post(self):
        agent = self.agent()
        agent.status.side_effect = [waiting(), waiting(), waiting(session=12)]
        self.assertFalse(agent.step())
        self.assertIsNone(agent.pending)
        agent.feed.assert_not_called()

    def test_pending_result_discarded_after_stop(self):
        agent = self.agent(waiting(active=False))
        agent.pending = ((11, 1), 'goldilocks')
        self.assertFalse(agent.step())
        self.assertIsNone(agent.pending)
        agent.gpu_verdict.assert_not_called()
        agent.feed.assert_not_called()

    def test_new_session_with_same_attempt_number_is_new_work(self):
        agent = self.agent()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(agent.step())
            agent.status.return_value = waiting(session=12)
            self.assertTrue(agent.step())
        self.assertEqual(agent.gpu_verdict.call_count, 2)
        self.assertEqual(agent.accepted, (12, 1))

    def test_server_rejects_stale_token_without_marking_accepted(self):
        # 用 `under` 而不是默认的 `hit`：`hit`/`over`/`goldilocks` 现在都会**先由我们自己**叫停战役
        # （见 test_the_goldilocks_row_survives_the_stopped_campaign），那种 409 是预期内的，判决行
        # 必须照打。这里要保的是另一半：不是我们停的、token 也已经过期 ⇒ 原样丢弃、不认账。
        agent = self.agent()
        agent.gpu_verdict = Mock(return_value='under')
        agent.feed.side_effect = urllib.error.HTTPError('http://x', 409, 'stale', {}, None)
        self.assertFalse(agent.step())
        self.assertIsNone(agent.accepted)
        self.assertIsNone(agent.pending)

    def test_invalid_token_values_are_ignored(self):
        for value in (0, -1, True, '1', 0x100000000):
            self.assertIsNone(awaiting_token(waiting(session=value)))

    def lost_agent(self, max_lost=1):
        """An agent whose only verdict ever is 'lost', with feed/stop recorded in order."""
        agent = VerdictAgent('http://example.invalid', '0000:01:00.0', max_lost=max_lost,
                             first_read_delay=0)   # 静默量本身另有专门测试，这里不付那 2 秒
        agent.status = Mock(return_value=waiting())
        agent.gpu_verdict = Mock(return_value='lost')
        calls = []
        agent.feed = Mock(side_effect=lambda *a: calls.append('feed'))
        agent.stop_board = Mock(side_effect=lambda: calls.append('stop'))
        return agent, calls

    def test_lost_verdict_stops_the_campaign_before_the_feed(self):
        # campaign.c ingests a verdict and advances the state machine in the same
        # campaign_poll() call, so the stop has to be asked for *before* the verdict is
        # delivered. Measured on the second card (2026-11-20): with the stop after the
        # feed, every "lost" round came back tries=2 — one judged shot plus one that
        # nobody ever judged. The archive rule is 一发 lost 即停火 (docs/UNLOCK_AND_POISON.md §四).
        agent, calls = self.lost_agent()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(agent.step())
        self.assertEqual(calls, ['stop', 'feed'])
        self.assertEqual(agent.lost_streak, 1)
        self.assertTrue(agent.lost_stopped)
        agent.stop_board.assert_called_once()
        # agent.log is what bench/mkledger.sh parses: a lost shot that is not recorded is a
        # coordinate the next power cycle has to buy again with scarce shots.
        self.assertIn('-> lost', out.getvalue())

    def test_lost_ledger_line_survives_the_stopped_campaign(self):
        # Stopping first means the board may answer the verdict with 409 (it no longer
        # awaits this token). That must not swallow the verdict line.
        agent, calls = self.lost_agent()
        agent.feed = Mock(side_effect=urllib.error.HTTPError('http://x', 409, 'gone', {}, None))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(agent.step())
        self.assertEqual(calls, ['stop'])
        self.assertIn('-> lost', out.getvalue())
        self.assertEqual(agent.lost_streak, 1)
        self.assertEqual(agent.accepted, (11, 1))

    def test_one_lost_is_enough_by_default(self):
        # The threshold used to be 3, which meant the agent never asked the board to stop
        # on the first lost at all; the only thing that stopped the campaign was crack.sh's
        # 3 s status poll, and it lost the race.
        self.assertEqual(VerdictAgent('http://example.invalid', '0000:00:00.0').max_lost, 1)
        agent, calls = self.lost_agent(max_lost=3)      # 显式放宽时仍然要能连吃三发
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(agent.step())
        self.assertEqual(calls, ['feed'])
        self.assertFalse(agent.lost_stopped)

    def test_under_verdicts_never_stop_the_campaign(self):
        # under 停火等于每批第一发就收工：搜索就再也铺不开发了数。
        for verdict in ('under',):
            agent = self.agent(waiting())
            agent.gpu_verdict = Mock(return_value=verdict)
            agent.stop_board = Mock()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(agent.step())
            self.assertFalse(agent.lost_stopped, verdict)
            self.assertFalse(agent.reacted, verdict)
            agent.stop_board.assert_not_called()

    def test_a_reaction_stops_the_campaign_before_the_feed(self):
        # 红线"见 DSOV=0 立刻停火"以前只是文档，2026-11-21 为它付了一次拔电：第一发
        # `DSOV=0 CRC=0x75`（delay=68 vd=372）之后板子按 ladder 又自己连打 3 发，最后一发
        # 没人判过，卡硬死。停火和 lost 同理必须在 feed **之前**（campaign.c 收判决与推进
        # 状态机在同一次 poll 里）。
        for verdict in ('over', 'hit', 'goldilocks'):
            agent = self.agent(waiting())
            agent.gpu_verdict = Mock(return_value=verdict)
            calls = []
            agent.feed = Mock(side_effect=lambda *a: calls.append('feed'))
            agent.stop_board = Mock(side_effect=lambda: calls.append('stop'))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(agent.step(), verdict)
            self.assertEqual(calls, ['stop', 'feed'], verdict)
            self.assertTrue(agent.reacted, verdict)
            self.assertFalse(agent.lost_stopped, verdict)

    def test_dsov_leaving_the_baseline_is_a_reaction_even_when_the_word_is_under(self):
        """判决词会漏（06:55 那发 CRC 脏却判 under），所以停火看的是遥测本身。"""
        agent = self.agent(waiting())
        # 真 gpu_verdict() 会把读到的寄存器写进 last_regs，evaluate 每发先清空它，所以模拟
        # 也必须写在判决那一刻，不能在外面预先塞。
        def shot():
            agent.last_regs = {"FUSECTRL": 0xe0040000, "DSOV": 0, "CRC": 0x75,
                               "PRIV": 0xffffffff, "FPF": 0}
            return 'under'
        agent.gpu_verdict = Mock(side_effect=shot)
        agent.stop_board = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(agent.step())
        self.assertTrue(agent.reacted)
        agent.stop_board.assert_called_once()

    def test_a_dirty_crc_alone_does_not_stop_the_campaign(self):
        """CRC 读脏 = 我们读得太早（本台账 33 发都有）。拿它停火等于每批只能买一发。"""
        agent = self.agent(waiting())
        def shot():
            agent.last_regs = {"FUSECTRL": 0xe0040000, "DSOV": 1, "CRC": 0x75,
                               "PRIV": 0xffffffff, "FPF": 0}
            return 'under'
        agent.gpu_verdict = Mock(side_effect=shot)
        agent.stop_board = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(agent.step())
        self.assertFalse(agent.reacted)
        agent.stop_board.assert_not_called()

    def test_run_forever_exits_after_a_reaction_so_the_family_gets_frozen(self):
        """代理必须**退出**而不是继续轮询：crack.sh 见代理活着就会把这一族再撑一发。"""
        agent = self.agent(waiting())
        agent.gpu_verdict = Mock(return_value='over')
        agent.stop_board = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent_module.run_forever(agent, poll=0, max_lost=1), 4)
        agent.stop_board.assert_called_once()

    def test_the_goldilocks_row_survives_the_stopped_campaign(self):
        """停火抢在 feed 前面 ⇒ 板子必回 409，而判决行**必须照样打印**。

        2026-11-21 的真实现场：`delay=64 vd=372` 读到 `DSOV=0 CRC=0x55`（goldilocks，本卡第一发
        命中），代理先 stop 再 feed，409 被当成"过期判决"丢掉 —— 命中发生了，agent.log 与台账里
        却没有这一发，包络与门禁于是都不知道我们摸到了窗口。
        """
        agent = self.agent(dict(waiting(), campaign=dict(waiting()['campaign'],
                                                         delay=64, v_dip=372)))
        agent.gpu_verdict = Mock(return_value='goldilocks')
        agent.feed = Mock(side_effect=urllib.error.HTTPError('http://x', 409, 'gone', {}, None))
        agent.stop_board = Mock()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(agent.step())
        txt = out.getvalue()
        self.assertIn('-> goldilocks', txt, "命中这一发在台账里不存在")
        self.assertIn('delay=64 vd=372', txt)     # 坐标必须与判决同行，否则台账不知道怎么精修
        self.assertEqual(agent.accepted, (11, 1), "409 之后要认账，否则下一轮重复交同一发")

    def test_run_forever_exits_after_the_agent_already_stopped(self):
        # Drive the real step(): the stop and the ledger line come from one code path, and
        # run_forever must not issue a second stop for the same event.
        #
        # The token list is a hard bound, not decoration: run_forever's only exits are the
        # lost stop and KeyboardInterrupt. If a mutation (or a regression) makes the lost
        # path never set lost_stopped, an unbounded stub would spin forever at poll=0 and
        # fill memory with the captured stdout — which is how this suite once froze the
        # bench machine. Empty list → KeyboardInterrupt → rc 0 → the assert below fails.
        agent = self.agent(waiting())
        agent.gpu_verdict = Mock(return_value='lost')
        calls = []
        tokens = [(11, 1), (11, 2), (11, 3)]

        def status():
            if not tokens:
                raise KeyboardInterrupt
            return waiting(session=tokens[0][0], attempt=tokens[0][1])

        def feed(token, verdict):
            tokens.pop(0)

        agent.status = Mock(side_effect=status)
        agent.feed = Mock(side_effect=feed)
        agent.stop_board = Mock(side_effect=lambda: calls.append('stop'))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent_module.run_forever(agent, poll=0, max_lost=1), 2)
        self.assertEqual(calls, ['stop'])          # 只停一次，不是每发一次

    def test_run_forever_honours_its_max_lost_argument(self):
        # main() passes --max-lost through run_forever; the loop must not keep a second,
        # private copy of the threshold. step() is the real one here.
        #
        # The token has to advance between polls: a stub that keeps handing back the same
        # token makes step() return False forever (accepted == token), and run_forever has
        # no other exit — with poll=0 that is an infinite loop, which is exactly what this
        # test did to the suite the first time. When the token list runs out we raise, so
        # an over-stalled threshold fails loudly instead of hanging.
        agent = self.agent(waiting())
        agent.gpu_verdict = Mock(return_value='lost')
        agent.stop_board = Mock()
        agent.max_lost = 9                      # 高于传进来的 3：只有听 run_forever 的才会停
        tokens = [(11, 1), (11, 2), (11, 3), (11, 4)]

        def status():
            if not tokens:
                raise KeyboardInterrupt          # 阈值失效时到这里退出，断言随即失败
            return waiting(session=tokens[0][0], attempt=tokens[0][1])

        def feed(token, verdict):
            tokens.pop(0)                       # 判决被收下方可换下一个 token

        agent.status = Mock(side_effect=status)
        agent.feed = Mock(side_effect=feed)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent_module.run_forever(agent, poll=0, max_lost=3), 2)
        agent.stop_board.assert_called_once()
        self.assertEqual(agent.max_lost, 3)
        self.assertEqual(agent.lost_streak, 3)   # 攒够 3 连 lost 才停，不是 9

    def test_cli_default_max_lost_is_one(self):
        with patch.object(agent_module, 'run_forever', return_value=0) as run:
            agent_module.main(['http://example.invalid'])
        agent, _poll, max_lost = run.call_args[0]
        self.assertEqual(max_lost, 1)
        self.assertEqual(agent.max_lost, 1)


class Bar0LinkGate(unittest.TestCase):
    """红线变成代码：BAR0 只允许在两条只读证据都说"卡活着"时才允许映射。

    2026-11-21 判决机整机死了四次，每一次都是把 MMIO 打向一块链路已经不存在的卡 ——
    包括"卡是异步死的"那种：判决读回来是好的，几秒后卡才在 devinit 里没掉。所以闸门
    必须长在 bar0.py 这个咽喉点上（cli / burn / agent 都从这儿进），而不是写在文档里
    等人记得。
    """

    def setUp(self):
        from ga100vfi import bar0 as mod
        self.mod = mod
        self.up = {"cap": 0x60, "status": 0x1003, "speed": 1, "width": 8,
                   "training": 0, "dll_active": 1}

    def test_bridge_dll_active_zero_must_NOT_block_our_own_bench(self):
        """2026-11-21 实测：卡健康（config 报 10de:20c2，内核报链路 Gen1 x8）时，上游桥
        LnkSta 仍是 0x1081 —— speed/width 正常，DLLLA(bit13) 与 LT(bit11) 恒为 0。这台
        Intel root port 根本没实现这两位。闸门最初正是拿 dll_active==0 否决的，于是它
        会把**每一发**都拒掉：'一发都判不出来' 比它想防的那个故障更贵、也更难发现。
        """
        stuck_low = dict(self.up, status=0x1081, dll_active=0, training=0)
        with patch.object(self.mod, "link_state", return_value=stuck_low), \
                patch.object(self.mod, "device_ids", return_value=(0x10DE, 0x20C2)):
            self.mod.require_mapped("0000:01:00.0")          # must not raise

    def test_refuses_every_poisoned_device_id_we_have_measured(self):
        # 健康只有一个值，毒态我们已经量到两个（0x2080 / 0x1061）—— 判据必须是"不是 20c2"。
        for dev in (0x2080, 0x1061, 0x0000, 0xFFFF):
            with patch.object(self.mod, "link_state", return_value=self.up), \
                    patch.object(self.mod, "device_ids", return_value=(0x10DE, dev)):
                with self.assertRaises(self.mod.Bar0Unavailable, msg=f"dev={dev:#06x}") as cm:
                    self.mod.require_mapped("0000:01:00.0")
            self.assertIn("fingerprint", str(cm.exception))

    def test_refuses_a_card_that_does_not_answer_config(self):
        with patch.object(self.mod, "link_state", return_value=self.up), \
                patch.object(self.mod, "device_ids", return_value=None):
            with self.assertRaises(self.mod.Bar0Unavailable):
                self.mod.require_mapped("0000:01:00.0")

    def test_allows_the_healthy_card_and_foreign_devices(self):
        with patch.object(self.mod, "link_state", return_value=self.up), \
                patch.object(self.mod, "device_ids", return_value=(0x10DE, 0x20C2)):
            self.mod.require_mapped("0000:01:00.0")          # must not raise
        with patch.object(self.mod, "link_state", return_value={}), \
                patch.object(self.mod, "device_ids", return_value=(0x8086, 0x0C01)):
            self.mod.require_mapped("0000:00:01.0")          # 不是我们这块卡，别拦别的设备

    def test_constructor_cannot_be_used_to_bypass_the_gate(self):
        # 闸门要长在 mmap 之前，否则"记得调闸门"又变回口头纪律。
        with patch.object(self.mod, "require_mapped",
                          side_effect=self.mod.Bar0Unavailable("gated")) as gate, \
                patch.object(self.mod.os, "open") as opener:
            with self.assertRaises(self.mod.Bar0Unavailable):
                self.mod.Bar0("0000:01:00.0")
            gate.assert_called_once_with("0000:01:00.0")
            opener.assert_not_called()

    def test_a_card_that_vanishes_mid_session_stops_the_very_next_read(self):
        """卡会**异步死**：今天有一发判决读回来是好的，四秒后卡才从总线上消失。判决是
        一串 BAR0 读，所以"映射之前查一次"留下的挂死窗口是整场判决的长度。rd32 之前先做
        一次 config 心跳（root port 代答，卡死了也读得动），把窗口从整场判决缩到一个寄存器。
        """
        bar = self.mod.Bar0.__new__(self.mod.Bar0)          # 绕开 mmap，只测守卫
        bar.bdf = "0000:01:00.0"
        bar.guard_reads = True
        alive = [(0x10DE, 0x20C2), (0x10DE, 0x20C2), None]  # 第三读之前卡没了
        with patch.object(self.mod, "device_ids", side_effect=alive):
            self.assertIsNone(bar._alive())
            self.assertIsNone(bar._alive())
            with self.assertRaises(self.mod.Bar0Unavailable):
                bar._alive()

    def test_the_heartbeat_is_not_something_a_caller_can_forget_to_call(self):
        src = inspect.getsource(self.mod.Bar0.rd32)
        self.assertIn("self._alive()", src)
        self.assertIn("self._alive()", inspect.getsource(self.mod.Bar0.wr32))

    def test_the_pre_shot_gate_never_touches_bar0(self):
        """发前闸门必须是 0 次 BAR0 —— 2026-11-21 第五次整机死机就是它造成的。

        那次板子 /api/log 里一发都没打（只有上一轮的 vd=354），判决机却硬死了：把机器
        带走的根本不需要波形，是我们在开火之前对卡做的探测（restore 写 COMMAND 打开 mem
        decode，再 mmap 读 FUSECTRL）。毒态本来就有纯 config 的指纹（健康只有 20c2，毒态
        实测 0x2080 / 0x1061），所以那一问从来就是可以省掉的 BAR0 花费。
        """
        script = (Path(__file__).resolve().parents[2] / "bench" / "poison_check.sh").read_text(
            encoding="utf-8")
        # 只看可执行的那几行：本文件的注释里大量讨论 restore 为什么危险，算进断言就自打。
        code = "\n".join(l for l in script.splitlines() if not l.strip().startswith("#"))
        self.assertIn('MODE=${POISON_MODE:-config}', code, "发前闸门默认必须 config-only")
        fuse = code.find('if mode != "fuse"')                        # 唯一的分岔点
        self.assertGreater(fuse, 0, "闸门里没有 config-only 的提前退出")
        head, tail = code[:fuse], code[fuse:]
        for reach in ("bar0.restore(", "bar0.Bar0(", "import bar0", "from ga100vfi import bar0"):
            self.assertNotIn(reach, head, f"config-only 段里不许出现 {reach}")
            self.assertIn(reach, tail, f"FUSECTRL 那一读应当留在 fuse 分支里：{reach}")
        # 每一步危险动作都得先留脚印（硬挂死时内核一句话都留不下，见 bar0._probe）
        self.assertIn('step("bar0.restore()', code)
        self.assertIn("os.fsync", code)

    def test_the_official_firing_entry_has_no_bar0_before_the_shot(self):
        """bench/crack.sh 在"板载 campaign"之前必须是 0 次 BAR0 —— 这条形状今天杀了两次机器。

        正式入口原来在第 1 步就 restore()+mmap 读 FUSECTRL：05:01 与 05:13 两次整机死机，板子
        /api/log 里**一发都没有**，也就是说把机器带走的是开火前的探测，不是波形。
        """
        src = (Path(__file__).resolve().parents[2] / "bench" / "crack.sh").read_text(
            encoding="utf-8")
        shot = src.find("板载 campaign ==")
        self.assertGreater(shot, 0, "找不到 campaign 起点，断言无从谈起")
        head = "\n".join(l for l in src[:shot].splitlines() if not l.strip().startswith("#"))
        for reach in ("bar0.Bar0(", "bar0.restore(", "from ga100vfi import bar0", "mmap"):
            self.assertNotIn(reach, head, f"开火之前不许出现 {reach}")
        self.assertIn("poison_check.sh", head, "发前闸门必须走 bench/poison_check.sh")
        # 写上游桥的两条（rescan / Retrain）实测 4/4 无效并挂死过整机：入口里不许再有
        # （注释里提它、解释为什么不用的那些不算 —— 判据只看可执行行）
        executable = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        self.assertNotIn("/sys/bus/pci/rescan", executable, "rescan 已从根上禁用")
        self.assertNotIn("link_retrain", executable, "对桥发 Retrain 的脚本不许出现在开火路径上")

    def test_upstream_bridge_is_read_only_forever(self):
        """写上游桥（retrain/rescan）实测 4/4 无效并且两次挂死整机 —— 从代码面上禁掉。"""
        src = inspect.getsource(self.mod.link_state)
        self.assertIn("os.O_RDONLY", src)
        for forbidden in ("O_RDWR", "O_WRONLY", "pwrite", "write("):
            self.assertNotIn(forbidden, src, f"bridge access must be read-only: {forbidden}")
        # 整个模块的写预算：卡自己的 config 两处（`restore()` 写 BAR0 基址、
        # `ensure_memory_enable()` 写 COMMAND），resource0 的 mmap 一处。0.4.0 之前是两处，
        # 多出来的那一处不是新开的权限面，而是把 COMMAND 的写从 restore 里搬进一个有判据、
        # 有脚印、能单独调用的函数（冷启动后没有驱动 claim 时 Command=0x0000，BAR0 整片读回
        # 0xffffffff，与掉卡不可区分 —— 见 0.4.0 的 CHANGELOG）。
        full = inspect.getsource(self.mod)
        self.assertEqual(full.count("os.O_RDWR"), 3, "config write budget changed")
        self.assertNotIn("0x04", inspect.getsource(self.mod.restore),
                         "COMMAND 的写必须只在 ensure_memory_enable 里：两处写点迟早有一处漏掉闸门")
        self.assertIn("Memory Space Enable", full,
                      "memory enable 这条判据的来由必须留在代码里，不许只剩一行写")

class WakeTests(unittest.TestCase):
    """`BAR0 读回全 0xff` 的第一处置是打开 memory decode，不是判卡已死。

    2026-11-21：11 条熔丝烧成功之后拔电冷启动，`lspci` 还列着设备（枚举缓存），BAR0 整片
    `0xffffffff`，`burntool status` 一片 `0xffffffff` —— 差一步就把一次成功烧写记成"卡死了"。
    真因是 Command=0x0000：这次 boot 没有任何驱动 claim 这张卡，Memory Space Enable 从来没被
    置起来。配置空间一直答 `10de:20c2`，这就是两者的分岔点。
    """

    def setUp(self):
        from ga100vfi import bar0 as mod
        self.mod = mod

    def _run(self, cmds, bar0_word=0x170000A1):
        written = []

        class Fd:
            pass

        with patch.object(self.mod, "card_answers_config",
                          lambda bdf=None: (0x10DE, 0x20C2)), \
                patch.object(self.mod, "command_reg",
                             side_effect=lambda bdf=None: cmds.pop(0)), \
                patch.object(self.mod, "bar0_first_word",
                             lambda bdf=None: bar0_word), \
                patch.object(self.mod.os, "open", lambda *a, **k: Fd()), \
                patch.object(self.mod.os, "close", lambda fd: None), \
                patch.object(self.mod, "_pwrite",
                             lambda fd, data, off: written.append((off, data))):
            r = self.mod.ensure_memory_enable("0000:01:00.0")
        return r, written

    def test_sets_bit1_and_proves_bar0_then_readable(self):
        r, written = self._run([0x0000, 0x0002])
        self.assertEqual((r["before"], r["after"], r["changed"]), (0x0000, 0x0002, True))
        self.assertEqual(written, [(0x04, b"\x02\x00")], "只能写 COMMAND 的那一位")
        self.assertTrue(r["bar0_ok"])

    def test_idempotent_when_already_enabled(self):
        r, written = self._run([0x0007, 0x0007])
        self.assertEqual(written, [], "已开就不该再写配置空间")
        self.assertFalse(r["changed"])
        self.assertTrue(r["bar0_ok"])

    def test_all_ones_config_is_reported_as_gone_not_unenabled(self):
        """`0xFFFF` 的 COMMAND 只能是卡不答 —— 这种形状不能"wake"，要按掉卡处置。"""
        r, written = self._run([0xFFFF])
        self.assertIsNone(r)
        self.assertEqual(written, [])

    def test_bar0_still_all_ones_after_enable_is_reported(self):
        r, _ = self._run([0x0000, 0x0002], bar0_word=0xFFFFFFFF)
        self.assertFalse(r["bar0_ok"], "开了 decode 还读回全 1：这不是 decode 问题，得往掉卡那侧走")


class BenchFlowScriptShape(unittest.TestCase):
    """今天人肉走过、现在写成脚本的那几道闸，必须能在源码层被钉住。

    断言的是**形状**（谁在谁之前、拒绝走哪条出口），不是输出文案：文案可以改，顺序错了
    就是又一次事故。与 `test_the_official_firing_entry_has_no_bar0_before_the_shot` 同源。
    """

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(__file__).resolve().parents[2] / "bench"

    @classmethod
    def src(cls, name):
        text = (cls.dir / name).read_text(encoding="utf-8")
        return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))

    def test_early_sh_reads_row18_without_ever_becoming_a_writer(self):
        """early 档是**读**相位刻度，不是第二个烧写路径。

        它开的是本台架唯一造成过整机死机的通路（BAR0），所以三条必须钉住：在场判断排在 capture
        之前（红线 1）；基线按卡身份分文件（换卡不许拿别人的 row 18 当参照）；脚本里不许出现任何
        写动作。
        """
        code = self.src("early.sh")
        self.assertLess(code.find("present ||"), code.find("capture -o"),
                        "卡不在场时一个 BAR0 都不开：在场判断必须排在 capture 前面")
        self.assertIn("ff-baseline-", code)
        self.assertIn("card_key", code)
        for banned in ("CMD_PROGRAM", "burn plan", "arm", " wake"):
            self.assertNotIn(banned, code, f"early.sh 里出现了写动作 {banned!r}")
        # `burn.py capture` 是按 -o 的后缀决定写 JSON 还是 rows.txt 的：后缀错了不会报错，
        # 只会静默落成文本，然后读的一方 json.load 报 "Extra data" —— 症状像"卡不配合"。
        import re as _re
        for arg in _re.findall(r'capture -o "([^"]+)"', code):
            self.assertTrue(arg.endswith(".json"),
                            f"{arg}: capture 的 -o 后缀不是 .json（临时名也要以 .json 结尾）")

    def test_early_sh_compare_reports_the_real_reason_when_the_card_is_gone(self):
        """批末卡死了，early 档要说"卡已离链"，不能说"没有基线"。

        2026-11-26 05:18 那批真踩到了：`compare` 用**当时**算出的卡身份去找基线，而卡已死 → 身份
        算不出 → `key=unknown` → 报"本电源周期没有本卡基线"，可基线（487 行）就躺在 `state/` 里。
        两条都判 `nojudge`，但下一步完全不同：一条要你去补基线，另一条就是那一发的现场结论。
        所以来源与顺序都要钉住：基线路径先按 `baseline` 那一步写下的指针；在场判断排在报"没基线"
        之前（卡不在了，先说这个）。
        """
        code = self.src("early.sh")
        self.assertIn('"$STATE/current"', code, "baseline 要把基线路径写成指针")
        self.assertLess(code.find('"$STATE/current"'), code.find("card_key 2>/dev/null"),
                        "compare 必须先读指针，卡死了才算身份（算不出来就别拿它当结论）")
        self.assertLess(code.find("批末卡已离链"), code.find("本电源周期没有本卡基线"),
                        "卡离链这句要排在「没基线」前面，否则死卡的那批永远报成「忘了抓基线」")

    def test_prefire_blanket_sleep_is_two_seconds_not_twentyfive(self):
        """开火前那段静置的默认值必须是 2 s（2026-11-26 操作员指令），不许悄悄爬回 25。

        这段静置的历史理由（"冷上电第一发剖面是歪的"）现在由两个东西接管：预热发负责吸收冷态第一发，
        `stretch_us` 负责把冷偏**量出来**。既然能量出来，就不该再用一个没测过的固定余量去盖它 —— 尤其
        是一个电源周期只买得到 2 发的时候，25 s/周期是纯税。测试钉的是默认值，不是那句注释。
        """
        code = self.src("crack.sh")
        self.assertIn("CARD_SETTLE_S=${CARD_SETTLE_S:-$(SP card_settle_s)}", code,
                      "开火前静置不再从规程取值：它会变成第 6 个出处（那是 CARD_SETTLE_S=2 被入口层"
                      "写死的 25 盖掉那一天的形状）")
        self.assertEqual(2, shot_policy.get("card_settle_s"),
                         "静置默认值不再是 2 s：那意味着每批又多交 23 s 的税")
        self.assertNotIn("CARD_SETTLE_S=25", code, "25 s 那个没测过的余量又爬回来了")
        self.assertIn('sleep "$CARD_SETTLE_S"', code, "静置还是得真的 sleep（谓词门是另一件事）")

    def test_the_firing_path_still_never_opens_bar0_before_the_shot(self):
        """0.5.9 加了 early 档，但**没有**把它塞进发前路径：那是 11-21 两次"板子一发没打、
        判决机却挂了"的形状。基线由人（或 r_go 那类体检脚本）在卡健康时抓一次。"""
        code = self.src("crack.sh")
        self.assertNotIn("early.sh", code.split("== 2.9")[0],
                         "发前路径里出现了 early.sh：发前一次 BAR0 都不开")

    def test_实发数与判决数对不上时必须补记(self):
        """板子 tries 比台账行数多出来的那些发，要在批末补成 `nojudge` 行。

        2026-11-26：tries=7、判决 6、`顺序` 里 6 行 —— 台账上"打过 6 发"是假的，实际打了 7 发。
        少记那一发不只是数字难看：它正是"打过没人判还继续打"那个档案形状（lost 之后补一发）的第一手证据。
        """
        code = self.src("crack.sh")
        self.assertIn("board_tries > len(rows)", code, "批末不再核对【板子发数 vs 判决行数】")
        self.assertIn('"nojudge"', code, "补记的判决词必须与代理那侧同名，台账才只有一种词表")
        self.assertIn("nojdg", code, "跨轮总表要有 nojudge 那一列，否则补记的行只进合计、看不出是哪一发")

    def test_revive_sh_pulses_reset_only_when_nothing_is_being_judged(self):
        """卡离链后的救援：允许花一次 PERST 脉冲，但不许在判决进行中拉复位，也不许碰 BAR0。

        顺序就是这条测试的全部内容：战役检查排在 `k` 之前。反过来的话，救援会把正在判决的那一发
        弄成假 `lost` —— 那是往台账里注水。
        """
        code = self.src("revive.sh")
        self.assertLess(code.find("战役"), code.find('"$CON" k'),
                        "战役是否在跑的检查必须排在 PERST 脉冲之前")
        self.assertNotIn("burn.py", code.replace("ga100vfi.burn", "burn.py"))
        self.assertIn("rescan", code)
        self.assertIn("exit 4", code, "请不回来要给出可区分的退码（=该断电了，不是失败了）")
        # 脉冲是否落地必须有凭证：第一次跑这一步我把 `timeout 20` 套在 console.sh 外面，拿到 124
        # 就当"叫不回"，而 124 只是超时被我设进了 console.sh 的重试预算里。所以既要认板子的
        # `released`，也要把超时按 console.sh 的预算算出来，两个都得在代码里。
        self.assertIn("CON_GREP=released", code, "'k' 的答复判据必须是 released，否则脉冲落地无凭证")
        self.assertLessEqual(code.find("BUDGET=$(("), code.find('timeout "$BUDGET"'),
                             "给 console.sh 的超时必须由预算算出来，不能拍脑袋")
        self.assertIn("exit 5", code, "脉冲未证实要单独一个退码（≠卡叫不回）")

    def test_burn_sh_gates_in_order(self):
        code = self.src("burn.sh")
        at = {k: code.find(k) for k in (
            "ga100vfi.burn --bdf \"$BDF\" wake",
            "goldilocks window: OPEN",
            "crosscheck",
            "burn \"$SNAP\" \"$PLAN\" --rows",
            "diffrows")}
        for k, v in at.items():
            self.assertGreater(v, 0, f"bench/burn.sh 里缺 {k}")
        self.assertLess(at['ga100vfi.burn --bdf "$BDF" wake'], at["crosscheck"],
                        "BAR0 全 0xff 的第一处置是 wake；顺序倒了就又会把成功判成死卡")
        self.assertLess(at["goldilocks window: OPEN"], at['burn "$SNAP" "$PLAN" --rows'],
                        "不在窗口里不许 PROGRAM")
        self.assertLess(at["crosscheck"], at['burn "$SNAP" "$PLAN" --rows'])
        # 每次真写之后都必须独立重读逐行核对。**不能**比 `burn …` 与 `diffrows` 两个 token 的首次
        # 出现位置：核对逻辑收在 `snapdiff()` 这个函数里，函数体在文件前半部，按位置比会把这条
        # 判据误判成"顺序倒了"（2026-11-26 踩到）。逐次检查才对：每一次真写之后必须跟一次核对。
        writes = [m.start() for m in re.finditer(r'-m ga100vfi\.burn --bdf "\$BDF" burn ', code)]
        self.assertEqual(2, len(writes), "试烧与续烧这两次真写都要在脚本里")
        for i, w in enumerate(writes, 1):
            self.assertGreater(code.find("snapdiff", w), 0,
                               f"第 {i} 次真写之后没有独立重读逐行核对")
        self.assertIn("read -r", code, "不可逆操作前必须有人确认，脚本不许自己往下烧")
        self.assertIn("结论：只有计划内的行按记录改变", code,
                      "核对判据必须是『只改了计划内的行』，不是『我改的那几行变了』")

    def test_postboot_sh_does_not_write_the_card_beyond_decode(self):
        """冷启动后只做三件事：wake、读、比。`classify` 会做一次 DSOV 回写探针 —— 这里不许用。"""
        code = self.src("postboot.sh")
        self.assertIn("ga100vfi.burn --bdf \"$BDF\" wake", code)
        self.assertIn("classify import SHADOW", code, "影子寄存器必须直接读，别顺手做写探针")
        self.assertNotIn("ga100vfi classify", code,
                         "ga100vfi classify 含 DSOV 回写；验证阶段不该再写这张卡")
        self.assertIn("diffrows", code, "『熔丝保住了没』必须是逐行比，不是看一眼 CRC")

    def test_crack_sh_checks_the_effective_i2c_speed(self):
        """波形配方是 1 MHz 的算术；总线自己降级过，所以闸门读板子报的**实际**速率。"""
        code = self.src("crack.sh")
        self.assertIn('EXPECT_BAUD=${EXPECT_BAUD:-1000000}', code)
        self.assertIn('get("board",{}).get("baud"', code,
                      "必须取 board.baud（实际生效值），取 net 里的请求值等于没检查")

    def test_crack_sh_prewarm_shot_is_judged_and_gates_the_batch(self):
        """预热发打的就是本批坐标 —— 它**必须**被判决，而且它的判决是开正式批的闸门。

        第一版设计是"排在代理起来之前，天然没人判"，写完即推翻：万一那一发就是唯一一次命中，
        没人判 = 交给超时 = 拿命中换一批 under。这里钉四条，防止有人为了省 20 s 把闸门拆回去：
          1) 预热有自己的代理与自己的日志（正式批的 agent.log / 坐标清点必须干净）；
          2) 波形逐参数等于正式批，尤其是 `wait=$WAIT`（settle 缩短就不是在预热同一个状态）；
          3) 只有判决 `under` 才继续；
          4) 这一段绝不按 PEX_RST（真命中之后按一下就是把熔丝链重放一遍）。
        """
        code = self.src("crack.sh")
        pre = code.split('== 3. 判决代理 ==')[0]
        self.assertIn("agent-prewarm.log", pre, "预热发要有自己的判决日志")
        self.assertIn("ga100vfi_agent", pre, "预热发必须有代理判，不能盲打")
        self.assertIn("wait=$WAIT", pre, "预热的 settle 必须与正式批一致")
        self.assertNotIn("wait=300", pre, "预热不许为了快点回来把 settle 改短")
        self.assertRegex(pre, r'case\s+"\$PWV"\s+in', "必须有判决词闸门")
        self.assertRegex(pre, r"\sunder\)", "只有 under 放行")
        # 判决行的取法：`grep -oE '[^\n]*'` 在 ERE 括号里是"排除反斜杠和 n 两个字符"，不是"排除换行"，
        # 于是带 n 的字段（unreachable / 任何后加的词）会把匹配咬断，PWV 变空 → 整批被误拦。
        # 用 sed 的贪婪 `.* -> ` 取最后一个箭头，行为与既有解析一致。
        self.assertIn("sed -n 's/.* -> \\([a-z_]*\\).*/\\1/p'", pre,
                      "取判决词要用 sed，不用 [^\\n] 那种看着像排除换行的写法")
        # 闸门放行之后才轮到 config 检查；顺序倒了会在"卡已掉链"时先打印误导信息
        self.assertLess(pre.index('case "$PWV"'), pre.index("预热后 config="), "先问判决，再看总线")
        self.assertNotIn('"j"', pre.split("== 2.9")[1], "预热段不许按 PEX_RST")
        self.assertNotIn("printf 'J'", pre.split("== 2.9")[1])
        self.assertIn("PREWARM_WAIT=${PREWARM_WAIT:-$(SP prewarm_wait_s)}", code,
                      "预热等待上限要从规程取值（这里再留一份字面量 = 第 6 个出处）")
        self.assertGreaterEqual(shot_policy.get("prewarm_wait_s"), 60,
                                "预热发要走完 settle + 读 + 复核；比 60 s 短就是在把唯一一次可能的命中"
                                "判给超时")

    def test_run_crack_sh_is_a_shell_with_no_geometry_of_its_own(self):
        """几何只能有一个出处。外壳再留一份默认值，就会有『我传的 PW 被改成别的』这种一天。"""
        code = self.src("run_crack.sh")
        for var in ("PW", "VD", "MINV", "MAXV", "DELAYS", "TD", "PULSES", "EXPECT_FW"):
            self.assertNotIn(f"{var}=${{{var}:-", code, f"run_crack.sh 不许自带 {var} 默认值")
        for reach in ("bar0", "mmap", "resource0"):
            self.assertNotIn(reach, code, f"外壳不碰 BAR0：{reach}")
        self.assertIn("bench/crack.sh", code)

    def test_card_probe_reads_command_register_without_writing(self):
        """『BAR0 全 0xff』的第一判据是 Command bit1；体检脚本自己不写配置空间。"""
        code = self.src("card_probe.sh")
        self.assertIn("setpci -s $BDF 4.w", code, "必须看 Command 寄存器")
        self.assertIn("wake", code, "Command=0 的形状必须给出 wake 这条处置")
        self.assertNotIn("4.w=", code, "体检脚本只读：Command 的写归 ga100vfi.burn wake")


class PageClaims(unittest.TestCase):
    """页面上的每句话都是操作程序的一部分：通道写错，人就照着错的那句去查。

    触发这一条的是 2026-11-26 的一句"未运行：判决机上跑 host/ga100vfi_agent.py，**经串口连本板**"。
    两处错：判决方与板子之间只有 WiFi/HTTP（`@A` 快照 + `/api/req`），串口是**人**的逃生舱
    （`bench/console.sh`）；而"未运行"把"没收到心跳"当成了事实 —— 板子其实分得清"本次开机以来从
    没见过"（`seq=0`）与"见过之后断了"（`seq>0`），这两种要查的东西完全不同（前者是没起 agent，
    后者通常是 agent 退出或**板子先重启**，本台架为这类混淆查过串口）。
    """

    def setUp(self):
        self.page = (Path(__file__).resolve().parents[2] / "main" / "webui.html").read_text(
            encoding="utf-8")

    def test_judge_channel_is_http_not_serial(self):
        seg = self.page.split("let peerLine;")[1].split("$('peer')")[0]
        self.assertIn("@A", seg, "判决方的可观测依据只有 @A 心跳，必须写在页面上")
        self.assertIn("不需要串口", seg, "要显式否认串口，否则人会去查线")
        # 整页（去掉注释行 —— 注释里允许引用被撤回的旧句子）里不许再出现"经串口"这种通道断言。
        # 控制台那一格的 aria-label 是"串口命令"，那是给人用的命令集名字，不算通道断言。
        visible = "\n".join(l for l in self.page.splitlines() if not l.strip().startswith("//"))
        self.assertNotIn("经串口", visible)
        self.assertNotIn("判决机上跑 host/ga100vfi_agent.py", visible)

    def test_absent_judge_distinguishes_never_seen_from_dropped(self):
        seg = self.page.split("let peerLine;")[1].split("$('peer')")[0]
        self.assertIn("!peer.seq", seg, "seq=0（从没见过）与 seq>0（见过又断了）必须分开说")
        # 没有心跳时固件把 age_s 填成 -1：拿它渲染就得到"-1s 前收到快照"这种假数
        self.assertNotIn("${peer.age_s}s 前收到快照", seg.split("} else {")[-1])

    def test_the_expiry_window_belongs_to_the_firmware_not_the_page(self):
        """页面不许自己抄一份过期秒数。

        0.5.6 的第一版这里写的是 `peer.present && peer.age_s <= 5`，而固件的 `present` 是
        "本次开机以来收到过"的闩锁：代理被杀之后板子照样回 `present:true / age_s:6`，于是这一格
        显示"在线" —— 比原来的"未运行"更毒，因为它说的是反话。过期门只能有一个主人（peer.h 的
        PEER_STALE_US），页面读 `present`；两边各存一份"5"就会各改各的，还看起来一致。
        """
        seg = self.page.split("let peerLine;")[1].split("$('stop')")[0]
        self.assertNotIn("age_s <= ", seg, "过期判定必须在固件里，页面只读 present")
        self.assertIn("const judging = peer.present;", seg, "开火按钮的依据必须就是那个过期门")
        peer_h = (Path(__file__).resolve().parents[2] / "main" / "peer.h").read_text(
            encoding="utf-8")
        match = re.search(r"#define\s+PEER_STALE_US\s+INT64_C\((\d+)\)", peer_h)
        self.assertIsNotNone(match, "固件里必须真有这个过期门（不然页面的 present 从哪来）")
        stale_s = int(match.group(1)) / 1_000_000
        # 两侧的比例也必须钉住：心跳比窗口慢就等于长期"未运行"，快几十倍就是在毛刺窗口里刷请求
        self.assertLessEqual(VerdictAgent.PEER_EVERY_S * 4, stale_s,
                             f"心跳 {VerdictAgent.PEER_EVERY_S}s 对 {stale_s}s 的窗口余量不足")
        self.assertGreaterEqual(VerdictAgent.PEER_EVERY_S, 0.2,
                                "心跳也不许快到在波形附近反复插队")
        self.assertEqual(VerdictAgent.PEER_STALE_S, stale_s,
                         "代理里的'半个过期周期'用的必须是同一个数")

    def test_a_stale_snapshot_still_shows_why_the_judge_stopped(self):
        """陈旧时必须仍然渲染 info：代理死了，halt 是它留下的唯一现场说明。

        这是固件那侧的契约（peer.c 用 `seen` 而不是 `present` 决定 info 给不给），页面这边要保证
        不把"陈旧"实现成"清空"。两条各查一半，缺一半就退化成"未运行 + 什么都没有"。
        """
        peer_c = (Path(__file__).resolve().parents[2] / "main" / "peer.c").read_text(
            encoding="utf-8")
        render = peer_c.split("void peer_render", 1)[1]
        self.assertIn("seen ? info : \"null\"", render,
                      "info 必须按'收到过快照'给，不能按'心跳没过期'给")
        seg = self.page.split("let peerLine;")[1].split("$('peer')")[0]
        self.assertIn("if (peer.info)", seg, "陈旧分支之后仍要贴最后一份快照")

    def test_page_reports_the_code_beside_the_millivolt(self):
        """VOUT_COMMAND 的 LSB 是 6.25 mV：只显 mV 的页面会把三发重复看成一次搜索。"""
        seg = self.page.split("let campaignLine")[1].split("$('campaign-status')")[0]
        self.assertIn("v_code", seg)
        self.assertIn("codes_tried", seg)

    def test_post_shot_status_registers_show_nack_as_nack(self):
        """可选寄存器"没读到"不等于"读到 0"：NACK 必须显式写出来。"""
        seg = self.page.split("if (b.last)")[1].split("$('status')")[0]
        self.assertIn("status_word", seg)
        self.assertIn("status_vout", seg)
        self.assertIn("(nack)", seg, "NACK 要显式标记，否则没实现的东西会被读成『没有故障』")


class PresenceWatch(unittest.TestCase):
    """与判决无关、按时间自己跑的 presence 看门狗。

    实测形状（2026-11-21 05:44 那批）：第 1 发判 `under` 之后卡自己从总线消失，代理只在
    awaiting_verdict 时才醒，板子于是把第 2 发的波形打在一张看不见的卡上（tries=2）。
    判决驱动的停手救不了这一发 —— 只有时间驱动的看门狗能救。
    """

    def _agent(self, ids, grace=3.0):
        agent = VerdictAgent('http://example.invalid', '0000:01:00.0')
        agent.presence_grace_s = grace
        seq = list(ids)
        agent.config_id = Mock(side_effect=lambda: seq.pop(0) if seq else ids[-1])
        agent.stop_board = Mock()
        return agent

    def test_transient_absence_inside_the_grace_window_must_not_stop(self):
        """每发板子自己拉 PERST#，那 ~1 s 里卡是合法地不在总线上。采到一次全 1 就停火 = 掐死每一批。"""
        a = self._agent([0xFFFFFFFF, 0xFFFFFFFF, 0x20c210de, 0xFFFFFFFF])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(a.presence_sample(True, now=0.0))
            self.assertFalse(a.presence_sample(True, now=2.0))      # 还没到 3 s
            self.assertFalse(a.presence_sample(True, now=2.5))      # 回来了 → 计时清零
            self.assertFalse(a.presence_sample(True, now=3.0))
        a.stop_board.assert_not_called()

    def test_一发在飞时看门狗用宽的宽限期(self):
        """看门狗防的是"往死卡上打下一发"，不是"抛弃已经打出去的那一发"。

        2026-11-26 08:23/08:27 两批实测：第 3 发（ladder 往深走到 vd=363）刚打出去，代理就以
        「卡连续 3.0s/3.3s 不在 PCIe 上」停战，而 presence 记录器同一时刻看到卡 +2~+12 s 仍在场
        （正常重枚举）⇒ 区间下沿唯一可能给 over 的那一发永远成 nojudge。
        """
        a = self._agent([0xFFFFFFFF] * 8)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(a.presence_sample(True, now=0.0, in_flight=True))
            self.assertFalse(a.presence_sample(True, now=3.5, in_flight=True),
                             "一发在飞时不许用 3 s 的空闲档把它抛弃")
            self.assertFalse(a.presence_sample(True, now=9.0, in_flight=True))
            self.assertTrue(a.presence_sample(True, now=12.0, in_flight=True),
                            "宽限也不能无限：卡真死了要停，别把整批吊死")
        # 空闲档（没有发在飞）必须还是 3 s：05:44 那批的形状是"卡没了板子还在连打"
        b = self._agent([0xFFFFFFFF] * 8)
        b.last_phase = "idle"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(b.presence_sample(True, now=0.0))
            self.assertTrue(b.presence_sample(True, now=3.5),
                            "空闲档被放宽 = 把 05:44 那个形状又放回来")

    def test_在飞与否看板子的phase而不是代理手里的判决(self):
        """pending 是"判决算好了待盖章"，那一发在飞时它是空的 —— 所以只能用板子的 phase。"""
        a = self._agent([0xFFFFFFFF] * 8)
        for phase in ("executing", "settle", "waiting"):
            b = self._agent([0xFFFFFFFF] * 8)
            b.last_phase = phase
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(b.presence_sample(True, now=0.0))
                self.assertFalse(b.presence_sample(True, now=3.5),
                                 f"phase={phase} 时这一发还没人判，不许用 3 s 抛弃它")
        from ga100vfi_agent import IN_FLIGHT_PHASES
        self.assertEqual(set(IN_FLIGHT_PHASES), {"executing", "settle", "waiting"})
        # phase 名要真是板子会报的那些（改了板子字符串而代理没跟，看门狗就退回空闲档）
        src = (Path(__file__).resolve().parents[2] / "main" / "campaign.c").read_text(encoding="utf-8")
        for phase in IN_FLIGHT_PHASES:
            self.assertIn(f'phase_locked("{phase}")', src, f"板子不再报 phase={phase}")

    def test_sustained_absence_stops_and_asks_the_board_to_stand_down(self):
        a = self._agent([0xFFFFFFFF] * 8)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(a.presence_sample(True, now=0.0))
            self.assertTrue(a.presence_sample(True, now=3.5))
        a.stop_board.assert_called_once_with()
        self.assertTrue(a.lost_stopped)
        self.assertIn("不在 PCIe", out.getvalue())

    def test_a_platform_that_never_saw_the_card_does_not_stop_every_batch(self):
        """非 Linux / 测试台上 config 节点根本打不开（config_id 返回 None）。那种形状不是"卡死了"，
        看门狗如果认它，每一批都会在第二发被自己掐死。"""
        a = self._agent([None] * 8)
        with contextlib.redirect_stdout(io.StringIO()):
            for t in (0, 1, 5, 30):
                self.assertFalse(a.presence_sample(True, now=float(t)))
        a.stop_board.assert_not_called()

    def test_node_being_ripped_after_being_seen_is_absence(self):
        a = self._agent([0x20c210de, None, None, None, None])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(a.presence_sample(True, now=0.0))      # 在场
            self.assertFalse(a.presence_sample(True, now=1.0))      # 节点没了，开始计时
            self.assertTrue(a.presence_sample(True, now=5.0))
        a.stop_board.assert_called_once_with()

    def test_watchdog_is_idle_while_nothing_is_firing(self):
        a = self._agent([0xFFFFFFFF] * 6)
        with contextlib.redirect_stdout(io.StringIO()):
            for t in (0, 2, 5, 8):
                self.assertFalse(a.presence_sample(False, now=float(t)))
        a.stop_board.assert_not_called()

    def test_the_watchdog_never_opens_bar0(self):
        """红线 1：卡看不见的时候更不许去摸 BAR0（毒态下读 BAR0 挂死整机）。"""
        a = self._agent([0xFFFFFFFF] * 4)
        with patch.object(agent_module, 'Bar0') as bar0, \
                contextlib.redirect_stdout(io.StringIO()):
            a.presence_sample(True, now=0.0)
            a.presence_sample(True, now=9.0)
        bar0.assert_not_called()

    def test_run_forever_uses_the_watchdog_while_it_waits(self):
        """看门狗必须由等待循环喂：05:44 那次就是等判决时没人看总线。"""
        a = VerdictAgent('http://example.invalid', '0000:01:00.0')
        a.step = Mock(return_value=False)
        a.running = True
        a.presence_sample = Mock(side_effect=[False, True])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent_module.run_forever(a, poll=0.2, max_lost=1), 2)
        self.assertEqual(a.presence_sample.call_count, 2)


class JudgeHeartbeat(unittest.TestCase):
    """判决方必须让板子的页面知道它活着（0.5.6）。

    触发这件事的是 2026-11-26：crack.sh 的代理明明在判每一发，WIFI 页面却显示"判决方未运行"，
    而那句話还写着"经串口连本板"。板子那侧的 `@A` 通道 0.5.0 就实现好了（`main/peer.c` +
    `main.c:peer_cmd`），缺的只是主机侧从来没发过。这里钉的是**协议形状**（板子校验很严，整包会
    被拒）与 **best-effort 语义**（心跳失败绝不能变成不判决）。
    """

    PEER_MAX = 256          # peer.c: PEER_INFO_MAX，且 ascii_json_object 逐字节查可打印 ASCII

    def _agent(self, **kw):
        agent = VerdictAgent('http://example.invalid', '0000:01:00.0', first_read_delay=0, **kw)
        agent.status = Mock(return_value=waiting())
        agent.gpu_verdict = Mock(return_value='under')
        agent.feed = Mock()
        agent.posts = []
        agent.post_cmd = Mock(side_effect=lambda line: agent.posts.append(line) or "peer ok")
        return agent

    def _assert_board_would_accept(self, payload: str):
        """把 peer.c 的 ascii_json_object 在测试里跑一遍，而不是相信它会接受。"""
        self.assertTrue(payload.startswith("{") and payload.endswith("}"), payload)
        self.assertLess(len(payload.encode()), self.PEER_MAX)
        for ch in payload:
            self.assertTrue(0x20 <= ord(ch) <= 0x7E, f"板子会整包拒收：{ch!r} in {payload!r}")
        self.assertEqual(payload.count("{"), payload.count("}"))
        json.loads(payload)                         # 板子原样嵌进 /api/status，必须合法

    def test_heartbeat_is_one_at_A_frame_over_http(self):
        agent = self._agent()
        self.assertTrue(agent.push_snapshot(force=True))
        self.assertEqual(len(agent.posts), 1)
        frame = agent.posts[0]
        self.assertTrue(frame.startswith("@A seq=0 "), frame)     # 板子按 '@A' 分派（main.c:564）
        self._assert_board_would_accept(frame.split(" ", 2)[2])
        self.assertTrue(agent.peer_ok)

    def test_snapshot_carries_what_the_page_cannot_get_from_the_board(self):
        # 板子知道自己的 tries/phase，但它不知道**代理为什么停手**：halt/ledger 只有判决方有。
        agent = self._agent()
        agent.shots, agent.tally, agent.dirty = 4, {"under": 3, "lost": 1}, 2
        agent.last_verdict, agent.stable, agent.halt = "lost", False, "presence"
        snap = agent.snapshot()
        self.assertEqual(snap["halt"], "presence")
        self.assertEqual(snap["state"], "stopped:presence")
        self.assertIn("shots=4", snap["ledger"])
        self.assertIn("dirty=2", snap["ledger"])
        self.assertIn("stable=NO", snap["last"])

    def test_a_long_or_non_ascii_reason_cannot_silence_the_heartbeat(self):
        # 中文停手原因会让整包被板子拒收 —— 页面于是又变成"未运行"，而且是**被静默地**变成。
        # 所以要求：在主机侧消毒，而不是等着被拒。
        agent = self._agent()
        agent.halt = "presence 看门狗停战（config 全 1）"
        agent.last_fail = "restore-failed: 上游桥没起链路 " * 12
        payload = agent.peer_frame().split(" ", 2)[2]
        self._assert_board_would_accept(payload)
        snap = json.loads(payload)
        self.assertNotIn("看门狗", snap["halt"])
        self.assertTrue(snap["state"].startswith("stopped:"))

    def test_overlong_snapshot_drops_fields_in_priority_order(self):
        # 溢出只能由**不截断**的字段造成（last_fail 进 snapshot 时已被削到 20 字符）—— 真会出现的
        # 形态就是 halt 被写成一句长话。丢字段的顺序必须是"装饰先丢，state/halt 最后动"。
        agent = self._agent()
        agent.halt = "poison " + "y" * 400
        snap = agent.snapshot()
        self.assertIn("halt", snap, "state/halt 是'代理还活着吗/为什么停'，最后才许动")
        self.assertIn("state", snap)
        self.assertNotIn("note", snap)
        self.assertNotIn("gates", snap)
        self._assert_board_would_accept(json.dumps(snap, separators=(",", ":")))
        # 类属性不能被改写：否则第一次溢出之后，之后每一份快照都永久少掉同一批字段
        self.assertEqual(agent.PEER_DROP_ORDER, ("note", "gates", "next", "ledger", "last"))

    def test_heartbeat_failure_never_costs_a_verdict(self):
        agent = self._agent()
        agent.post_cmd = Mock(side_effect=OSError("wifi down"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(agent.push_snapshot(force=True))
        self.assertFalse(agent.peer_ok)
        self.assertIn("wifi down", agent.peer_err)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(agent.step())                 # 判决照交
        agent.feed.assert_called_once()

    def test_heartbeat_stays_quiet_during_the_waveform_but_cannot_lie_forever(self):
        agent = self._agent()
        agent.last_phase = "executing"
        self.assertTrue(agent.push_snapshot(force=True))
        self.assertFalse(agent.push_snapshot())                    # 1 s 之内不重发
        agent._peer_t -= 3.0                                       # 距上次 3 s：波形中但快过期
        self.assertTrue(agent.push_snapshot(), "宁可多发一个请求，也不能让页面假显示'未运行'")

    def test_disabled_by_flag(self):
        agent = self._agent()
        agent.peer_every_s = 0.0
        self.assertFalse(agent.push_snapshot(force=True))
        self.assertEqual(agent.posts, [])

    def test_the_agent_does_not_fire_on_the_page_start_request(self):
        """回包里带页面的 op=start 时只许打印。代管开火=绕过 crack.sh 的发前闸门。"""
        agent = self._agent()
        agent.post_cmd = Mock(side_effect=lambda line: agent.posts.append(line)
                              or "req pending op=start&vd=350&max=200")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(agent.push_snapshot(force=True))
        self.assertEqual(len(agent.posts), 1, "不许把页面的请求转成一次真开火")
        self.assertIn("不代管开火", out.getvalue())

    def test_stop_paths_publish_the_halt_reason(self):
        # 停手那一刻页面必须知道是**谁**停的：presence/poison/lost/reacted 各自要查的东西不同。
        for flag, expect in (("lost_stopped", "lost"), ("reacted", "reacted")):
            agent = self._agent()
            setattr(agent, flag, True)
            agent.step = Mock(return_value=False)
            agent.stop_board = Mock()
            agent.presence_sample = Mock(return_value=False)
            with contextlib.redirect_stdout(io.StringIO()):
                code = agent_module.run_forever(agent, poll=0.06, max_lost=1)
            self.assertIn(code, (2, 4))
            self.assertTrue(agent.posts, "停手时没发心跳：页面只会显示过期的在线")
            self.assertIn(f'"halt":"{expect}"', agent.posts[-1])
        agent = self._agent()
        agent.step = Mock(return_value=False)
        agent.presence_sample = Mock(return_value=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent_module.run_forever(agent, poll=0.06, max_lost=1), 2)
        self.assertIn('"halt":"presence"', agent.posts[-1])

    def test_poll_zero_cannot_silence_the_presence_watchdog(self):
        """poll=0 曾让内圈一次都不跑：卡离开总线的保护静默消失，循环还会永久空转。"""
        agent = self._agent()
        agent.step = Mock(return_value=False)
        agent.presence_sample = Mock(return_value=True)
        with contextlib.redirect_stdout(io.StringIO()):
            code = agent_module.run_forever(agent, poll=0.0, max_lost=1)   # 抬到下限，必须停
        self.assertEqual(code, 2)
        self.assertTrue(agent.presence_sample.called, "看门狗一次都没跑 = 保护被 poll=0 关掉了")

    def test_seq_continues_from_what_the_board_already_has(self):
        """换一个 agent 进程不许把"最后快照 #N"数回去。

        板子的 peer_store 不校验单调（原样存），所以倒退不会被挡 —— 只会让操作员看见 #5 变 #3，
        而那个征状在他眼里意味着"板子重启过"，恰好是这两态里另一态的征状。0.5.7 上板自证第 5 节
        实测到的就是这个倒退。
        """
        agent = self._agent()
        agent.status = Mock(return_value={"campaign": {}, "peer": {"seq": 5, "present": True}})
        self.assertEqual(agent.seed_peer_seq(), 6, "板子上那份是 #5，下一份必须是 #6")
        self.assertTrue(agent.push_snapshot(force=True))
        self.assertTrue(agent.posts[0].startswith("@A seq=6 "), agent.posts[0])
        self.assertTrue(agent.push_snapshot(force=True))     # 本实例自己往上数
        self.assertTrue(agent.posts[1].startswith("@A seq=7 "), agent.posts[1])

    def test_seq_seeding_never_blocks_the_heartbeat(self):
        # 种子读不到（板子刚起、WiFi 抖、测试台）就从 0 数 —— 心跳本身比序号对不对更值钱。
        agent = self._agent()
        agent.status = Mock(side_effect=OSError("board not answering"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent.seed_peer_seq(), 0)
            self.assertTrue(agent.push_snapshot(force=True))
        self.assertTrue(agent.posts[0].startswith("@A seq=0 "), agent.posts[0])

    def test_post_cmd_reaches_the_console_endpoint(self):
        # /api/cmd 是**人**用的那个入口：心跳走同一条路，才不会出现"只有脚本看得见代理"。
        agent = self._agent()
        agent.post_cmd = VerdictAgent.post_cmd.__get__(agent, VerdictAgent)   # 用回真方法
        seen = {}

        class Resp:
            def read(self): return b"peer ok"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(request, timeout=None):
            seen["url"] = request.full_url
            seen["data"] = request.data
            seen["timeout"] = timeout
            return Resp()

        with patch.object(agent_module.urllib.request, "urlopen", fake):
            self.assertEqual(agent.post_cmd("@A seq=1 {}"), "peer ok")
        self.assertTrue(seen["url"].endswith("/api/cmd"), seen)
        self.assertTrue(seen["data"].startswith(b"@A "))
        self.assertEqual(seen["timeout"], agent.timeout, "心跳也必须带超时：卡死的 HTTP 不能挂住代理")


class ShotPolicy(unittest.TestCase):
    """每发前后的操作规程必须有唯一出处，而且必须能在代码里找到落点。

    为什么单独一组测试：发数打到几百发之后，「打完之后我们到底做了什么」这件事仍然要靠人回忆，
    而回忆的代价今天付了两次 —— CARD_SETTLE_S 被判决机上那层不在版本库里的入口写死成 25（改了不
    生效）、批前 487 行 FF capture 被我随手塞进开火脚本（每个电源周期从此都在第 2 发死）。把规程
    写进 host/ga100vfi/shot_policy.py 之后，这组测试负责让「散着的默认值」重新变成编译期事故。
    """

    ROOT = Path(__file__).resolve().parents[2]

    def test_代理的默认值全部来自规程(self):
        ag = VerdictAgent('http://example.invalid', '0000:01:00.0')
        for attr, key in (('max_lost', 'max_lost'),
                          ('first_read_delay', 'first_read_delay_s'),
                          ('confirm_reads', 'confirm_reads'),
                          ('stable_s', 'stable_s'),
                          ('budget_s', 'verdict_budget_s'),
                          ('max_bar0_sessions', 'bar0_sessions_per_shot'),
                          ('presence_grace_s', 'presence_grace_s')):
            self.assertEqual(getattr(ag, attr), shot_policy.get(key),
                             f"VerdictAgent.{attr} 没从 shot_policy.{key} 取：又出现第二个出处了")

    def test_仓库里不许在规程之外写死每发的节奏(self):
        bad = shot_policy.check_scripts(str(self.ROOT))
        self.assertEqual([], bad, "这些脚本把节奏常量写死在规程之外：\n" + "\n".join(bad))

    def test_crack_sh确实是从规程取值而不是干脆删掉(self):
        # check_scripts 只保证"别处没写字面量"；这里保证 crack.sh 真的去取了（否则它会安静地
        # 依赖代理的默认值，而默认值改了没人知道）。
        txt = (self.ROOT / 'bench' / 'crack.sh').read_text(encoding='utf-8')
        for key in ('card_settle_s', 'first_read_delay_s', 'confirm_reads', 'stable_s',
                    'prewarm_wait_s', 'bar0_sessions_per_shot', 'wait_card_s', 'prewarm',
                    'capture_min_gap_s', 'settle_wait_ms', 'survivor_halt_s',
                    'halt_read_tries', 'halt_read_gap_s'):
            self.assertIn(f'(SP {key})', txt, f"crack.sh 没有从规程取 {key}")
        # 调用点在，定义也得在：部署器就是靠这条拒绝上线的（改名会让 crack.sh 安静地拿不到值）
        self.assertRegex(txt, r"(?m)^SP\(\)\s*\{", "crack.sh 里那个取值函数的定义没了")

    def test_规程的数必须彼此咬得住(self):
        g = shot_policy.get
        # 判决预算必须容得下"静默下限 + 盖章复读"，否则把 first_read_delay 调大就会把每一发判成
        # lost —— 这类"两个默认值互相咬"的坑必须比现场更早爆炸。
        need = (g('first_read_delay_s') + g('stable_s') * max(0, g('confirm_reads') - 1) + 10)
        self.assertGreater(g('verdict_budget_s'), need)
        if g('bar0_sessions_per_shot'):
            self.assertGreaterEqual(g('bar0_sessions_per_shot'), g('confirm_reads'))
        # 全量 capture 与第一发之间必须留够距离（红线 1 的机制，见 capture_min_gap_s 的 why）
        self.assertGreaterEqual(g('capture_min_gap_s'), 60)

    def test_合法模式不许被参数校验拒掉(self):
        """2026-11-26：`--max-bar0-sessions >= 1` 这条校验把 config-only 模式挡在门外，那一批
        预热代理起不来、正式批没发一发，症状只是"批已结束 +30s"。校验本身也要有测试。"""
        a = agent_module.validated_args(["http://example.invalid", "--max-bar0-sessions", "0"])
        self.assertEqual(0, a.max_bar0_sessions, "0（一发都不读）必须是合法值")
        self.assertEqual(0, a.confirm_reads, "一次都不读时【盖章】这个概念必须一起关掉，而不是判成盖章失败")
        d = agent_module.validated_args(["http://example.invalid"])
        self.assertEqual(shot_policy.get("bar0_sessions_per_shot"), d.max_bar0_sessions,
                         "argparse 又自带了一份默认值")
        self.assertEqual(shot_policy.get("confirm_reads") if d.max_bar0_sessions else 0,
                         d.confirm_reads)
        with self.assertRaises(SystemExit):
            # 会话预算不够盖章：这才是该拒的形状
            agent_module.validated_args(["http://example.invalid", "--max-bar0-sessions", "1",
                                         "--confirm-reads", "2"])

    def test_每条规程都有代码落点(self):
        for name, steps in (('BEFORE_SHOT', shot_policy.BEFORE_SHOT),
                            ('AT_SHOT', shot_policy.AT_SHOT),
                            ('AFTER_SHOT', shot_policy.AFTER_SHOT)):
            self.assertTrue(steps, f"{name} 空了：规程不能只是愿望")
            for kind, text, where in steps:
                self.assertIn(kind, ('must', 'never'), f"{name}: {text}")
                self.assertTrue(text.strip() and where.strip(), f"{name}: 缺文字或缺落点 {text}")

    def test_config_only判决一次BAR0都不开(self):
        """bar0 会话预算=0 ⇒ 连 Bar0() 都不许构造（restore 写 COMMAND 也不算"只是读"）。"""
        ag = VerdictAgent('http://example.invalid', '0000:01:00.0',
                          first_read_delay=0, budget_s=5, max_bar0_sessions=0)
        ag.status = Mock(return_value=waiting())
        ag.present = Mock(return_value=True)

        def boom(*a, **k):
            raise AssertionError("config-only 判决却构造了 Bar0：这就是杀死卡的那只手")

        tok = awaiting_token(waiting())
        with patch.object(agent_module, 'Bar0', boom):
            self.assertEqual('under', ag.evaluate(tok))
        self.assertEqual(0, ag.reads, "config-only 判决的读数计数必须为 0（台账要能看出没盖章）")
        self.assertIsNone(ag.stable, "没读寄存器就不是 stable=yes：它天生未盖章")
        self.assertTrue(ag.present.called)

    def test_config_only判决卡不在场时判lost而不是继续读(self):
        ag = VerdictAgent('http://example.invalid', '0000:01:00.0', first_read_delay=0,
                          budget_s=0.3, poll_s=0.05, max_bar0_sessions=0)
        ag.status = Mock(return_value=waiting())
        ag.present = Mock(return_value=False)
        with patch.object(agent_module, 'Bar0',
                          lambda *a, **k: self.fail("卡都不在场还开 BAR0")):
            self.assertEqual('lost', ag.evaluate(awaiting_token(waiting())))
        self.assertEqual(0, ag.reads)

    def test_冻结配方就是档案里那一发(self):
        """11-21 唯一命中的那发：vd=372 td=-40 vr=750 tr=-20 2x222 gap=150 delay=64 revert=0。

        钉死它是因为判决机外壳以前自带过 REV=1 / SWIN=1200 的默认值：那一忘 export 就不是那一发了，
        而"我们打了 60 发其实打的不是同一发"这种事故从日志里看不出来。
        """
        self.assertEqual(shot_policy.RECIPE, dict(
            PULSES=2, PW=222, GAP=150, TD=-40, VR=750, TR=-20, VD=372, MINV=372, MAXV=372,
            STEP=2, DELAYS=64, MAX=60, SWIN=0, REVERT=0, BATCH=1, PREWARM=1),
            "冻结配方漂了：改它要在决策链里给理由，而不是改这里让测试变绿")
        self.assertIn("export PW=222", shot_policy.recipe_exports())

    def test_判决机外壳是纯透传(self):
        bad = shot_policy.check_entry()
        self.assertEqual([], bad, "判决机外壳又自带配方/节奏了：\n" + "\n".join(bad))
        run = (self.ROOT / "bench" / "judge-entry" / "run.sh").read_text(encoding="utf-8")
        batch = (self.ROOT / "bench" / "judge-entry" / "batch.sh").read_text(encoding="utf-8")
        for text, name in ((run, "run.sh"), (batch, "batch.sh")):
            self.assertNotIn("mig-card2/run.sh", text, f"{name} 还在引用仓库外那份入口层")
            self.assertIn("shot_policy", text, f"{name} 的节律没从规程取")
        self.assertIn("presence7.csv", run, "presence 记录器是'卡活到第几秒'的唯一证据，不能被删")

    def test_开火脚本用生成的配方而不是抄一份(self):
        code = (self.ROOT / "temp" / "r_fast.sh").read_text(encoding="utf-8")
        # 只看真正会执行的行：注释里提这些名字是这件东西的历史，不是第二个出处。
        body = "\n".join(ln for ln in code.splitlines() if not ln.strip().startswith("#"))
        self.assertIn("--recipe", body, "开火脚本又自己抄了一份配方：它会和 shot_policy 漂开")
        self.assertNotIn("/root/mig-card2/run.sh", body, "还在用仓库外那层入口（跨机器 sed 那一天的根源）")
        for literal in ("PW=222", "VD=372", "CARD_SETTLE_S=", "CONFIRM_READS=", "FIRST_READ_DELAY="):
            self.assertNotIn(literal, body, f"开火脚本里又出现了字面量 {literal}")

    def test_survivor闸门就地读窗口(self):
        """卡一直活着时，那唯一一次熔丝读数必须在闸门里当场花掉，不能等批末。

        2026-11-26 07:27:35 第一次出现这个形状（11 发、卡活到最后一发后 +90 s）：板子 07:30:03 停在
        stopped，批末那次 burn status 落在 07:30:3x —— 中间隔着盯梢循环退出、台账、包络门禁、survival
        表，实测 ~30 s，卡在这 30 s 里死了，读回来是 ff ff。零读模式下这是整批唯一一次能读到窗口的
        机会，读晚了等于这批白打。所以：闸门里先 op=stop 再读（读的同时不能有新的坑），批末那一次
        见到落盘读数就跳过。
        """
        code = (self.ROOT / "bench" / "crack.sh").read_text(encoding="utf-8")
        gate = code.split("$(SP survivor_halt_s)")[1].split("CONS_LOST")[0]
        self.assertIn("window-at-halt.txt", gate, "survivor 闸门没有就地读熔丝状态")
        self.assertLess(gate.index("op=stop"), gate.index("window-at-halt.txt"),
                        "必须先停战役再读：读的那 1~2 s 里板子不能正在打下一发")
        self.assertIn("burn", gate, "闸门要读的是熔丝状态（burn status），不是别的东西")
        entry = (self.ROOT / "bench" / "judge-entry" / "batch.sh").read_text(encoding="utf-8")
        self.assertIn("window-at-halt.txt", entry,
                      "批末那次读不认闸门落盘的读数 ⇒ 同一个周期白花两次 BAR0 会话")

    def test_批内里程碑有时间钟(self):
        """起批→预热→op=start 这几步必须有墙钟里程碑，否则"每周期只买到 2 发"永远只能靠猜。

        2026-11-26：07:49:42 起批、07:50:35 战役第一发 executing，中间 50 s 归属不明（只能从文件 mtime
        反推它在预热段）。$SECONDS + date 的 TS 打印是唯一不靠事后考古就能分到每一步的办法。
        """
        code = (self.ROOT / "bench" / "crack.sh").read_text(encoding="utf-8")
        for milestone in ('TS "crack.sh 起来', 'TS "预热发开始', 'TS "预热战役已 park',
                          'TS "预热段结束', 'TS "战役 op=start 之前'):
            self.assertIn(milestone, code, f"批内里程碑缺 {milestone}")
        self.assertIn("${SECONDS}s", code, "里程碑必须带批内累计秒数，光有墙钟看不出花了多久")

    def test_锁检查的串口预算来自规程(self):
        """锁检查那两次 console 往返实测吃掉 38 s，而卡只活 ~60 s —— 预算必须受控并且两处副本一致。

        console.sh 的第一行输出就是它的预算公式：tries × (wait + 1) + 3。监听窗口【无论有没有答复都会
        跑满】，所以 wait=6 时一次"成功"的调用也要 7 s，判前 + 解后两次就是十几到三十几秒。它以前在
        crack.sh 与 console.sh（还有 crack.sh 内嵌的那份兜底副本）各写一份，谁都没被钉住。
        """
        crack = (self.ROOT / "bench" / "crack.sh").read_text(encoding="utf-8")
        for knob in ("console_tries", "console_wait_s", "console_repeat"):
            self.assertIn(f"(SP {knob})", crack, f"crack.sh 没从规程取 {knob}：串口预算又变成写死的")
        for rel in ("bench/console.sh",):
            txt = (self.ROOT / rel).read_text(encoding="utf-8")
            for env_name, knob in (("CON_TRIES", "console_tries"), ("CON_WAIT", "console_wait_s"),
                                   ("CON_REPEAT", "console_repeat")):
                m = re.search(rf"{env_name}=\$\{{{env_name}:-([0-9]+)\}}", txt)
                self.assertIsNotNone(m, f"{rel} 里 {env_name} 的兜底值找不到了")
                self.assertEqual(shot_policy.get(knob), int(m.group(1)),
                                 f"{rel} 的 {env_name} 兜底值与规程 {knob} 不一致（人工直调时会漂）")
        # 内嵌兜底副本也必须在同一预算上（它没有 SP，只能靠测试钉）
        self.assertIn("CON_TRIES=${CON_TRIES:-2}", crack,
                      "crack.sh 内嵌的 console 兜底副本预算与规程不一致")

    def test_孤儿代理要被收掉而不是把整批挡在门外(self):
        """发前那条"已有代理"闸门必须分得清孤儿与在判，否则它会自己饿死自己。

        2026-11-26 07:55:17：crack.sh 死在自己新加的一行上（set -u + 未初始化变量），预热代理被留在
        场上；下一批 r_fast.sh 看到代理在跑就直接 exit 3，白扔掉一张还活着、还在总线上的卡。真正的
        危险是【有 crack.sh 活着】= 那一批正在判决，两个 BAR0 参与者会互相踩；代理活着而发射器死了
        只是事故残留，必须收掉。
        """
        code = (self.ROOT / "temp" / "r_fast.sh").read_text(encoding="utf-8")
        self.assertIn("bash .*crack.sh", code, "闸门不再区分【在判的代理】与【孤儿代理】")
        self.assertLess(code.index("bash .*crack.sh"), code.index("pkill -9 -f 'ga100vfi_ag[ent]'"),
                        "必须先确认没有发射器在跑，再收代理；顺序反了会把正在判的那批的代理杀掉")

    def test_找区间阶段每发必须真读一次(self):
        """MODE 决定这批是【找区间】还是【死磕】；找区间不许用 config-only 判决。

        操作员 2026-11-26：把这张卡当成新卡、忘记历史，按一般流程重新找区间。找区间的判据是 under 与
        over 的分界，而 config-only 只看得见"卡在不在场"，永远给不出 over —— 那种批次跑完，区间边界
        一根都没量到，只是又烧了一个电源周期。反过来（hammer）才该零读。
        """
        exports = {}
        for ln in shot_policy.recipe_exports().splitlines():
            verb, _, rest = ln.partition(" ")
            self.assertEqual(verb, "export", f"配方导出行形状不对：{ln}")
            name, sep, val = rest.partition("=")
            self.assertTrue(sep, f"导出行没有 =：{ln}")
            exports[name] = val
        self.assertIn(shot_policy.MODE, ("hunt", "hammer"), "MODE 只认这两个阶段")
        if shot_policy.MODE == "hunt":
            self.assertLessEqual(int(exports["MINV"]), int(exports["VD"]), "vd 必须落在带内")
            self.assertLessEqual(int(exports["VD"]), int(exports["MAXV"]), "vd 必须落在带内")
            if exports["MINV"] == exports["MAXV"]:
                # 钉死一个坐标 = 把整个周期的发数押在同一格上。板子对 `VD=MINV=MAXV` 豁免去重，
                # 所以代码不会拦我们 —— 拦的只能是理由：没有台账里那一格有命中迹象，就不该钉死。
                self.assertIn("goldilocks", shot_policy.SWEEP_WHY,
                              "把带钉成一个坐标必须有【命中迹象】级别的理由，并写进 SWEEP_WHY")
            self.assertGreaterEqual(
                int(exports["VD"]), shot_policy.MEASURED_LETHAL_MV + int(exports["STEP"]),
                "战役起点至少要比【实测致死格】高一格：落在致死格或更深 ⇒ 首发判不出来，"
                "（2026-11-26 实测：vd=334 首发即死、vd=356 从来没判到）")
            # 预热发的坐标单独钉：它吸收的是【冷态第一发】这个危险状态，不是替阶梯试深。
            self.assertIn("PREWARM_VD", exports,
                          "预热坐标必须由规程导出；它一旦退回成 $VD 的别名，抬起点就等于让预热去试深")
            self.assertGreaterEqual(
                int(exports["PREWARM_VD"]), shot_policy.MEASURED_LETHAL_MV + 2 * int(exports["STEP"]),
                "预热发必须打在【从没丢过卡】的格子上（比致死格至少隔两格）：08:54:22 那批预热首发打死"
                "卡 ⇒ 闸门拒开正式批 ⇒ 整个电源周期 0 判决")
            crack = (self.ROOT / "bench" / "crack.sh").read_text(encoding="utf-8")
            self.assertIn("op=start&vd=$PREWARM_VD", crack,
                          "crack.sh 的预热战役又去打 $VD 了（预热与战役起点必须分开）")
            self.assertGreaterEqual(int(exports["BAR0_SESSIONS"]), 1,
                                    "找区间却不读熔丝块 ⇒ under/over 分不出来，边界量不到")
            # 操作员 2026-11-26："为啥总是 3 发，不应该是打得越多越好" —— 答案：不是上限卡的，是卡
            # ~11 s 就掉线 + max_lost=1 一发即停火（红线，不许放宽）。既然如此，那几发每一发都必须落在
            # 没量过的格子上：BATCH=2 时有 2 发在重复 368，而它的 under 早已有 10+ 发盖章。
            self.assertLessEqual(int(exports["BATCH"]), 1,
                                 "找区间不许在已知 under 的坐标上重复消耗每周期那 ~3 发的预算")
            # 深格子是【在判决之前就死】的：读排在死亡之后 = 整批 0 判决（08:05:33 与 08:18:34 两批）
            self.assertIn("FIRST_READ_DELAY", exports,
                          "hunt 阶段不覆盖读时刻 ⇒ 4 s 的读又落在死亡之后，白扔一个电源周期")
            self.assertLessEqual(int(exports["FIRST_READ_DELAY"]),
                                 shot_policy.get("first_read_delay_s"),
                                 "找区间的读必须不晚于死磕阶段")
            self.assertGreaterEqual(int(exports["FIRST_READ_DELAY"]), 1,
                                    "再早就撞上红线（在 ROM/devinit 里读熔丝块会挂死判决机）")
            # 真正的读时刻 = 板子的 settle + 代理的 FIRST_READ_DELAY。只调后者等于只转半个旋钮：
            # 09:00:37 那批卡在第 2 发后 +6.7 s 持续不可达，而我们的读排在 4000+2000=6 s —— 差 1 s 判不到。
            self.assertIn("WAIT", exports,
                          "hunt 阶段不覆盖 settle ⇒ 读永远排在发后 4+2=6 s，深格子每次都刚好落在死亡之后")
            total_ms = int(exports["WAIT"]) + 1000 * int(exports["FIRST_READ_DELAY"])
            self.assertLessEqual(total_ms, 5000,
                                 "发后到第一次读的总静默必须明显早于实测死亡时刻（~6.7 s）")
            self.assertGreaterEqual(total_ms, 3000,
                                    "总静默 <3 s 就排进卡自己的 PERST# 重枚举/ROM 里 —— 红线 1")
        else:
            self.assertEqual(exports["VD"], exports["MINV"], "hammer 阶段就该钉死一个坐标")
        code = (self.ROOT / "temp" / "r_fast.sh").read_text(encoding="utf-8")
        self.assertIn("阶段=${MODE}", code, "批报告要回显阶段，否则台账答不出这批是找区间还是死磕")

    def test_零读是hammer的设定不是全局默认(self):
        """代理的默认必须是【盖章判决】；零读只能由 MODE=hammer 显式导出。

        2026-11-26 把 bar0_sessions_per_shot 的全局默认改成 0（那天做零读对照批）之后，9 条代理测试
        同时变红：'lost' 被判成 'under'、复读与寄存器留痕全丢、first_read_delay 的下限没人验了。
        那不是测试太挑剔 —— 它说的是：任何不经批脚本的人工直调都会静默产出未盖章的 under，而
        未盖章的 under 会让我们继续往一张已经动了的卡上打下一发。
        """
        self.assertGreaterEqual(shot_policy.get("bar0_sessions_per_shot"), 1,
                                "默认零读 = 人工直调代理就得到未盖章判决")
        self.assertGreaterEqual(shot_policy.get("confirm_reads"), 1,
                                "默认不许盖章复读=0（同上：默认必须是能盖章的那一档）")
        self.assertEqual(shot_policy.HAMMER.get("BAR0_SESSIONS"), 0,
                         "hammer 的零读必须显式导出，不能靠全局默认")
        # 阶段切换必须同时改判决侧（只改搜索轴 = 找区间却判不出 over）
        hunt = shot_policy.recipe_exports()
        self.assertIn("export BAR0_SESSIONS=1", hunt, "hunt 阶段必须真读：config-only 给不出 over")

    def test_开火入口只许有一个(self):
        """能起战役的入口在 temp/ 下必须只有一个。

        今天数了一下：temp/ 里堆了 119 个 .sh。成因每次都一样 —— 出一次事故我就新写一个脚本，而不是
        把教训折回那一个入口，于是"这一批到底带的是哪组数"重新变成考古现场（r_fire.sh 里那份
        `CARD_SETTLE_S=25` 正是这样挖出来的）。操作员的说法更直接：**就弄一个开火的脚本**。
        要新形态就在 shot_policy 里做成键，或在 r_fast.sh 里加分支；新开一份 = 这条测试红。
        """
        import os
        d = self.ROOT / "temp"
        firing = [n for n in sorted(os.listdir(d))
                  if n.endswith(".sh") and "judge-entry/run.sh" in
                  (d / n).read_text(encoding="utf-8", errors="replace")]
        self.assertEqual(["r_fast.sh"], firing,
                         f"开火入口不止一个：{firing} —— 把差异折回 r_fast.sh 或做成规程键")

    def test_temp_下的临时脚本是有穷集合(self):
        """一条功能 = 一个脚本；确定的逻辑进 bench/ 或 host/，temp/ 只留三件一次性工具。

        今天 temp/ 攒到 119 个 .sh：每次事故我都新写一个探针，于是"当时的逻辑"散在 119 份副本里，
        而其中被验证过的那部分本该长成 bench/ 里的一个函数。白名单之外再出现 .sh，就说明有人在
        用新脚本代替固化 —— 那正是操作员点名要停掉的习惯。
        """
        import os
        allowed = {"r_fast.sh", "r_down.sh", "append_chain.sh"}
        extra = sorted(n for n in os.listdir(self.ROOT / "temp")
                       if n.endswith(".sh") and n not in allowed)
        self.assertEqual([], extra,
                         "temp/ 里出现了新的 .sh：%s —— 它是哪种功能？确定的话就并进 bench/*.sh 或 "
                         "host/ga100vfi/ 里（带测试），别留在 temp/" % ", ".join(extra))

    def test_发前的capture闸门在代码里而不是在注释里(self):
        crack = (self.ROOT / 'bench' / 'crack.sh').read_text(encoding='utf-8')
        early = (self.ROOT / 'bench' / 'early.sh').read_text(encoding='utf-8')
        self.assertIn('last-capture', early, "early.sh 不再记 capture 时刻：闸门就成了瞎子")
        self.assertIn('last-capture', crack)
        self.assertIn('capture_min_gap_s', crack, "crack.sh 的 capture 闸门没引用规程")
        self.assertIn('CAPTURE_OK_OLD', crack, "闸门必须留一条显式、要写理由的绕行路径")


class UnjudgedShotAccounting(unittest.TestCase):
    """打了但没人判的那一发，必须在台账里占一行。

    2026-11-26 的 config-only 批：板子 tries=3、判决 2、`顺序` 里只有 2 行 —— presence 看门狗停战
    吞掉了悬着的那一发。停战是对的，**少记一行是假的**：那之后"实发数"这一列本身不可信。
    """

    def test_看门狗停战时悬着的那一发要记成nojudge(self):
        ag = VerdictAgent('http://example.invalid', '0000:01:00.0',
                          first_read_delay=0, max_bar0_sessions=0)
        # 只看停战那一段：判决还悬着（step 什么都不做），第二次 presence 采样时卡已经不在了。
        ag.step = Mock(return_value=False)
        ag.presence_sample = Mock(side_effect=[False, True])
        ag.status = Mock(return_value={'campaign': dict(waiting()['campaign'],
                                                        delay=64, v_dip=372)})
        ag.stop_board = Mock()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(2, agent_module.run_forever(ag, poll=0.1, max_lost=1))
        txt = out.getvalue()
        self.assertIn("-> nojudge", txt, "悬着的那一发没进台账：实发数这一列是假的")
        self.assertIn("delay=64", txt)
        self.assertIn("vd=372", txt, "补记的行必须带坐标，否则不知道往哪个坐标重打")

    def test_没有悬案时不要凭空多记一行(self):
        ag = VerdictAgent('http://example.invalid', '0000:01:00.0', max_bar0_sessions=0)
        ag.status = Mock(return_value={'campaign': {'running': False}})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(ag.note_unjudged("测试：战役已停"))
        self.assertNotIn("nojudge", out.getvalue())


if __name__ == '__main__':
    unittest.main()
