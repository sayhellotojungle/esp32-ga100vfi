"""Behavioral regressions for framing, failure accounting and verdict delivery."""
import contextlib
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
        agent = VerdictAgent('http://example.invalid', '0000:01:00.0')
        agent.status = Mock(return_value=state or waiting())
        agent.gpu_verdict = Mock(return_value='hit')
        agent.feed = Mock()
        return agent

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
        agent = self.agent()
        agent.feed.side_effect = urllib.error.HTTPError('http://x', 409, 'stale', {}, None)
        self.assertFalse(agent.step())
        self.assertIsNone(agent.accepted)
        self.assertIsNone(agent.pending)

    def test_invalid_token_values_are_ignored(self):
        for value in (0, -1, True, '1', 0x100000000):
            self.assertIsNone(awaiting_token(waiting(session=value)))


if __name__ == '__main__':
    unittest.main()
