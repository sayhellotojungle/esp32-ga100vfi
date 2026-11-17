"""Offline unit tests for the host client (no hardware needed)."""
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ga100vfi import glitch  # noqa: E402


class FakeSerial:
    def __init__(self, replies: bytes):
        self.buf = io.BytesIO(replies)
        self.written = b""

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.written += data

    def flush(self):
        pass

    def read(self, _n):
        return self.buf.read()

    def close(self):
        pass


def make_card(replies: bytes) -> glitch.Card:
    card = glitch.Card.__new__(glitch.Card)   # skip the real serial open
    card.s = FakeSerial(replies)
    return card


class AttemptCommand(unittest.TestCase):
    def test_acceptance_command_is_byte_identical_to_pico(self):
        a = glitch.Attempt(354, -60, 750, -20, 0, 1, 0, 3, 198, 150, 117, 0)
        self.assertEqual(a.command(),
                         "g 354 -60 750 -20 0 1 0 3 198 150 117 0")

    def test_defaults(self):
        self.assertEqual(
            glitch.Attempt().command(),
            "g 325 -100 650 -20 0 1 0 1 0 0 75 1")


class ResponseParsing(unittest.TestCase):
    def test_attempt_release_build(self):
        card = make_card(b"glitch vd=354 td=-60\n"
                         b"diag sw=0x0000 vout_uv=1\n"
                         b"glitch done pwd=1 e1=0xc2fa writes_failed=0\n")
        rep = card.attempt(glitch.Attempt())
        self.assertTrue(rep.pwd)
        self.assertEqual(rep.e1, 0xC2FA)
        self.assertEqual(rep.writes_failed, 0)
        self.assertTrue(rep.diag_ok)
        self.assertEqual((rep.slip_us, rep.reset_slip_us), (0, 0))

    def test_attempt_raises_without_done_line(self):
        card = make_card(b"glitch vd=354")
        with patch.object(glitch.time, "monotonic", side_effect=[0, 0, 1, 21]):
            with self.assertRaises(IOError):
                card.attempt(glitch.Attempt())

    def test_i2c_read(self):
        card = make_card(b"I2C OK c8 08\n")
        self.assertEqual(card.read(0x25, 0x7E, 2), bytes([0xC8, 0x08]))

    def test_i2c_err(self):
        card = make_card(b"I2C ERR\n")
        with self.assertRaises(IOError):
            card.write(0x25, 0x21, b"\x2f\x00")


class PortDiscovery(unittest.TestCase):
    def test_explicit_port_passes_through(self):
        self.assertEqual(glitch.resolve_port("COM7"), "COM7")
        self.assertEqual(glitch.resolve_port("/dev/ttyACM0"), "/dev/ttyACM0")

    def test_auto_prefers_espressif(self):
        ports = [
            SimpleNamespace(vid=0x1234, pid=1, device="COM1"),
            SimpleNamespace(vid=0x303A, pid=0x1001, device="COM5"),
            SimpleNamespace(vid=0x2E8A, pid=0x0009, device="COM2"),
        ]
        with patch.object(glitch, "_list_ports", return_value=ports):
            self.assertEqual(glitch.resolve_port("auto"), "COM5")

    def test_auto_falls_back_to_pico(self):
        ports = [SimpleNamespace(vid=0x2E8A, pid=0x0009, device="COM2")]
        with patch.object(glitch, "_list_ports", return_value=ports):
            self.assertEqual(glitch.resolve_port("auto"), "COM2")

    def test_auto_raises_when_empty(self):
        with patch.object(glitch, "_list_ports", return_value=[]):
            with self.assertRaises(IOError):
                glitch.resolve_port("auto")


if __name__ == "__main__":
    unittest.main()
