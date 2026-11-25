#!/usr/bin/env python3
"""Deliver correlated PCIe verdicts to an ESP32 campaign.

Run on the card's Linux host:
    sudo python3 host/ga100vfi_agent.py http://192.168.4.1 --bdf 0000:01:00.0

Since 0.4.1 the board cannot judge a shot at all: every shot waits for the host,
so this agent (or a human on the web page) is the only thing that advances a
campaign. Only campaigns waiting for a verdict are inspected. A verdict is
retained until the board acknowledges it; retrying never repeats the register
classification, which itself writes DSOV. Importing this module opens no device.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ga100vfi.bar0 import Bar0, Bar0Unavailable, restore
from ga100vfi.classify import (FUSECTRL, UNREADABLE, FuseControllerStop, Poisoned,
                               attempt_outcome, classify, require_usable_ctrl)
# 健康基线只有**一处**定义（envelope），代理与包络必须用同一个数判"遥测动没动"。
from ga100vfi.envelope import HEALTHY_DSOV

Token = tuple[int, int]


def awaiting_token(status: dict) -> Token | None:
    """The shot the board is waiting on, or None.

    0.4.1: the board has no on-board oracle, so `awaiting_verdict` after the settle
    interval is the whole condition — there is no mode in which the board judges a
    shot by itself. The token still has to be a real (session, attempt) pair: a
    status block that predates a restart carries a session this agent's verdict
    would be applied to the wrong shot.
    """
    c = status.get("campaign", {})
    if not c.get("running") or not c.get("awaiting_verdict"):
        return None
    session, attempt = c.get("session"), c.get("attempt")
    if not all(type(x) is int and 0 < x <= 0xFFFFFFFF for x in (session, attempt)):
        return None
    return session, attempt


class VerdictAgent:
    def __init__(self, board: str, bdf: str, timeout: float = 4.0, max_lost: int = 1, *,
                 first_read_delay: float = 2.0, max_bar0_sessions: int = 2,
                 poll_s: float = 2.0, budget_s: float = 100.0):
        self.board = board.rstrip("/")
        self.bdf = bdf
        self.timeout = timeout
        # 打完之后到第一次读 BAR0 之间的静默（操作员 2026-11-20 明确要求"打完后过一会儿再读"）。
        # 板子侧本来就有 settle=restore_wait_ms 才置 awaiting_verdict（本轮实测 62 s），这段是
        # 额外余量：卡刚从 PEX_RST 释放、正在跑 ROM，此时读熔丝块正是红线 1 禁止的场景。
        self.first_read_delay = first_read_delay
        # 红线 1："BAR0 的熔丝块读会干扰启动，只读一次"。2026-11-20 实测打脸：一发 lost 的
        # waiting 段是 08:36:19→08:36:47 = 28 s ≈ 15 次 × 2 s —— 卡正在死去的那 28 秒里，代理
        # 对它开了 15 次 BAR0 会话。默认 2 次（1 次正常 + 1 次容忍链路刚起来），其余时间只做
        # config 的被动 presence 观察，绝不碰 BAR0。
        self.max_bar0_sessions = max_bar0_sessions
        self.poll_s = poll_s
        # 必须短于固件的 MANUAL_TIMEOUT_US(120 s)：超了板子就自己计 timeouts++ 并跳过这一发，
        # 那比拿一个 lost 更糟 —— 坐标白烧。
        self.budget_s = budget_s
        self.pending: tuple[Token, str] | None = None
        self.accepted: Token | None = None
        # Consecutive "lost" verdicts. This version's campaign.c counts a lost verdict
        # as `over` (campaign.c:271), so a card that has left PCIe makes the ladder climb
        # toward *deeper* dips while every remaining shot is fired at an absent card. The
        # 0.2.x host tooling stopped after a streak; the rolled-back agent must too.
        self.lost_streak = 0
        # 停火阈值必须在交判决**之前**就用掉（见 step 里的 stopping）：campaign.c 收判决和
        # 推进状态机在同一次 campaign_poll() 里，stop 跟在判决后面就追不上下一发。
        # 2026-11-20 第二张卡实测：三发 lost 的每一轮 tries 都是 2 —— 一发有判决、另一发
        # 谁也没判过；那正是档案里"lost 之后补一发 → poison"的形状
        # （第一张卡 330 那轮：lost、lost、poison，见 §2.4）。
        # 默认 1 而不是 3：README §2.4 规则 2 一直是"一发 lost 即停火"，而 3 让代理自认
        # "还没到停手的量"，根本不下停止命令，唯一的拦截落到 crack.sh 的 3 s 状态轮询上。
        self.max_lost = max_lost
        self.lost_stopped = False
        # 摸到窗口（遥测动了）同样立刻停战，理由见 reaction()。
        self.reacted = False
        # 与判决无关、按时间自己跑的 presence 看门狗（见 presence_sample 的三条限制）。
        # grace 取 3 s 而不是"单次采到就停"：每一发板子自己拉 PERST#，那 ~1 s 里卡是合法地
        # 不在总线上，采到就停会把每一批都掐死；而今天这种"第 1 发判完卡消失"是持续性的。
        self.presence_grace_s = 3.0
        self._absent_since = None
        self._seen_once = False
        self.running = False
        # The registers the last verdict was made from, printed with it. Two rounds of
        # "shot 1 under, shot 2 poison" could not be explained from verdict words alone --
        # all we knew was the outcome word. Whether the controller was already ill *before*
        # the pulse that we then blamed is only answerable if every shot carries the
        # controller's own state with its verdict.
        self.last_regs: dict[str, int] = {}
        # Which shot last_regs belongs to. Without this the lost line prints the previous
        # shot's registers and reads as "the controller was clean when the card vanished".
        self._regs_for: Token | None = None
        # Why the most recent read attempt inside the current evaluation failed. Only
        # meaningful together with an empty/partial last_regs; cleared per shot.
        self.last_fail: str = ""

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
        try:
            if not restore(self.bdf, wait_s=0.25, settle_s=0.0):
                self.last_fail = "restore-failed: 上游桥没起链路"
                return None
            bar_ctx = Bar0(self.bdf)
        except Bar0Unavailable as exc:
            # 闸门拒绝 = 这一发判不了，判成 dead。判据全是只读的（桥 LnkSta 的 dll_active、
            # config 的 device id），一次 MMIO 都没花 —— 今天四次整机死机，每一次都是链路
            # 已经没了还去摸 BAR0。宁可这一发没有判决，也不能再拿一台机器去换。
            self.last_fail = f"bar0-gated: {exc}"
            return None
        with bar_ctx as bar:
            # The controller question is asked before the presence probe, because the probe
            # is itself a BAR0 read and 红线 1 forbids BAR0 reads once the controller says
            # 0xBADF…. Keep the number too: it is the only per-shot record of what the
            # controller looked like at the moment we took the verdict.
            self.last_regs = {"FUSECTRL": bar.rd32(FUSECTRL)}
            # 非 IDLE、非 0xBADF 的第三种态（实测 0x00000000）同样不许往下判：那一次
            # 同电压的下一发就把控制器打成 0xBADF510E 并把卡打掉 PCIe。
            require_usable_ctrl(self.last_regs["FUSECTRL"])
            if bar.rd32(0) == UNREADABLE:
                self.last_fail = "bar0-unreadable: config/BAR0 读回 0xFFFFFFFF"
                return None
            verdict = classify(bar)
            self.last_regs.update({"DSOV": verdict.dsov, "CRC": verdict.crc_status,
                                   "PRIV": verdict.priv_mask, "FPF": verdict.fpf_row})
            return attempt_outcome(verdict)

    def config_id(self) -> int | None:
        """config 头 4 字节（vendor:device）。None = 这个平台连节点都打不开（测试台/非 Linux）。

        单独拆出来当接缝：真卡上的 presence 判定全靠它，而它用的两个系统调用在 Windows 上
        不存在或不适用，测试不能靠 patch 那些调用去验证。
        """
        path = Path(f"/sys/bus/pci/devices/{self.bdf}/config")
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return None
        try:
            return struct.unpack("<I", os.pread(fd, 4, 0))[0]
        except OSError:
            return UNREADABLE
        finally:
            os.close(fd)

    def present(self) -> bool:
        """只读 config 的 vendor/device id：卡在不在这条总线上。不开 BAR0、不写任何东西。

        顺序很要紧。旧实现是"先 restore() 再开 BAR0，能不能判出来都算一次"，于是一发 lost 会
        连开 15 次 BAR0 会话（实测 waiting 段 28 s ≈ 15×2 s），而 logs/02 记的正是对不在总线上
        的卡做 BAR0 访问会把上游桥一起拖死。读不到节点时按在场处理 —— 这道闸门绝不能反过来
        把真卡说成不在，那等于让代理一枪都判不了。
        """
        vd = self.config_id()
        if vd is None:
            return True
        if vd == UNREADABLE:
            self.last_fail = "absent: config 读回全 1（期间没有打开 BAR0）"
            return False
        return True

    def evaluate(self, token: Token) -> str | None:
        # 读数按"发"归零，不按调用归零：同一个 token 可能因网络错误被 evaluate 第二次，
        # 那时清掉就会把这一发已经读到的数丢掉。跨发必须清，否则 lost 那一行会印上
        # 上一发的寄存器（2026-11-20 实测：两发逐字节相同，判决却是 under 与 lost）。
        if self._regs_for != token:
            self.last_regs, self.last_fail, self._regs_for = {}, "", token
        if self.first_read_delay > 0:
            time.sleep(self.first_read_delay)      # 打完先让卡跑完它自己的启动，再读
        deadline = time.monotonic() + self.budget_s
        sessions = 0
        while time.monotonic() < deadline:
            if awaiting_token(self.status()) != token:
                return None
            if sessions < self.max_bar0_sessions and self.present():
                sessions += 1
                verdict = self.gpu_verdict()
                if verdict is not None:
                    return verdict
            time.sleep(self.poll_s)
        return "lost"

    def regs_line(self) -> str:
        """判决行里的读数段：只许出现这一发自己读到的数。

        2026-11-20 的教训：lost 的那一行印着 `FUSECTRL=0xe0040000 DSOV=0x00000001
        CRC=0x00000075`，与上一发逐字节相同 —— 因为 gpu_verdict() 的几条早退路
        （restore 失败 / BAR0 读回 0xFFFFFFFF）根本不写 last_regs，而打印处照打不误。
        那行读起来像"卡掉链路时控制器是干净的"，而事实是我们**从没为这一发读过控制器**，
        正是判"POST 还是拔电"最依赖的那个证据。读不到就明说读不到，并带上失败原因。
        """
        if not self.last_regs:
            return f"读数=无（本发一个寄存器都没读到：{self.last_fail or '未知'}）"
        body = " ".join(f"{k}=0x{v:08x}" for k, v in self.last_regs.items())
        if "DSOV" not in self.last_regs:
            body += f" 注:只读到控制器，熔丝块没读到（{self.last_fail or '未知'}）"
        return body

    def stop_board(self) -> None:
        """Park the board after a poison report: the campaign must not keep firing at a
        card that we are no longer allowed to read."""
        try:
            self.get("/api/campaign?op=stop")
        except OSError as exc:
            print(f"agent: could not stop the campaign ({exc}); stop it by hand", flush=True)

    def presence_sample(self, running: bool, now: float | None = None) -> bool:
        """被动看一眼 config 头 4 字节；卡持续不在总线上就立刻停战。返回 True = 已停战。

        2026-11-21 05:44 那批实测：第 1 发判完 `under` 之后卡自己从总线消失，而代理只在
        `awaiting_verdict` 时才醒 —— 于是板子把第 2 发的波形打在一张已经看不见的卡上
        （tries=2，其中一发谁也没判过）。档案 §2.4 的形状就是"lost 之后补一发 → poison"。
        判决驱动的停手只能救"下一次判决"，救不了"已经打出去的那一发"，所以这里要有一条与判决
        无关、按时间自己跑的看门狗。

        三条限制缺一不可：
          * **只读 config**（一次 pread），绝不碰 BAR0 —— 毒态下读 BAR0 挂死整机（红线 1）；
          * 必须**持续**消失 >= presence_grace_s：每一发板子本来就自己拉 PERST#，卡在这段时间
            里是**合法地**不在总线上（ROM 下载约 1 s），单次采到全 1 就停火会把每一批都掐死；
          * 只在战役 running 时生效：没在打的时候停谁。
        """
        import time as _t
        if not running:
            self._absent_since = None
            return False
        vd = self.config_id()
        if vd is None:
            # 节点根本打不开：非 Linux / 测试台上是这个形状，必须**不看门**，否则每批都被自己掐死。
            # 只有"曾经在场、现在打不开"才算卡掉了（sysfs 节点被内核摘掉的那种死法）。
            gone = self._seen_once
        else:
            gone = (vd & 0xFFFF) == 0xFFFF or (vd & 0xFFFF0000) == 0xFFFF0000
            if not gone:
                self._seen_once = True
        if not gone:
            self._absent_since = None
            return False
        if self._absent_since is None:
            self._absent_since = _t.monotonic() if now is None else now
            return False
        elapsed = (_t.monotonic() if now is None else now) - self._absent_since
        if elapsed < self.presence_grace_s:
            return False
        print(f"agent: 卡连续 {elapsed:.1f}s 不在 PCIe 上（config 读回全 1，期间没开 BAR0）"
              " —— 不等判决，立刻停战", flush=True)
        self.lost_stopped = True
        self.stop_board()
        return True

    def reaction(self, verdict: str) -> bool:
        """这一发是不是**摸到窗口边**了 —— 摸到就必须停火，不许让 ladder 替我们继续下注。

        判据与包络同一条（`envelope.Shot.progress`）：判决词 over/hit/goldilocks，或 DSOV 离开
        健康基线。CRC 读脏**不算**（那是"我们读得太早"，本台账 33 发都有，拿它停火等于每批一发）。

        为什么这条必须由代理执行：红线一直是"见 DSOV=0 立刻停火冻结"，而 2026-11-21 之前它只是
        一句文档话。实测第一发 `DSOV=0 CRC=0x75`（delay=68 vd=372）之后，板子按 ladder 又自己连打
        3 发，其中一发没人判过 —— 卡就此硬死，第 8 次拔电。反应不是"再深一点试试"的许可证，它
        是"停手、单轴精修"的口令；靠人盯日志再 curl 停，慢一拍就是一发。
        """
        if verdict in ("over", "hit", "goldilocks"):
            return True
        dsov = self.last_regs.get("DSOV")
        return dsov is not None and dsov != HEALTHY_DSOV

    def step(self) -> bool:
        state0 = self.status()
        self.running = bool(state0.get("campaign", {}).get("running"))
        token = awaiting_token(state0)
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
        streak = self.lost_streak + 1 if verdict == "lost" else 0
        # Order matters, and the archive is what says so. campaign.c ingests a verdict and
        # advances the state machine in the *same* campaign_poll() call, so a stop that
        # follows the feed loses the race to the next physical attempt: measured on the
        # second card (2026-11-20, 3×222µs @ vd=326 / delay=54) every "lost" round came
        # back with tries=2 — one judged lost plus one nobody ever judged. The stop must
        # therefore be requested *before* the verdict is delivered, and the board's own
        # check (running && stop_requested → finish_locked) then fires in the same poll.
        stopping = bool(self.max_lost) and streak >= self.max_lost
        if stopping:
            print(f"agent: 连续 {streak} 发 lost — 卡已不在 PCIe 上，先让板子停手，"
                  "再交这一发的判决。继续打只是把发数烧在一张看不见的卡上。", flush=True)
            self.lost_stopped = True
        # 反应排在 lost 之后、feed 之前：同一次 poll 里 stop 才追得上下一发（上面的注释）。
        reacted = self.reaction(verdict)
        if reacted:
            print(f"agent: 反应 —— {self.regs_line()}（判决 {verdict}）：立刻停火。"
                  "下一发要由人挑坐标，不能让 ladder 替我们继续往深里下注。", flush=True)
            self.reacted = True
        if stopping or reacted:
            self.stop_board()
        try:
            self.feed(token, verdict)
        except urllib.error.HTTPError as exc:
            if exc.code != 409:
                raise
            # 只要停火是**我们自己**发起的（lost 或反应都一样），409 就在意料之中：板子已经不
            # 等这一发的 token 了。下面那行判决记录是 bench/mkledger.sh 与包络读的，必须照打 ——
            # 坐标从台账里丢掉，是唯一一次拔电买不回来的东西。
            # 这里以前只看 `stopping`（lost 那条路），于是 2026-11-21 那颗 **goldilocks**
            # （delay=64 vd=372，DSOV=0/CRC=0x55）在 agent.log 里没有那一行：代理先 stop 再 feed，
            # 板子回 409，代码按"过期判决"丢弃并 return —— 命中发生了，台账上却不存在。
            if not (stopping or reacted):   # 真的过期判决（战役被人换了/停了）：原样丢弃，不认账
                self.pending = None
                return False
            self.pending = None
            self.accepted = token
        else:
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
        # we also kept what the controller read as on shot 1 — and only ever that shot's own
        # reading, which is what regs_line() guarantees.
        print(f"session {token[0]} attempt {token[1]} delay={c.get('delay')} "
              f"vd={c.get('v_dip')} {self.regs_line()} -> {verdict}", flush=True)
        self.lost_streak = streak
        return True


def run_forever(agent: "VerdictAgent", poll: float, max_lost: int) -> int:
    """The poll loop, split out of main() so the two stop paths are testable."""
    agent.max_lost = max_lost      # single source of truth: step() uses agent.max_lost
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
            if agent.reacted:
                # 摸到窗口边就退出，不只是"叫板子停手"：crack.sh 看到代理还在，会把判决会话
                # 再撑一轮，而我们要的是**冻结** —— 下一发的坐标得由人（或包络的精修模式）挑。
                print("agent: 已停火。卡若还应答 config，就先只读抓窗口（bench/window.sh），"
                      "别急着重开战役撞同一族。", flush=True)
                return 4
            if agent.lost_stopped:
                # campaign.c turns a lost verdict into `over` (line 271), so a card that
                # left PCIe would make the ladder climb toward deeper dips while every
                # remaining shot is fired at an absent card. That is how 10 shots were
                # once spent buying 10 "lost". The stop request itself went out inside
                # step(), in the same iteration as the verdict — waiting for this line is
                # what cost a real extra shot on 2026-11-20 (tries=2, board.last seq=2,
                # one pulse nobody ever judged).
                return 2
            # 判决驱动的停手救不了"已经打出去的那一发"，所以等待期间也得起看门狗（0.3 s 一跳，
            # 不是 2 s：卡的异步死是发后几百毫秒到几秒，2 s 采样等于让它多打一发）。
            deadline = time.monotonic() + poll
            while time.monotonic() < deadline:
                if agent.presence_sample(agent.running):
                    print("agent: presence 看门狗停战（config 全 1，BAR0 一次没花）", flush=True)
                    return 2
                time.sleep(0.3)
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
    parser.add_argument("--max-lost", type=int, default=1,
                        help="stop the campaign after this many consecutive 'lost' verdicts "
                             "(0 disables). Default 1 because a shot fired at a card that "
                             "already left PCIe is the archive's poison recipe (README §2.4 "
                             "规则 2), and the verdict that would have stopped the campaign "
                             "arrives 1-2 s before the next physical attempt anyway.")
    parser.add_argument("--first-read-delay", type=float, default=2.0,
                        help="seconds to leave the card alone after the shot before the first "
                             "BAR0 read. The board's settle (restore_wait_ms) already runs "
                             "first; this is the extra margin for the card's own boot, during "
                             "which a fuse-block read is exactly what 红线 1 forbids.")
    parser.add_argument("--max-bar0-sessions", type=int, default=2,
                        help="BAR0 sessions allowed per verdict; after that the agent only "
                             "polls config space. One lost shot used to open 15 (实测 2026-11-20 "
                             "waiting 段 28 s ≈ 15×2 s) — repeated BAR0 access against a card "
                             "that is leaving the bus is the hang from logs/02.")
    args = parser.parse_args(argv)
    url = urllib.parse.urlparse(args.board)
    if url.scheme not in ("http", "https") or not url.netloc:
        parser.error("board must be an http:// or https:// URL")
    if not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", args.bdf):
        parser.error("--bdf must have the form 0000:01:00.0")
    if args.poll <= 0 or args.timeout <= 0:
        parser.error("--poll and --timeout must be positive")
    if args.first_read_delay < 0 or args.max_bar0_sessions < 1:
        parser.error("--first-read-delay must be >= 0 and --max-bar0-sessions >= 1")
    agent = VerdictAgent(args.board, args.bdf, args.timeout, args.max_lost,
                         first_read_delay=args.first_read_delay,
                         max_bar0_sessions=args.max_bar0_sessions)
    print(f"agent: board={agent.board} bdf={agent.bdf}", flush=True)
    return run_forever(agent, args.poll, args.max_lost)


if __name__ == "__main__":
    raise SystemExit(main())
