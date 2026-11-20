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
from ga100vfi.classify import (FUSECTRL, UNREADABLE, FuseControllerStop, Poisoned,
                               attempt_outcome, classify, require_usable_ctrl)

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
        # Consecutive "lost" verdicts. This version's campaign.c counts a lost verdict
        # as `over` (campaign.c:271), so a card that has left PCIe makes the ladder climb
        # toward *deeper* dips while every remaining shot is fired at an absent card. The
        # 0.2.x host tooling stopped after a streak; the rolled-back agent must too.
        self.lost_streak = 0
        # The registers the last verdict was made from, printed with it. Two rounds of
        # "shot 1 under, shot 2 poison" could not be explained from verdict words alone --
        # all we knew was the outcome word. Whether the controller was already ill *before*
        # the pulse that we then blamed is only answerable if every shot carries the
        # controller's own state with its verdict.
        self.last_regs: dict[str, int] = {}

    def get(self, path: str) -> str:
        """One GET. Exists as a method so "did the agent really ask the board to stop?"
        is something a test can assert instead of something we hope."""
        request = urllib.request.Request(self.board + path,
                                         headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read().decode()

    def status(self) -> dict:
        return json.loads(self.get("/api/status"))

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
        except OSError:
            # Best effort only. FileNotFoundError (a subclass) is what a kernel without the
            # runtime-PM attribute raises; the tests run this on a platform where the whole
            # /sys path cannot exist and it raises a plain OSError instead. Nothing here is
            # worth stopping a campaign over -- restore() below is the real reachability test.
            pass
        if not restore(self.bdf, wait_s=0.25, settle_s=0.0):
            return None
        with Bar0(self.bdf) as bar:
            # The controller question is asked before the presence probe, because the probe
            # is itself a BAR0 read and 红线 1 forbids BAR0 reads once the controller says
            # 0xBADF…. Keep the number too: it is the only per-shot record of what the
            # controller looked like at the moment we took the verdict.
            self.last_regs = {"FUSECTRL": bar.rd32(FUSECTRL)}
            # 非 IDLE、非 0xBADF 的第三种态（实测 0x00000000）同样不许往下判：那一次
            # 同电压的下一发就把控制器打成 0xBADF510E 并把卡打掉 PCIe。
            require_usable_ctrl(self.last_regs["FUSECTRL"])
            if bar.rd32(0) == UNREADABLE:
                return None
            verdict = classify(bar)
            self.last_regs.update({"DSOV": verdict.dsov, "CRC": verdict.crc_status,
                                   "PRIV": verdict.priv_mask, "FPF": verdict.fpf_row})
            return attempt_outcome(verdict)

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

    def stop_board(self) -> None:
        """Park the board after a poison report: the campaign must not keep firing at a
        card that we are no longer allowed to read."""
        try:
            self.get("/api/campaign?op=stop")
        except OSError as exc:
            print(f"agent: could not stop the campaign ({exc}); stop it by hand", flush=True)

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
        state = self.status()
        if awaiting_token(state) != token:
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
        # Coordinates belong in the verdict line. A run of 20 attempts that all
        # landed on the same voltage looked exactly like a run that had searched the
        # grid, because the only thing in this log was the verdict word — the ladder
        # had clamped and never actually moved. This costs no extra request: state is
        # the same /api/status reply we just read to confirm the token.
        c = state.get("campaign", {}) if isinstance(state, dict) else {}
        # delay and v_dip are what make a verdict readable; this firmware's status has no
        # per-point `step` key, so printing it only produced "step=None" noise. The register
        # dump rides along for the same reason: "shot 2 -> poison" is only interpretable if
        # we also kept what the controller read as on shot 1.
        regs = " ".join(f"{k}=0x{v:08x}" for k, v in self.last_regs.items())
        print(f"session {token[0]} attempt {token[1]} delay={c.get('delay')} "
              f"vd={c.get('v_dip')} {regs} -> {verdict}", flush=True)
        self.lost_streak = self.lost_streak + 1 if verdict == "lost" else 0
        return True


def run_forever(agent: "VerdictAgent", poll: float, max_lost: int) -> int:
    """The poll loop, split out of main() so the two stop paths are testable."""
    try:
        while True:
            try:
                agent.step()
            except FuseControllerStop as exc:
                # 安全红线 1: once the controller reports 0xBADFxxxx, every further BAR0
                # read risks the upstream hang from logs/02. Do not poll it "once more to
                # be sure": stop firing, stop reading, and ask for a cold boot.
                # 第三种态（非 IDLE 非 0xBADF，实测 0x00000000）走同一处置，但日志要能区分：
                # 那天同一电压的下一发就是 0xBADF510E + 卡掉链路。
                if isinstance(exc, Poisoned):
                    print("agent: POISON — " + str(exc), flush=True)
                else:
                    print("agent: FUSECTRL 第三种态 — " + str(exc), flush=True)
                agent.stop_board()
                print("agent: 已让板子停止战役；请把显卡 12V+3V3 断电 >=30s 再上电（README 5.1）",
                      flush=True)
                return 3
            except (OSError, ValueError) as exc:
                print(f"agent: {exc}", flush=True)
            if max_lost and agent.lost_streak >= max_lost:
                # campaign.c turns a lost verdict into `over` (line 271), so a card that
                # left PCIe would make the ladder climb toward deeper dips while every
                # remaining shot is fired at an absent card. That is how 10 shots were
                # once spent buying 10 "lost".
                print(f"agent: 连续 {agent.lost_streak} 个 lost —— 卡已不在 PCIe 上，停手。"
                      "继续打只是把发数烧在一张看不见的卡上。", flush=True)
                agent.stop_board()
                return 2
            time.sleep(poll)
    except KeyboardInterrupt:
        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", nargs="?", default="http://192.168.4.1")
    parser.add_argument("--bdf", default="0000:01:00.0",
                        help="PCI address of the target card")
    parser.add_argument("--poll", type=float, default=0.2,
                        help="seconds between status polls")
    parser.add_argument("--timeout", type=float, default=4.0,
                        help="HTTP timeout in seconds")
    parser.add_argument("--max-lost", type=int, default=3,
                        help="stop the campaign after this many consecutive 'lost' verdicts "
                             "(0 disables). A card that left PCIe makes the ladder climb the "
                             "wrong way while every shot is wasted.")
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
    return run_forever(agent, args.poll, args.max_lost)


if __name__ == "__main__":
    raise SystemExit(main())
