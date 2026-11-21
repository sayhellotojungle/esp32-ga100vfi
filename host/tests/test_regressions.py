"""Behavioral regressions for framing, failure accounting and verdict delivery."""
import contextlib
import inspect
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ga100vfi import classify as classify_module, cli, glitch
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


def waiting(session=11, attempt=1, mode=1, active=True):
    return {'campaign': {'running': active, 'mode': mode,
                         'awaiting_verdict': active,
                         'session': session, 'attempt': attempt}}


class AgentDelivery(unittest.TestCase):
    def agent(self, state=None):
        # first_read_delay=0：这个量是给真卡留的启动静默，测试里不该付那 2 秒（整份套件本来
        # 0.2 s 跑完）。它自己的那条测试把它设回 2.0 来验证真的会等。
        agent = VerdictAgent('http://example.invalid', '0000:01:00.0', first_read_delay=0)
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
        agent = self.agent(waiting(session=11, attempt=8))
        agent.first_read_delay = 2.0
        slept = []
        agent.gpu_verdict = Mock(return_value='under')
        with patch.object(agent_module.time, "sleep", lambda s: slept.append(s)), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent.evaluate((11, 8)), 'under')
        self.assertIn(2.0, slept)
        self.assertLess(len(slept), 3)              # 不是过去那种 15×2 s 的重读

    def test_read_gating_defaults_are_the_safe_ones(self):
        agent = VerdictAgent('http://example.invalid', '0000:00:00.0')
        self.assertEqual((agent.first_read_delay, agent.max_bar0_sessions), (2.0, 2))
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

    def test_does_not_observe_automatic_or_nonwaiting_campaign(self):
        for state in (waiting(mode=0), waiting(active=False)):
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
        # nobody ever judged. The archive rule is 一发 lost 即停火 (§2.4 规则 2).
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
        self.assertLess(at['burn "$SNAP" "$PLAN" --rows'], at["diffrows"],
                        "每次真写之后都必须独立重读逐行核对")
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


if __name__ == '__main__':
    unittest.main()
