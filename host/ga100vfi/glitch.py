"""Serial client for the ESP32-S3 firmware and one-attempt issuance.

Wire-compatible with the RP2350 firmware (docs/PROTOCOL.md of the pico
repo). Port auto-discovery is optional:
pass port="auto" to pick the first Espressif (0x303A, the S3 native
USB-Serial-JTAG) or Raspberry Pi (0x2E8A) CDC device, so the same host
stack drives either MCU without hard-coding a port.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

PORT = "/dev/ttyACM0"

# (VID, PID) hints for auto-discovery; PID None = any.
KNOWN_USB_IDS = [(0x303A, None), (0x2E8A, None)]  # Espressif, Raspberry Pi


def _serial_module():
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError("Serial access requires pyserial; install the host package "
                           "with 'python -m pip install ./host'.") from exc
    return serial


def _list_ports():
    _serial_module()
    from serial.tools import list_ports
    return list_ports.comports()


def resolve_port(port: str) -> str:
    """Map "auto" to a concrete serial port; pass anything else through."""
    if port != "auto":
        return port
    ports = _list_ports()
    for vid, pid in KNOWN_USB_IDS:
        for p in ports:
            if p.vid == vid and (pid is None or p.pid == pid):
                return p.device
    raise IOError("no glitcher serial port found (pass an explicit --port?)")


@dataclass
class Attempt:
    v_dip_mv: int = 325
    t_dip_us: int = -100
    v_restore_mv: int = 650
    t_restore_us: int = -20
    t_revert_us: int = 0
    fast: int = 1
    hold_ms: int = 0
    pulses: int = 1
    pw: int = 0
    gap: int = 0
    delay: int = 75
    revert: int = 1

    def command(self) -> str:
        return (f"g {self.v_dip_mv} {self.t_dip_us} {self.v_restore_mv} "
                f"{self.t_restore_us} {self.t_revert_us} {self.fast} "
                f"{self.hold_ms} {self.pulses} {self.pw} {self.gap} {self.delay} "
                f"{self.revert}")


@dataclass
class Report:
    pwd: bool
    e1: int
    writes_failed: int
    raw: str
    diag_ok: bool = True
    slip_us: int = 0
    reset_slip_us: int = 0


class Card:
    def __init__(self, port: str = PORT, baud: int = 115200):
        self.s = _serial_module().Serial(resolve_port(port), baud, timeout=0.02)
        time.sleep(0.05)
        self.s.reset_input_buffer()

    def close(self) -> None:
        self.s.close()

    def cmd(self, line: str, wait: float = 0.3, total: float = 3.0,
            until: str | tuple[str, ...] | None = None) -> str:
        """Collect complete response lines, tolerating arbitrary serial chunks.

        A completion prefix applies only to a fully received line. With an
        explicit completion marker, silence is not completion: use the total
        monotonic deadline, including commands that intentionally hold reset.
        """
        if "\n" in line or "\r" in line:
            raise ValueError("cmd accepts one command without line terminators")
        self.s.reset_input_buffer()
        self.s.write((line + "\n").encode())
        self.s.flush()
        buf = bytearray()
        pending = bytearray()
        prefixes = (until,) if isinstance(until, str) else until
        last = time.monotonic()
        deadline = last + total
        while time.monotonic() < deadline:
            chunk = self.s.read(4096)
            if chunk:
                buf.extend(chunk)
                pending.extend(chunk)
                last = time.monotonic()
                while b"\n" in pending:
                    complete, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    text = complete.decode(errors="replace").rstrip("\r")
                    if text.startswith(("ERR ", "glitch ERR ", "ERROR ", "error:", "busy:")):
                        raise IOError(text)
                    if prefixes and text.startswith(prefixes):
                        return bytes(buf).decode(errors="replace").strip()
            elif not prefixes and time.monotonic() - last >= wait:
                break
        result = bytes(buf).decode(errors="replace").strip()
        if prefixes:
            raise TimeoutError(f"timeout waiting for a complete {prefixes!r} line: {result}")
        return result

    @staticmethod
    def _reply_line(reply: str, prefix: str) -> str:
        for line in reply.splitlines():
            if line.startswith(prefix):
                return line
        raise IOError(reply or f"missing {prefix!r} response")

    # --- PMBus over the bridge -------------------------------------------
    def read(self, addr: int, reg: int, n: int) -> bytes:
        reply = self.cmd(f"@r {addr:02x} {reg:02x} {n:02x}", until="I2C ")
        r = self._reply_line(reply, "I2C ")
        if not r.startswith("I2C OK"):
            raise IOError(r)
        data = bytes(int(x, 16) for x in r.split()[2:])
        if len(data) != n:
            raise IOError(f"short I2C read: expected {n} bytes, got {len(data)} ({r!r})")
        return data

    def write(self, addr: int, reg: int, data: bytes) -> str:
        line = f"@w {addr:02x} {reg:02x} " + " ".join(f"{b:02x}" for b in data)
        r = self._reply_line(self.cmd(line, until="I2C "), "I2C ")
        if not r.startswith("I2C OK"):
            raise IOError(r)
        return r

    # --- card commands ---------------------------------------------------
    def unlock(self) -> bool:
        r = self._reply_line(self.cmd("u", until="unlock "), "unlock ")
        return "MATCH" in r

    def lock_status(self) -> tuple[bool, int]:
        r = self.cmd("z", until=("cml=", "status ERR"))
        m = re.search(r"pwd=(\d) e1=0x([0-9a-f]{4})", r)
        if not m:
            raise IOError(r)
        return bool(int(m.group(1))), int(m.group(2), 16)

    def attempt(self, a: Attempt) -> Report:
        r = self.cmd(a.command(), wait=0.8,
                     total=20.0 + max(0, a.hold_ms) / 1000, until="glitch done ")
        done = self._reply_line(r, "glitch done ")
        m = re.search(
            r"glitch done pwd=(\d) e1=0x([0-9a-f]{4})"
            r" writes_failed=(\d+)", done)
        if not m:
            raise IOError(r)
        diagnostics = {}
        for name in ("diag_ok", "slip", "reset_slip"):
            value = re.search(r"\b" + name + r"=(-?\d+)(?:\s|$)", done)
            if value:
                diagnostics[name] = int(value.group(1))
            elif name + "=" in done:
                raise IOError(f"malformed {name} in response: {done}")
        if diagnostics.get("diag_ok", 1) not in (0, 1):
            raise IOError(f"invalid diag_ok in response: {done}")
        return Report(bool(int(m.group(1))), int(m.group(2), 16),
                      int(m.group(3)), r,
                      diag_ok=bool(diagnostics.get("diag_ok", 1)),
                      slip_us=diagnostics.get("slip", 0),
                      reset_slip_us=diagnostics.get("reset_slip", 0))

    def revert(self) -> str:
        reply = self.cmd("w", wait=0.8, total=3.0, until="revert")
        line = self._reply_line(reply, "revert")
        flags = re.findall(r"\b(?:vmin|vout|mode|reset)=(\d+)", line)
        if "ERR" in line or any(int(flag) == 0 for flag in flags):
            raise IOError(line)
        return reply

    def status(self) -> str:
        return self.cmd("v")

    def __enter__(self) -> "Card":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
