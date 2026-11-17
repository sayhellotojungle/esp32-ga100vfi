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
from ga100vfi import cli, glitch
from ga100vfi.classify import Verdict, attempt_outcome
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
