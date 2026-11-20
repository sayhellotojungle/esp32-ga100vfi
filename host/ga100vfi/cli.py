"""Command line: classify, baseline, attempt, campaign, revert."""
from __future__ import annotations

import argparse
import json
import sys
import time

from .bar0 import DEFAULT_BDF, Bar0, restore
from .classify import (STATE_POISONED, STATE_UNKNOWN, FuseControllerStop,
                       attempt_outcome, classify, fusectrl_state, snapshot)
from .glitch import Attempt, Card


POISON_EXIT = 4


def _poison_report(exc) -> int:
    """Loud, actionable, and above all *final* (README 安全红线 1, incident logs/02)."""
    print(f"STOP: {exc}", file=sys.stderr)
    return POISON_EXIT


def cmd_classify(args) -> int:
    if not restore(args.bdf, wait_s=0.5, settle_s=0.0):
        print(f"{args.bdf}: GPU config space unreachable (0xffffffff)", file=sys.stderr)
        return 2
    with Bar0(args.bdf) as bar:
        try:
            verdict = classify(bar)
        except FuseControllerStop as exc:
            return _poison_report(exc)
    print(json.dumps(verdict.as_dict(), indent=2))
    return 0 if verdict.passed else 1


def cmd_baseline(args) -> int:
    if not restore(args.bdf, wait_s=0.5, settle_s=0.0):
        print(f"{args.bdf}: GPU config space unreachable (0xffffffff)", file=sys.stderr)
        return 2
    with Bar0(args.bdf) as bar:
        try:
            data = snapshot(bar)
        except FuseControllerStop as exc:
            return _poison_report(exc)
    print(json.dumps(data, indent=2))
    return 0


def cmd_attempt(args) -> int:
    a = Attempt(args.v_dip, args.t_dip, args.v_restore,
                args.t_restore, args.t_revert, args.fast, args.hold_ms,
                args.pulses, args.pw, args.gap, args.delay, args.revert)
    with Card(args.port) as card:
        if not args.no_unlock and not _ensure_unlocked(card):
            print("unlock failed", file=sys.stderr)
            return 2
        rep = card.attempt(a)
        print(f"pwd={rep.pwd} e1=0x{rep.e1:04x} "
              f"writes_failed={rep.writes_failed} diag_ok={int(rep.diag_ok)} "
              f"slip_us={rep.slip_us} reset_slip_us={rep.reset_slip_us}")
    return 0 if rep.writes_failed == 0 and rep.diag_ok else 1


def _ensure_unlocked(card: Card) -> bool:
    pwd, _ = card.lock_status()
    if pwd:
        return True
    return card.unlock()


def _classify_attempt(v) -> str:
    """Classify a glitch attempt for adaptive calibration.

    under: dsov=1 (dip too shallow). over: dsov=0 but crc != 0x55 (too deep).
    hit: dsov=0 and crc=0x55 (in the window). goldilocks: all checks pass.
    """
    return attempt_outcome(v)


