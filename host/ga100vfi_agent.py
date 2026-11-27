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
from ga100vfi import shot_policy
from ga100vfi.classify import (FUSECTRL, UNREADABLE, FuseControllerStop, Poisoned,
                               attempt_outcome, classify, require_usable_ctrl)
# 健康基线只有**一处**定义（envelope），代理与包络必须用同一个数判"遥测动没动"。
from ga100vfi.envelope import (HEALTHY_CRC, HEALTHY_DSOV, code_to_mv, mv_to_code)

Token = tuple[int, int]

# 板子 campaign 的 phase 里，哪些意味着【有一发已经打出去（或正在打）而还没人判】。
# 出处是 main/campaign.c 的 phase_locked：executing(456)=波形窗口内（板子自己拉 PERST#，卡这段
# 时间合法地不在总线上）；settle(512)=打完了在等卡自己起来；waiting(469)=awaiting_verdict，
# 等主机判决。idle/stopped 不在此列 —— 那时不在场才是真的死了。
# 看门狗在这三档要用【宽的】宽限期（post_shot_grace_s），理由见 presence_sample 的两档注释。
IN_FLIGHT_PHASES = ("executing", "settle", "waiting")


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
    # 默认值全部来自 shot_policy —— 每发的节奏只有一份出处。以前这里写着 first_read_delay=2.0 而
    # crack.sh 里写着 4，同一件事两个数，谁生效取决于谁被调用（部署时也没人发现）。写错键名会当场
    # KeyError，而不是安静地拿一个旧默认值去跑一批。
    def __init__(self, board: str, bdf: str, timeout: float = 4.0,
                 max_lost: int = None, *,
                 first_read_delay: float = None,
                 max_bar0_sessions: int = None,
                 poll_s: float = 2.0, budget_s: float = None,
                 ready_poll_s: float = 0.25, confirm_reads: int = None,
                 stable_s: float = None):
        P = shot_policy.get
        max_lost = P("max_lost") if max_lost is None else max_lost
        first_read_delay = P("first_read_delay_s") if first_read_delay is None else first_read_delay
        max_bar0_sessions = (P("bar0_sessions_per_shot")
                             if max_bar0_sessions is None else max_bar0_sessions)
        budget_s = P("verdict_budget_s") if budget_s is None else budget_s
        confirm_reads = P("confirm_reads") if confirm_reads is None else confirm_reads
        stable_s = P("stable_s") if stable_s is None else stable_s
        self.board = board.rstrip("/")
        self.bdf = bdf
        self.timeout = timeout
        # --- 0.5.5 判决节奏：把两个"闭眼睡"换成"看着条件走" --------------------------------
        # 一发原来花 ~12 s：板子 settle 8 s + 代理 first_read_delay 4 s，全是固定睡眠。一个电源周期
        # 能买到几发是随机的（实测 1..40），所以每一秒固定等待都是直接从彩票数里扣。改成：静默下限
        # 仍然有（first_read_delay，红线 1 要求），但下限之内改为每 ready_poll_s 看一眼 token 还在不在，
        # 下限之后按谓词（config 已应答）决定何时开 BAR0，并把 t_cfg/t_bar0 记进判决行 —— 把"要等多久
        # 才读"从信仰变成**测量**，下一批才有资格调小它。没测到之前不许调。
        self.ready_poll_s = ready_poll_s
        # 脏读：11-25 那 40 发里约 1/3 的 CRC 读成 0x57/0x75（该是 0x55）。单发单读分不清"卡变了"
        # 和"我们读早了"。现在判决关键寄存器必须连读两次一致才盖章；两次不一致时**取更报警的那个**
        # ——误报 under 是往一颗已经武装起来的卡上继续打（唯一不可逆的损失），误报反应只是多停一次火。
        self.confirm_reads = max(1, confirm_reads)
        self.stable_s = stable_s
        # 每次判决重填（见 evaluate）：config 第一次应答 / 第一次真正开 BAR0 的相对时刻。
        self.t_cfg_s: float | None = None
        self.t_bar0_s: float | None = None
        self.stable: bool | None = None
        self.reads = 0
        # -----------------------------------------------------------------------------
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
        # toward *deeper* dips while every remaining shot was fired at an absent card. 0.5.5
        # fixed that board-side (campaign.c counts lost on its own and ends the campaign after
        # two at a point / three in a row) — the agent's stop stays, because the board only
        # knows a shot was lost once we tell it, and the 0.2.x tooling already stopped on a
        # streak. Two independent brakes, different failure models.
        self.lost_streak = 0
        # 停火阈值必须在交判决**之前**就用掉（见 step 里的 stopping）：campaign.c 收判决和
        # 推进状态机在同一次 campaign_poll() 里，stop 跟在判决后面就追不上下一发。
        # 2026-11-20 实测：三发 lost 的每一轮 tries 都是 2 —— 一发有判决、另一发
        # 谁也没判过；那正是档案里"lost 之后补一发 → poison"的形状
        # （330 那一轮：lost、lost、poison，见 docs/UNLOCK_AND_POISON.md §四）。
        # 默认 1 而不是 3：判据一直是"一发 lost 即停火"，而 3 让代理自认
        # "还没到停手的量"，根本不下停止命令，唯一的拦截落到 crack.sh 的 3 s 状态轮询上。
        self.max_lost = max_lost
        self.lost_stopped = False
        # 摸到窗口（遥测动了）同样立刻停战，理由见 reaction()。
        self.reacted = False
        # 与判决无关、按时间自己跑的 presence 看门狗（见 presence_sample 的三条限制）。
        # grace 取 3 s 而不是"单次采到就停"：每一发板子自己拉 PERST#，那 ~1 s 里卡是合法地
        # 不在总线上，采到就停会把每一批都掐死；而今天这种"第 1 发判完卡消失"是持续性的。
        self.presence_grace_s = shot_policy.get("presence_grace_s")
        # 那一发还在飞（板子 phase ∈ IN_FLIGHT_PHASES）时改用这一档：抛弃那一发 = 白丢一条边界证据。
        self.post_shot_grace_s = shot_policy.get("post_shot_grace_s")
        self._absent_since = None
        self._seen_once = False
        self.running = False
        # 板子 campaign 的 phase 字符串（executing/settle/waiting/stopped…）。心跳靠它躲开
        # 波形窗口，见 push_snapshot 的注释。
        self.last_phase = ""
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
        # ---- 判决方自己的心跳（板子的 @A 通道，见 push_snapshot 的注释）----
        self.peer_every_s = self.PEER_EVERY_S
        self._peer_t = 0.0
        self._peer_seq = 0
        self.peer_ok: bool | None = None
        self.peer_err = ""
        # 代理侧的计数。以前"这一批判过什么"只能批末从 agent.log 里数（timeline.py 干的就是这事），
        # 于是代理为什么停手这件事在批中是不可见的 —— 页面有 halt/ledger 两格，从来没人喂过它。
        self.shots = 0
        self.tally: dict[str, int] = {}
        self.dirty = 0
        self.last_verdict = ""
        self.halt = ""
        self.last_campaign: dict[str, object] = {}

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

    # ---- 判决方心跳（@A）----------------------------------------------------
    # 板子判断"判决方在不在"的唯一依据，是它有没有收到过 `@A` 快照（main.c 的 peer_cmd）。
    # 这条路 0.5.0 就在固件里实现好了（HTTP POST /api/cmd 的 `@A seq=N {…}`，回包还带回页面上
    # 递交的开跑请求），但主机侧从来没有发过它 —— 于是 crack.sh 的代理明明在跑，WIFI 页面照样
    # 显示"未运行"，而那句提示还错说判决方"经串口连本板"（2026-11-26 操作员指出，两个错：通道是
    # WiFi/HTTP，串口是**人**的逃生舱）。心跳顺带让页面上的 last/ledger/halt 三格第一次有真内容：
    # 代理在批中为什么停手，以前只能等批末翻 agent.log。
    PEER_EVERY_S = 1.0        # 过期门是固件的 peer.h:PEER_STALE_US(5 s)，1 Hz 留五倍余量
    PEER_STALE_S = 5.0        # 只用来算"半个过期周期"；**闸门在板子那边**，这里不是权威
    PEER_MAX_BYTES = 240      # peer.c 的 PEER_INFO_MAX=256，且整个载荷必须是可打印 ASCII
    # 超预算时按这个顺序丢字段：state/halt 是"代理还活着吗/为什么停"，最贵，最后才丢。
    PEER_DROP_ORDER = ("note", "gates", "next", "ledger", "last")

    def snapshot(self) -> dict:
        """一页纸的代理状态。**必须全是可打印 ASCII**：peer.c 对整个载荷逐字节查 0x20..0x7e，
        混进一个中文就整包被拒 —— 那等于没有心跳，而且是被静默地没有（页面继续显示"未运行"）。
        所以这里主动把不可打印/非 ASCII 的字符剔掉，而不是等着被板子退回。"""
        info = {
            "state": ("stopped:" + self.halt) if self.halt else (
                "judging" if self.pending else
                ("watching" if self.running else "idle")),
            "gates": f"bar0={self.reads}/{self.max_bar0_sessions} "
                     f"confirm={self.confirm_reads} ready_poll={self.ready_poll_s:g}s",
            "next": " ".join(f"{k}={v}" for k, v in sorted(self.last_campaign.items())
                             if v is not None) or "-",
            "last": f"#{self.shots} {self.last_verdict or '-'} stable="
                    + ("yes" if self.stable else ("NO" if self.stable is False else "-")),
            "ledger": (f"shots={self.shots} "
                       + " ".join(f"{k}={v}" for k, v in sorted(self.tally.items()))
                       + f" dirty={self.dirty}"),
            "halt": self.halt or "-",
            "note": f"bdf={self.bdf}" + (f" fail={self.last_fail[:20]}" if self.last_fail else ""),
        }
        clean = {}
        for key, value in info.items():
            text = "".join(ch for ch in str(value) if 0x20 <= ord(ch) <= 0x7E)
            clean[key] = text.strip() or "-"

        def size() -> int:
            return len(json.dumps(clean, separators=(",", ":"), ensure_ascii=True).encode())

        # 超预算就按优先级丢字段。**不能改类属性**：那样第一次溢出之后，后续每一份快照都会
        # 永久少掉同一批字段（实例上盖一个同名属性就把类的那份"用光"了）。
        order = list(self.PEER_DROP_ORDER)
        while size() > self.PEER_MAX_BYTES and order:
            clean.pop(order.pop(0), None)
        if size() > self.PEER_MAX_BYTES:          # 剩下的都是 state/halt：截断值而不是丢字段
            for key in list(clean):
                clean[key] = clean[key][:24]
        while size() > self.PEER_MAX_BYTES and len(clean) > 1:
            clean.pop(max(clean, key=len), None)  # 还是塞不下就只留最短的那几个
        return clean

    def peer_frame(self) -> str:
        return (f"@A seq={self._peer_seq} "
                + json.dumps(self.snapshot(), separators=(",", ":"), ensure_ascii=True))

    def seed_peer_seq(self) -> int:
        """接着板子上已有的序号往下数（best-effort，读不到就从 0 数）。

        `seq` 是**判决方实例**的计数，而页面把 `peer.seq` 当"最后快照 #N"给操作员看，操作员拿它分辨
        两件事："判决方在这儿待了多久"与"板子是不是重启过"。换一个新的 agent 进程从头数，数字就会
        倒退 —— 0.5.7 的上板自证第 5 节实测：板子从 #5 变回 #3。倒退看起来像"板子重启过"，而那正是
        这两态里另一态的征状，等于给了操作员一个假线索。板子那边不校验单调（`peer.c:peer_store` 原样
        存），所以倒退不会被拒绝、不会被任何闸门挡住 —— 只能由发送方自觉。
        """
        try:
            seen = int((self.status().get("peer") or {}).get("seq") or 0)
        except (OSError, ValueError, AttributeError, TypeError):
            return self._peer_seq
        if seen >= self._peer_seq:
            self._peer_seq = seen + 1        # 板子上那份是 seen，下一份才是新的
        return self._peer_seq

    def post_cmd(self, line: str) -> str:
        """POST 一行控制台命令给板子（与人在 `bench/console.sh` 里敲的是同一个入口）。
        单独成方法是为了让"心跳到底发没发出去"成为可断言的事，而不是一句希望 ——
        `get()`/`stop_board()` 存在的理由完全一样。"""
        request = urllib.request.Request(self.board + "/api/cmd",
                                         data=line.encode("ascii"),
                                         headers={"Content-Type": "text/plain"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read().decode(errors="replace")

    def push_snapshot(self, force: bool = False) -> bool:
        """把心跳交上去。**best-effort：心跳失败永远不能影响判决** —— 它是可观测性，不是闸门。
        失败只记 `peer_ok/peer_err`，由日志与页面自己解释。"""
        if self.peer_every_s <= 0:
            return False                       # --no-peer-heartbeat
        now = time.monotonic()
        since = now - self._peer_t
        if not force and since < self.peer_every_s:
            return False
        if not force and self.last_phase in ("executing", "settle") \
                and since < self.PEER_STALE_S / 2:
            # 波形正在跑（executing=计划步，settle=发后等待判决前那段）时**能不发就不发**。
            # 理由不是猜的：本台架量过"采样本身就是扰动"（0x8b 那 7 笔把 reset_slip 从 8~231 µs
            # 推到 370 µs）。一次 HTTP 请求在板子上要过 httpd 任务、串口/临界区，落在毛刺的计划
            # 表附近就是同一类扰动。但过期门（peer.h 的 PEER_STALE_US）一过，页面就把判决方显示成
            # "见过但断了"，而且**开火按钮会被 disable**（judging = peer.present）—— 观测面撒谎的
            # 代价是操作员得在批中重新点一次，所以超过半个过期周期照发。
            return False
        self._peer_t = now
        frame = self.peer_frame()
        try:
            reply = self.post_cmd(frame)
        except (OSError, ValueError) as exc:   # 包含载荷不是 ASCII 时的编码错误
            self.peer_ok, self.peer_err = False, str(exc)[:80]
            return False
        self._peer_seq += 1
        self.peer_ok, self.peer_err = True, ""
        # 回包里可能带页面上递交的开跑请求（这是 0.5.0 的设计：代理不需要第二条通道才知道有人点了
        # 「开始」）。本台架**不代管开火**：开火方是 crack.sh，它跑完发前闸门才 op=start。代理要是
        # 在这里替页面把请求打出去，就绕过了 poison 预检、锁状态与固件版本闸门 —— 只打印，不执行。
        if "op=start" in reply:
            print("agent: 页面上有开跑请求，但本代理不代管开火（发起方是 crack.sh，"
                  f"它跑完发前闸门才 op=start）：{reply.strip()[:90]}", flush=True)
        return True

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

    # ---- 0.5.5 的两条新判决规矩：谓词门（见 evaluate）与复读确认（见下） --------------------
    def _is_baseline(self, regs: dict) -> bool:
        """读数就是"什么都没动"的健康基线吗。是 → 不值得为它再开一次 BAR0 会话。"""
        return (regs.get("DSOV") == HEALTHY_DSOV
                and regs.get("CRC") == HEALTHY_CRC)

    def _alarm(self, regs: dict) -> tuple:
        """读数有多"像反应"。两次读法不一致时按这个排序取**更报警**的那一个。"""
        return (regs.get("DSOV") != HEALTHY_DSOV,
                regs.get("CRC") != HEALTHY_CRC,
                regs.get("PRIV") not in (None, 0x1E),
                regs.get("FPF") in (0, 0xFFFFFFFF) and regs.get("FPF") is not None)

    def _needs_confirm(self, verdict: str, regs: dict) -> bool:
        """这一发值不值得再开一次 BAR0 会话复核。

        三条"绝不追加读"的规矩都来自红线 1 与 2026-11-20 的实测：那一发 lost 的 28 s waiting 段里，
        代理对一块正在离开总线的卡开了 15 次 BAR0 —— 每一次都是整机死机的候选。于是：
          * lost / 判不了（熔丝块根本没读到）→ 一次都不追加，宁可这一发没有判决；
          * 健康基线 → 不复核。它是绝大多数发的形状，为它多开会话只是加风险；
          * 只有"读到了、而且不是基线"才复核 —— 脏读（CRC=0x57/0x75）与真反应混在一起的正是这一族。
        """
        if self.confirm_reads < 2 or self.reads >= self.max_bar0_sessions:
            return False
        if verdict in ("lost", "dead") or "DSOV" not in regs:
            return False
        return not self._is_baseline(regs)

    def evaluate(self, token: Token) -> str | None:
        # 读数按"发"归零，不按调用归零：同一个 token 可能因网络错误被 evaluate 第二次，
        # 那时清掉就会把这一发已经读到的数丢掉。跨发必须清，否则 lost 那一行会印上
        # 上一发的寄存器（2026-11-20 实测：两发逐字节相同，判决却是 under 与 lost）。
        if self._regs_for != token:
            self.last_regs, self.last_fail, self._regs_for = {}, "", token
            self.t_cfg_s = self.t_bar0_s = None
            self.stable, self.reads = None, 0
        t0 = time.monotonic()
        # 静默下限还在（红线 1：卡在自己 ROM 启动里时读熔丝块正是要禁的场景），但它不再是一整段
        # 闭眼睡：下限内每 ready_poll_s 看一眼 token 还在不在（人可能已经停手/换战役），并且把
        # config 第一次应答的时刻记下来。"下一次能把下限调到几秒"只能由这个数回答，不能由胆量回答。
        floor = t0 + self.first_read_delay
        deadline = t0 + self.budget_s
        while time.monotonic() < floor:
            if awaiting_token(self.status()) != token:
                return None
            if self.t_cfg_s is None and self.present():
                self.t_cfg_s = time.monotonic() - t0
            time.sleep(self.ready_poll_s)
        sessions = 0
        # ===== 一发**零次**熔丝块读（max_bar0_sessions=0）：判决只看 config，BAR0 一次不开 =====
        # 为什么要有这条通路（2026-11-26 对照批的证据）：把第一次开 BAR0 从 +4 s 推到 +20 s，卡的
        # 死亡时刻就跟着从 +10~13 s 推到 +25~30 s —— 死亡时间跟着**我们的读**走，不跟着那发坑走。
        # 而"打完之后主机还做了什么"里只剩两件与卡有关：这次 BAR0 会话（含 restore() 写 COMMAND）
        # 与 4 Hz 的 config pread。要判定前者，就得把后者之外的全部动作清零：config-only 判决。
        # 代价必须说清楚：这样的判决**没有寄存器盖章**（stable=None，判决行标 bar0=0），它只能说明
        # "到探测点为止没看到死"，不能证明 DSOV 没动 —— 真反应/命中要靠批末那**一次**单独读。
        # 也就是说这一批改的是"每周期能买到几发"，命中判据仍由 `burn status` 那一次读负责。
        if self.max_bar0_sessions == 0:
            while time.monotonic() < deadline:
                if awaiting_token(self.status()) != token:
                    return None
                if self.present():
                    if self.t_cfg_s is None:
                        self.t_cfg_s = time.monotonic() - t0
                    self.reads = 0
                    self.stable = None          # 没有寄存器读数 = 天生未盖章，不是"读崩了"
                    return "under"
                time.sleep(self.poll_s)
            return "lost"
        while time.monotonic() < deadline:
            if awaiting_token(self.status()) != token:
                return None
            if sessions < self.max_bar0_sessions and self.present():
                if self.t_cfg_s is None:
                    self.t_cfg_s = time.monotonic() - t0
                sessions += 1
                self.reads += 1
                if self.t_bar0_s is None:
                    self.t_bar0_s = time.monotonic() - t0
                verdict = self.gpu_verdict()
                if verdict is None:
                    continue
                snap = dict(self.last_regs)
                if self._needs_confirm(verdict, snap):
                    # 隔 stable_s 复读一次：脏读（读早了）不会重现，真反应会。
                    time.sleep(self.stable_s)
                    if awaiting_token(self.status()) != token:
                        return None
                    if sessions >= self.max_bar0_sessions:
                        self.stable = False          # 会话预算用尽：这一发没盖章
                    else:
                        sessions += 1
                        self.reads += 1
                        verdict2 = self.gpu_verdict()
                        snap2 = dict(self.last_regs)
                        if verdict2 is not None and "DSOV" in snap2:
                            self.stable = self._alarm(snap2) == self._alarm(snap)
                            if not self.stable and self._alarm(snap2) > self._alarm(snap):
                                # 两次不一致：取更报警的读数与判决。误报 under 会往一颗已经武装
                                # 起来的卡上继续打，那是本台架唯一不可逆的损失。
                                verdict, snap = verdict2, snap2
                            self.last_regs = snap
                        else:
                            self.stable = False
                else:
                    if self.confirm_reads >= 2 and verdict not in ("lost", "dead") \
                            and "DSOV" in snap:
                        self.stable = True           # 基线读数：按规矩不需要盖章就是干净的
                return verdict
            time.sleep(self.poll_s)
        return "lost"

    def shot_tags(self, campaign: dict) -> str:
        """判决行里 `vd=` 之后、读数之前那一串标签。三个都是"事后就再也拿不到"的证据：

        code= —— 物理坐标。VOUT_COMMAND 的 LSB 是 6.25 mV：372 与 375 是同一个码 60，而 366/368/370
          是另一个码 59。台账与包络按 mV 索引，所以两批分别打了 372 和 375 时，只有 code 能看出那是
          **同一发**。0.5.5 起板子自己报 v_code / codes_tried；老固件（没这两个键）代理按同一条取整
          自己算 —— 一个数只能有一处定义，见 ga100vfi.envelope.mv_to_code。
        t_cfg= / t_bar0= —— 从代理看到 awaiting_verdict 起，config 第一次应答、与第一次真正开 BAR0
          的相对秒数。"静默下限以后还能调到几秒"只能由这两个数回答；不记，下次就还是靠胆。
        stable= —— 判决关键寄存器有没有连读两次一致（见 _needs_confirm）。NO 的那一行既不能当反应、
          也不能当没反应：11-25 那批约 1/3 的 CRC 读成 0x57/0x75（该是 0x55），就是没盖章的形状。
        """
        out = ""
        code = campaign.get("v_code")
        vd = campaign.get("v_dip")
        if code is None and vd is not None:
            code = mv_to_code(vd)
        if code is not None:
            out += f" code={code}"
            if isinstance(vd, int) and code_to_mv(code) != vd:
                out += f"({code_to_mv(code)}mV)"      # 你打的 mV 与码的真正值不同
        if campaign.get("codes_tried") is not None:
            out += f" codes={campaign['codes_tried']}"
        if self.max_bar0_sessions == 0:
            out += " bar0=0/0"            # 台账要能一眼看出这批一发熔丝块读都没花（config-only）
        if self.t_cfg_s is not None:
            out += f" t_cfg={self.t_cfg_s:.1f}s"
        if self.t_bar0_s is not None:
            out += f" t_bar0={self.t_bar0_s:.1f}s"
        if self.stable is not None:
            out += f" stable={'yes' if self.stable else 'NO'}"
        return out

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

    def note_unjudged(self, reason: str) -> bool:
        """停战那一刻若还有一发悬着，就在台账里给它补一行 `-> nojudge`。

        没这一行时，"板子打了 N 发、判决 N-1 发"在台账里就只剩 N-1 发：少掉的那一发连坐标都没有，
        于是【实发数】这一列本身就是假的。2026-11-26 的 config-only 批正是这个形状（tries=3、判决
        2、顺序里只有 2 行），而 presence 看门狗是正确行为 —— 错的是我们没有为它记账。
        """
        try:
            st = self.status()
        except Exception:
            return False
        tok = awaiting_token(st)
        if tok is None:
            return False
        c = st.get("campaign") or {}
        self.reads = 0
        self.stable = None                      # 天生未盖章：我们没读任何东西
        print(f"session {tok[0]} attempt {tok[1]} delay={c.get('delay')} vd={c.get('v_dip')} "
              f"读数={reason} -> nojudge", flush=True)
        return True

    def presence_sample(self, running: bool, now: float | None = None,
                        in_flight: bool | None = None) -> bool:
        """被动看一眼 config 头 4 字节；卡持续不在总线上就立刻停战。返回 True = 已停战。

        2026-11-21 05:44 那批实测：第 1 发判完 `under` 之后卡自己从总线消失，而代理只在
        `awaiting_verdict` 时才醒 —— 于是板子把第 2 发的波形打在一张已经看不见的卡上
        （tries=2，其中一发谁也没判过）。判据的形状就是"lost 之后补一发 → poison"。
        判决驱动的停手只能救"下一次判决"，救不了"已经打出去的那一发"，所以这里要有一条与判决
        无关、按时间自己跑的看门狗。

        三条限制缺一不可：
          * **只读 config**（一次 pread），绝不碰 BAR0 —— 毒态下读 BAR0 挂死整机（红线 1）；
          * 必须**持续**消失 >= 相应宽限期：每一发板子本来就自己拉 PERST#，卡在这段时间
            里是**合法地**不在总线上（ROM 下载约 1 s），单次采到全 1 就停火会把每一批都掐死；
          * 只在战役 running 时生效：没在打的时候停谁。

        【两档宽限期，2026-11-26】已经打出去、还没判决的那一发（in_flight）用 post_shot_grace_s，
        空闲档才用 presence_grace_s。理由是被实测咬出来的：08:23 与 08:27 两批的 agent.log 都是
        `卡连续 3.0s/3.3s 不在 PCIe 上 —— 不等判决，立刻停战`，而那正是第 3 发（ladder 往深走的
        那一格 vd=363）打出去之后：卡在做它正常的重枚举（presence 记录器同一时刻看到它在 +2~+12 s
        仍然在场），代理却以 3 s 的"空闲档"把这一发抛弃了 ⇒ 区间下沿唯一可能给出 over 的那一发
        永远是 nojudge。看门狗的职责是**别让板子往死卡上打下一发**，不是**抛弃已经打出去的那一发**：
        一发在飞的时候板子本来就不会打下一发（它在等判决），多等一会儿不花任何发数。
        """
        import time as _t
        if not running:
            self._absent_since = None
            return False
        if in_flight is None:
            # 板子的 phase 里，"有一发已经打出去（或正在打）而还没人判"的就是这三档。用板子的话
            # 判断，不用代理自己的 pending：pending 是"判决算好了待盖章"，那一发在飞的时候它是空的。
            in_flight = self.last_phase in IN_FLIGHT_PHASES
        grace = self.post_shot_grace_s if in_flight else self.presence_grace_s
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
        if elapsed < grace:
            return False
        print(f"agent: 卡连续 {elapsed:.1f}s 不在 PCIe 上（config 读回全 1，期间没开 BAR0）"
              f" —— {'那一发还在飞' if in_flight else '空闲'}档宽限期 {grace}s 用尽，停战"
              f"（{'这一发的判决' if in_flight else ''}按 nojudge 记入台账）", flush=True)
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
        # 心跳要躲开波形窗口（见 push_snapshot）：这一行是它唯一的依据，所以必须在读到状态的这里
        # 一起记下来，而不是再发一次 /api/status 去问。
        self.last_phase = str(state0.get("campaign", {}).get("phase") or "")
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
              f"vd={c.get('v_dip')}{self.shot_tags(c)} {self.regs_line()} -> {verdict}",
              flush=True)
        # 心跳的账在这里落地：代理侧的"判过几发、各是什么、几发没盖章"。这不是给日志加料
        # （那些已经有 agent.log），而是让**页面**在批中就看得见代理在干什么 —— 停手那一刻
        # halt 字段是唯一的现场说明，而它必须来自判过每一发的那一方。
        self.shots += 1
        self.tally[verdict] = self.tally.get(verdict, 0) + 1
        if self.stable is False:
            self.dirty += 1
        self.last_verdict = verdict
        self.last_campaign = {k: c.get(k) for k in
                              ("delay", "v_dip", "v_code", "tries", "phase")}
        self.lost_streak = streak
        return True


def run_forever(agent: "VerdictAgent", poll: float, max_lost: int) -> int:
    """The poll loop, split out of main() so the two stop paths are testable."""
    agent.max_lost = max_lost      # single source of truth: step() uses agent.max_lost
    # poll<=0 必须抬到下限：下面那个 presence 看门狗跑在 `while monotonic() < now+poll` 的内圈里，
    # poll=0 时内圈一次都不执行 —— 于是"卡已经离开总线也照样把整批发完"这个唯一的保护**静默消失**。
    # 今天就是被这个坑到的：一个测试传 poll=0，循环既不判停也不看门狗，永久空转。
    poll = max(float(poll), 0.05)
    # 一上来先发一次心跳：页面的"判决方"那一格在此之前只能显示"本次开机以来没收到过快照"，
    # 而操作员看到的就是"没人判决"——他没法区分"代理没起"与"代理起了但没说话"。
    agent.push_snapshot(force=True)
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
                    agent.halt = "poison"
                    print("agent: POISON — " + str(exc), flush=True)
                else:
                    agent.halt = "fusectrl3"
                    print("agent: FUSECTRL 第三种态 — " + str(exc), flush=True)
                agent.stop_board()
                print("agent: 已让板子停止战役；请把显卡 12V+3V3 断电 >=30s 再上电（README 5.1）",
                      flush=True)
                agent.push_snapshot(force=True)
                return 3
            except (OSError, ValueError) as exc:
                print(f"agent: {exc}", flush=True)
            agent.push_snapshot()          # 1 Hz，best-effort；失败绝不影响判决
            if agent.reacted:
                # 摸到窗口边就退出，不只是"叫板子停手"：crack.sh 看到代理还在，会把判决会话
                # 再撑一轮，而我们要的是**冻结** —— 下一发的坐标得由人（或包络的精修模式）挑。
                agent.halt = "reacted"
                print("agent: 已停火。卡若还应答 config，就先只读抓窗口（bench/window.sh），"
                      "别急着重开战役撞同一族。", flush=True)
                agent.push_snapshot(force=True)
                return 4
            if agent.lost_stopped:
                # campaign.c turns a lost verdict into `over` (line 271), so a card that
                # left PCIe would make the ladder climb toward deeper dips while every
                # remaining shot is fired at an absent card. That is how 10 shots were
                # once spent buying 10 "lost". The stop request itself went out inside
                # step(), in the same iteration as the verdict — waiting for this line is
                # what cost a real extra shot on 2026-11-20 (tries=2, board.last seq=2,
                # one pulse nobody ever judged).
                agent.halt = "lost"
                agent.push_snapshot(force=True)
                return 2
            # 判决驱动的停手救不了"已经打出去的那一发"，所以等待期间也得起看门狗（0.3 s 一跳，
            # 不是 2 s：卡的异步死是发后几百毫秒到几秒，2 s 采样等于让它多打一发）。
            deadline = time.monotonic() + poll
            while time.monotonic() < deadline:
                if agent.presence_sample(agent.running):
                    print("agent: presence 看门狗停战（config 全 1，BAR0 一次没花）", flush=True)
                    agent.note_unjudged("停战时卡已离链")      # 悬着的那一发必须进台账
                    agent.halt = "presence"
                    agent.push_snapshot(force=True)
                    return 2
                time.sleep(0.3)
    except KeyboardInterrupt:
        agent.halt = "keyboard"
        agent.push_snapshot(force=True)
        return 0


def build_parser():
    """参数与它们的默认值（默认值一律来自 shot_policy）。单独成函数是为了让测试能直接校这些规则：
    2026-11-26 一条 `--max-bar0-sessions >= 1` 的校验把合法模式挡在门外，整批发了一发都没打。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", nargs="?", default="http://192.168.4.1")
    parser.add_argument("--bdf", default="0000:01:00.0",
                        help="PCI address of the target card")
    parser.add_argument("--poll", type=float, default=0.2,
                        help="seconds between status polls")
    parser.add_argument("--timeout", type=float, default=4.0,
                        help="HTTP timeout in seconds")
    P = shot_policy.get          # 每发节奏的唯一出处（host/ga100vfi/shot_policy.py）
    parser.add_argument("--max-lost", type=int, default=P("max_lost"),
                        help="stop the campaign after this many consecutive 'lost' verdicts "
                             "(0 disables). Default 1 because a shot fired at a card that "
                             "already left PCIe is the archive's poison recipe (docs/UNLOCK_AND_POISON.md §四 "
                             "规则 2), and the verdict that would have stopped the campaign "
                             "arrives 1-2 s before the next physical attempt anyway.")
    parser.add_argument("--first-read-delay", type=float, default=P("first_read_delay_s"),
                        help="seconds to leave the card alone after the shot before the first "
                             "BAR0 read. The board's settle (restore_wait_ms) already runs "
                             "first; this is the extra margin for the card's own boot, during "
                             "which a fuse-block read is exactly what 红线 1 forbids.")
    parser.add_argument("--confirm-reads", type=int, default=P("confirm_reads"),
                        help="how many agreeing BAR0 readings a verdict needs before it is "
                             "stamped (1 = the pre-0.5.5 single read). Only readings that are "
                             "NOT the healthy baseline are re-read: a lost or partial shot "
                             "never gets an extra BAR0 session (红线 1, and the 15 sessions "
                             "one dying shot used to open on 2026-11-20).")
    parser.add_argument("--no-peer-heartbeat", action="store_true",
                        help="do not push the @A snapshot to the board. Off by default: the "
                             "board's page can only tell 判决方在线 from that snapshot, and a "
                             "batch that runs with no heartbeat shows 未运行 on the wall even "
                             "while shots are flying. Disable only for debugging.")
    parser.add_argument("--stable-s", type=float, default=P("stable_s"),
                        help="gap between the two readings of a shot that moved something; a "
                             "read-too-early value (CRC=0x57/0x75) does not repeat, a real one "
                             "does")
    parser.add_argument("--ready-poll", type=float, default=0.25,
                        help="seconds between token/presence checks inside the silence floor, "
                             "so the read lands when the card actually comes back instead of "
                             "on the next --poll tick")
    parser.add_argument("--max-bar0-sessions", type=int, default=P("bar0_sessions_per_shot"),
                        help="BAR0 sessions allowed per verdict; 0 = config-only verdict (never "
                             "opens BAR0 at all, verdict is unsealed by definition). After the "
                             "budget is used up the agent only polls config space. One lost shot "
                             "used to open 15 (实测 2026-11-20 waiting 段 28 s ≈ 15×2 s) — "
                             "repeated BAR0 access against a card that is leaving the bus is the "
                             "hang from logs/02.")
    return parser


def validated_args(argv=None):
    """解析 + 校验。校验规则单独可测：**合法模式不许被校验拒掉**（2026-11-26 那条 `>= 1` 让
    config-only 那批一发都没打，症状只是"批已结束 +30s"，代价是一个电源周期）。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    url = urllib.parse.urlparse(args.board)
    if url.scheme not in ("http", "https") or not url.netloc:
        parser.error("board must be an http:// or https:// URL")
    if not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", args.bdf):
        parser.error("--bdf must have the form 0000:01:00.0")
    if args.poll <= 0 or args.timeout <= 0:
        parser.error("--poll and --timeout must be positive")
    if args.first_read_delay < 0 or args.max_bar0_sessions < 0:
        # 0 是合法值 = config-only 判决（一发一次熔丝块读都不开）。2026-11-26 这里写着 >= 1，
        # 于是规程把 bar0_sessions_per_shot 设成 0 的那一批【一发都没打出去】：agent 起不来，
        # crack.sh 在预热段就 exit 1，症状只是"批已结束 +30s"。参数校验拒绝合法模式，付的是一个
        # 电源周期。所以现在：0 放行，并且把"盖章"这个概念一起关掉（见下面）。
        parser.error("--first-read-delay must be >= 0 and --max-bar0-sessions >= 0")
    if args.stable_s < 0 or args.ready_poll <= 0:
        parser.error("--stable-s must be >= 0 and --ready-poll > 0")
    if args.max_bar0_sessions == 0:
        args.confirm_reads = args.max_bar0_sessions   # 一次都不读：盖章次数不可能超过读的次数
    elif not 1 <= args.confirm_reads <= 3:
        parser.error("--confirm-reads must be 1..3 when BAR0 is read at all")
    elif args.max_bar0_sessions < args.confirm_reads:
        # 复核要用掉第二个会话；预算比复核次数还小，等于每一条判决都盖不了章。
        parser.error("--max-bar0-sessions must be >= --confirm-reads to stamp a verdict")
    return args


def main(argv=None) -> int:
    args = validated_args(argv)
    agent = VerdictAgent(args.board, args.bdf, args.timeout, args.max_lost,
                         first_read_delay=args.first_read_delay,
                         max_bar0_sessions=args.max_bar0_sessions,
                         ready_poll_s=args.ready_poll,
                         confirm_reads=args.confirm_reads,
                         stable_s=args.stable_s)
    if args.no_peer_heartbeat:
        agent.peer_every_s = 0.0
    print(f"agent: board={agent.board} bdf={agent.bdf}", flush=True)
    # 每一批的 agent.log 都必须自报"这一批一发花几次熔丝块读、什么时候开第一次"。这条不是装饰：
    # 台账里出现过"我以为没读、其实读了"的形状，而事后没人能从寄存器反推出当时的读时机。
    if agent.max_bar0_sessions == 0:
        print(f"agent: 本批是 config-only 判决：一发一次 BAR0 都不开（连 restore 写 COMMAND 都不做），"
              f"探测点 = 发后 {agent.first_read_delay:g}s；判决天生未盖章（stable=-），"
              f"命中/反应要靠批末那一次单独的 burn status。", flush=True)
    else:
        print(f"agent: 每发 BAR0 会话预算={agent.max_bar0_sessions} 次，"
              f"首次读时机=发后 {agent.first_read_delay:g}s，盖章复读={agent.confirm_reads} 次"
              f"（间隔 {agent.stable_s:g}s）", flush=True)
    # 序号接着板子上那份数（见 seed_peer_seq）：换一个进程就把 #5 数回 #3，页面看起来像板子重启过。
    seeded = agent.seed_peer_seq()
    # 心跳成没成立要留在 agent.log 里：页面显示"未运行"时，这一行就是分清"代理没起"、
    # "起了但板子不收"、与"板子收了但过期"三件事的第一手证据。
    if agent.push_snapshot(force=True):
        print(f"agent: 心跳已交给板子（@A/HTTP，从 seq={seeded} 接续，"
              f"{agent.peer_frame()[:120]}…）", flush=True)
    else:
        print("agent: [!] 心跳没交上去（不影响判决，但 WIFI 页面会显示没有判决方）："
              f"{agent.peer_err or 'disabled'}", flush=True)
    return run_forever(agent, args.poll, args.max_lost)


if __name__ == "__main__":
    raise SystemExit(main())
