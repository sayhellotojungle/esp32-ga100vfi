#!/usr/bin/env python3
"""Deliver correlated PCIe verdicts to an ESP32 campaign in host/manual mode.

Run on the card's Linux host:
    sudo python3 host/ga100vfi_agent.py http://192.168.4.1 --bdf 0000:01:00.0

Only campaigns explicitly waiting for a verdict are inspected. A verdict is
retained until the board acknowledges it; retrying never repeats the register
classification, which itself writes DSOV. Importing this module opens no device.
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ga100vfi.bar0 import Bar0, restore
from ga100vfi.classify import UNREADABLE, attempt_outcome, classify

Token = tuple[int, int]


def awaiting_token(status: dict) -> Token | None:
    c = status.get("campaign", {})
    if not c.get("running") or c.get("mode") != 1 or not c.get("awaiting_verdict"):
        return None
    session, attempt = c.get("session"), c.get("attempt")
    if not all(type(x) is int and 0 < x <= 0xFFFFFFFF for x in (session, attempt)):
        return None
    return session, attempt


class VerdictAgent:
    def __init__(self, board: str, bdf: str, timeout: float = 4.0):
        self.board = board.rstrip("/")
        self.bdf = bdf
        self.timeout = timeout
        self.pending: tuple[Token, str] | None = None
        self.accepted: Token | None = None

    def status(self) -> dict:
        request = urllib.request.Request(self.board + "/api/status",
                                         headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())

    def feed(self, token: Token, verdict: str) -> None:
        query = urllib.parse.urlencode({"session": token[0], "attempt": token[1],
                                        "r": verdict})
        with urllib.request.urlopen(self.board + "/api/verdict?" + query,
                                    timeout=self.timeout) as response:
            if response.read().decode().strip() != "ok":
                raise IOError("board did not acknowledge the verdict")

    def gpu_verdict(self) -> str | None:
        """None means link not ready; never pulse reset during observation."""
        try:
            Path(f"/sys/bus/pci/devices/{self.bdf}/power/control").write_text("on")
        except FileNotFoundError:
            pass  # Some kernels do not expose the runtime power attribute.
        if not restore(self.bdf, wait_s=0.25, settle_s=0.0):
            return None
        with Bar0(self.bdf) as bar:
            if bar.rd32(0) == UNREADABLE:
                return None
            return attempt_outcome(classify(bar))

    def evaluate(self, token: Token) -> str | None:
        for index in range(15):
            if awaiting_token(self.status()) != token:
                return None
            verdict = self.gpu_verdict()
            if verdict is not None:
                return verdict
            if index < 14:
                time.sleep(2.0)
        return "lost"

    def step(self) -> bool:
        token = awaiting_token(self.status())
        if self.pending is not None and self.pending[0] != token:
            self.pending = None
        if token is None or token == self.accepted:
            return False
        if self.pending is None:
            verdict = self.evaluate(token)
            if verdict is None:
                return False
            self.pending = token, verdict

        # The user may have stopped/restarted while PCIe was recovering.
        if awaiting_token(self.status()) != token:
            self.pending = None
            return False
        verdict = self.pending[1]
        try:
            self.feed(token, verdict)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:  # Stale token or campaign already stopped.
                self.pending = None
                return False
            raise
        # Network errors above leave pending intact for an idempotent retry.
        self.accepted = token
        self.pending = None
        print(f"session {token[0]} attempt {token[1]} -> {verdict}", flush=True)
        return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", nargs="?", default="http://192.168.4.1")
    parser.add_argument("--bdf", default="0000:01:00.0",
                        help="PCI address of the target card")
    parser.add_argument("--poll", type=float, default=0.2,
                        help="seconds between status polls")
    parser.add_argument("--timeout", type=float, default=4.0,
                        help="HTTP timeout in seconds")
    args = parser.parse_args(argv)
    url = urllib.parse.urlparse(args.board)
    if url.scheme not in ("http", "https") or not url.netloc:
        parser.error("board must be an http:// or https:// URL")
    if not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", args.bdf):
        parser.error("--bdf must have the form 0000:01:00.0")
    if args.poll <= 0 or args.timeout <= 0:
        parser.error("--poll and --timeout must be positive")
    agent = VerdictAgent(args.board, args.bdf, args.timeout)
    print(f"agent: board={agent.board} bdf={agent.bdf}", flush=True)
    try:
        while True:
            try:
                agent.step()
            except (OSError, ValueError) as exc:
                print(f"agent: {exc}", flush=True)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