def _adjust_v_dip(v_dip, step, direction, oc, args):
    """Pick the next v_dip from a batch's outcome counts.

    Returns (v_dip, step, direction, still_searching).
    """
    n_u = oc["under"]
    n_o = oc["over"] + oc["lost"]
    n_h = oc["hit"] + oc["goldilocks"]

    if n_h > 0:
        return v_dip, step, direction, False
    if n_u > 0 and n_o == 0:
        new_dir = -1
        if direction != 0 and new_dir != direction:
            step = max(step // 2, 6)
        return max(v_dip - step, args.min_v), step, new_dir, True
    if n_o > 0 and n_u == 0:
        new_dir = +1
        if direction != 0 and new_dir != direction:
            step = max(step // 2, 6)
        return min(v_dip + step, args.max_v), step, new_dir, True
    if n_u > 0 and n_o > 0:
        return v_dip, max(step // 2, 6), direction, True
    return v_dip, step, direction, True


def cmd_campaign(args) -> int:
    v_dip = args.v_dip
    step = args.step
    direction = 0
    searching = args.adaptive
    batch_i = 0
    outcomes = ("goldilocks", "hit", "under", "over", "lost", "nack")
    batch_oc = dict.fromkeys(outcomes, 0)
    rounds = []
    t0 = time.monotonic()
    tries = valid = lost = nack = dsov0 = 0
    verdict = None

    with Card(args.port) as card:
        if not args.no_unlock and not _ensure_unlocked(card):
            # Do not call this "locked until VDD33 cycles": that sentence sent an operator
            # away to pull the card's power for what was an ordering bug. The controller
            # refuses the password while a fault is latched, and CLEAR_FAULTS (0x03) is the
            # way out -- the firmware does it in that order from this commit on; until the
            # board is reflashed, send `@w 25 03` from the console before `u`.
            print("unlock failed: VRM 无应答或拒绝密码（先试串口 `@w 25 03` 再 `u`；"
                  "只有清完故障仍 pwd=0 才需要考虑断电）",
                  file=sys.stderr)
            return 2
        # Set when the fuse controller reports itself poisoned: the loop stops there,
        # because the next BAR0 read is what hangs the upstream (README 红线 1).
        poisoned_stop = False
        try:
            while args.max_attempts <= 0 or tries < args.max_attempts:
                if verdict is not None:
                    break
                tries += 1
                a = Attempt(
                    v_dip_mv=v_dip, t_dip_us=args.t_dip,
                    v_restore_mv=args.v_restore, t_restore_us=args.t_restore,
                    t_revert_us=args.t_revert, fast=1, hold_ms=0,
                    pulses=args.pulses, pw=args.pw, gap=args.gap,
                    delay=args.delay, revert=0,
                )
                outcome, v = "nack", None
                try:
                    rep = card.attempt(a)
                except IOError as e:
                    print(f"[{tries}] glitch err: {e}", file=sys.stderr)
                    rep = None
                if rep is not None and rep.writes_failed == 0 and rep.diag_ok:
                    outcome = "lost"
                    if restore(args.bdf, wait_s=args.restore_wait):
                        with Bar0(args.bdf) as bar:
                            # Red line 1: the controller is asked before the presence probe,
                            # because the probe is itself a BAR0 read and a tripped controller
                            # answers that by hanging the PCIe upstream (logs/02).
                            # 2026-11-20 起判据扩到第三种态：非 IDLE、非 0xBADF 也不判决、不开火。
                            state = fusectrl_state(bar.rd32(FUSECTRL))
                            if state in (STATE_POISONED, STATE_UNKNOWN):
                                print("STOP: FUSECTRL 不是 IDLE（%s）—— 本次不再读 BAR0、"
                                      "不再开火。先重启判决机（POST 拉 PERST#），"
                                      "仍不可达再断电 ≥30 s" % state,
                                      file=sys.stderr, flush=True)
                                poisoned_stop = True
                                break
                            if bar.rd32(0x00) != 0xFFFFFFFF:
                                v = classify(bar)
                                outcome = _classify_attempt(v)
                if outcome == "nack":
                    nack += 1
                    time.sleep(0.5)
                    if not args.no_unlock:
                        try:
                            _ensure_unlocked(card)
                        except IOError:
                            pass
                elif outcome == "lost":
                    lost += 1
                else:
                    valid += 1
                    if v.dsov == 0:
                        dsov0 += 1
                    print(f"[{tries}] valid={valid} lost={lost} dsov={v.dsov} "
                          f"crc=0x{v.crc_status:02x} priv=0x{v.priv_mask:08x} "
                          f"{outcome}", file=sys.stderr, flush=True)
                    if outcome == "goldilocks":
                        verdict = v

                if not args.adaptive:
                    continue

                # Every physical attempt consumes the batch budget. In
                # particular, LOST must reach the voltage adjustment logic.
                batch_oc[outcome] += 1
                batch_i += 1

                if batch_i < args.batch and verdict is None:
                    continue

                # Batch complete — adjust v_dip
                rounds.append({"v_dip": v_dip, "outcomes": dict(batch_oc),
                              "searching": searching})
                if searching:
                    v_dip, step, direction, searching = _adjust_v_dip(
                        v_dip, step, direction, batch_oc, args)
                    if not searching:
                        print(f"[adaptive] window found at v_dip={v_dip}",
                              file=sys.stderr, flush=True)
                print(f"[adaptive] v_dip={v_dip} step={step} dir={direction} "
                      f"({batch_oc})", file=sys.stderr, flush=True)
                batch_i = 0
                batch_oc = dict.fromkeys(outcomes, 0)
        finally:
            if not args.no_revert:
                try:
                    card.revert()
                except Exception as e:
                    print(f"warn: revert failed: {e}", file=sys.stderr)

    out = {
        "goldilocks": verdict is not None,
        "v_dip": v_dip,
        "attempts": tries,
        "valid": valid,
        # Whether the loop ended because the controller tripped. Callers must not read
        # this as "out of attempts" and retry: the card needs a reboot (or a power cycle)
        # first, and every BAR0 read in between risks the machine-level hang from logs/02.
        "poisoned_stop": poisoned_stop,
        "lost": lost,
        "nack": nack,
        "dsov_zero": dsov0,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "verdict": verdict.as_dict() if verdict else None,
    }
    if args.adaptive:
        out["rounds"] = rounds
    print(json.dumps(out, indent=2))
    return 0 if verdict is not None else 1


def cmd_revert(args) -> int:
    with Card(args.port) as card:
        print(card.revert())
        print(card.status())
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ga100vfi")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("classify", help="run the five checks")
    c.add_argument("--bdf", default=DEFAULT_BDF)
    c.set_defaults(fn=cmd_classify)

    b = sub.add_parser("baseline", help="dump the shadow fuse registers")
    b.add_argument("--bdf", default=DEFAULT_BDF)
    b.set_defaults(fn=cmd_baseline)

    a = sub.add_parser("attempt", help="issue one glitch")
    a.add_argument("--port", default="/dev/ttyACM0")
    a.add_argument("--v-dip", type=int, default=325)
    a.add_argument("--t-dip", type=int, default=-100)
    a.add_argument("--v-restore", type=int, default=650)
    a.add_argument("--t-restore", type=int, default=-20)
    a.add_argument("--t-revert", type=int, default=0)
    a.add_argument("--fast", type=int, choices=(0, 1), default=1)
    a.add_argument("--hold-ms", type=int, default=0,
                   help="post-timeline SCL hold in milliseconds")
    a.add_argument("--delay", type=int, default=75,
                   help="command timing offset in microseconds; board-dependent")
    a.add_argument("--pulses", type=int, default=1,
                   help="3 for the Goldilocks triple pulse")
    a.add_argument("--pw", type=int, default=0, help="pulse width us (0=auto)")
    a.add_argument("--gap", type=int, default=0, help="gap between pulses us (0=auto)")
    a.add_argument("--revert", type=int, default=1, choices=(0, 1),
                   help="1=hand back to PWM-VID after dip; 0=stay PMBus (fast campaigns)")
    a.add_argument("--no-unlock", action="store_true")
    a.set_defaults(fn=cmd_attempt)

    camp = sub.add_parser("campaign", help="loop glitches until Goldilocks")
    camp.add_argument("--port", default="/dev/ttyACM0")
    camp.add_argument("--bdf", default=DEFAULT_BDF)
    camp.add_argument("--max-attempts", type=int, default=100,
                      help="0 = unlimited until Goldilocks")
    camp.add_argument("--v-dip", type=int, default=354)
    camp.add_argument("--t-dip", type=int, default=-60)
    camp.add_argument("--v-restore", type=int, default=750)
    camp.add_argument("--t-restore", type=int, default=-20)
    camp.add_argument("--t-revert", type=int, default=0)
    camp.add_argument("--pulses", type=int, default=3)
    camp.add_argument("--pw", type=int, default=198)
    camp.add_argument("--gap", type=int, default=150)
    camp.add_argument("--delay", type=int, default=117)
    camp.add_argument("--restore-wait", type=float, default=0.25,
                      help="seconds to wait for PCIe link after PEX_RST")
    camp.add_argument("--no-unlock", action="store_true")
    camp.add_argument("--no-revert", action="store_true",
                      help="leave VRM in PMBus mode after campaign")
    camp.add_argument("--adaptive", action="store_true",
                      help="auto-calibrate v_dip: adjust between batches based "
                           "on under/over/hit outcomes until the Goldilocks "
                           "window is found, then grind")
    camp.add_argument("--step", type=int, default=6,
                      help="adaptive: adjustment step in mV; 6 = one PMBus LSB")
    camp.add_argument("--batch", type=int, default=5,
                      help="adaptive: attempts per v_dip before adjusting")
    camp.add_argument("--min-v", type=int, default=300,
                      help="adaptive: safety floor (mV)")
    camp.add_argument("--max-v", type=int, default=380,
                      help="adaptive: safety ceiling (mV)")
    camp.set_defaults(fn=cmd_campaign)

    r = sub.add_parser("revert", help="restore the stock control path")
    r.add_argument("--port", default="/dev/ttyACM0")
    r.set_defaults(fn=cmd_revert)

    args = p.parse_args(argv)
    if args.cmd in ("attempt", "campaign"):
        if not (0 <= args.v_dip <= 1000 and 0 <= args.v_restore <= 1000):
            p.error("--v-dip and --v-restore must be 0..1000 mV")
        if not 1 <= args.pulses <= 16:
            p.error("--pulses must be 1..16")
    if args.cmd == "attempt" and not 0 <= args.hold_ms <= 10000:
        p.error("--hold-ms must be 0..10000")
    if args.cmd == "campaign":
        if args.max_attempts < 0 or args.restore_wait < 0:
            p.error("--max-attempts and --restore-wait must be non-negative")
        if args.adaptive and (args.batch < 1 or args.step < 1 or
                              not args.min_v <= args.v_dip <= args.max_v):
            p.error("adaptive mode needs --batch/--step > 0 and min-v <= v-dip <= max-v")
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
