"""安全包络与升级策略：把"往哪打、能不能打、打完怎么总结"变成代码，而不是我每轮即兴决定。

为什么要有这个模块（2026-11-21 的血账，四条都是被要求"进代码"的判断）：

  1. 熔丝不可逆、卡会硬死，硬死一次就要人拔电（今天拔了 5 次）。所以能量必须**从安全一侧
     单轴往外走**，撞死一次就说明这一档太深，要复盘降档，不许在同一坐标白烧重试。
  2. 今天上午我把 td=+10 当成"更浅更安全"放了进去 —— 它其实是当时**最危险**的一档（释放后
     轨低太久 = 卡死），那一发四秒后把卡带走。方向搞反不是浪费发数，是白死一次卡。所以
     "谁比谁危险"必须是数据 + 一条单调性测试，不能是我的直觉。
  3. 「贴着安全那边使劲打，打多了自然就随机进去了」：升档的唯一理由是**浅档已经打了足够多、
     且没有任何反应**（DSOV 没动、CRC 没被打脏）。没有这个证据就不许升 —— 这个判据在下面
     `allowed_rungs()` 里，不在我脑子里。
  4. 「每次要总结」：`summary()` 每次跑完自动出，漏不掉。

实测到的边界（本卡 0x2d，第二张 10de:20c2）：
  * 活着回来并给出判决：pw<=198、delay=117、td<=0、pulses=3（今天 ~55 发）
  * 卡硬死（config ffff:ffff，PEX_RST 叫不回，必须拔电）：pw>=400 @ vd 354/356/357、
    td=+10、delay=84/92/100 那几簇
  * 判决 unknown（卡冷上电、熔丝控制器没起来）不是坐标问题，是 FUSECTRL=0x00000000 ——
    开火前用板子 PEX_RST 2 s 拉一次，别拿坐标去试
"""
from __future__ import annotations

import os
import random
import re
import sys
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- 判决台账

# verdicts.tsv 的一行：  05:12:44\tvd=354 pw=198 d=117 p=3 -> under slip=48/5
LINE = re.compile(
    r"^\s*(?P<t>\d{2}:\d{2}:\d{2})\s+vd=(?P<vd>-?\d+)\s+pw=(?P<pw>-?\d+)\s+"
    r"d=(?P<delay>-?\d+)\s+p=(?P<pulses>\d+)\s*->\s*(?P<verdict>[a-z]+)"
    r"(?:\s+(?P<slip>slip=\d+/?\d*))?")


@dataclass
class Shot:
    t: str
    vd: int
    pw: int
    delay: int
    pulses: int
    verdict: str                      # under / over / hit / lost / dead / poison / unknown / ''
    slip: str = ""
    td: int = -60                     # 台账行里现在还不带 td（判决行只写 vd/pw/d/p）
    dsov: int = None                  # 判决当时读到的原始遥测；None = 那一发没读到（lost/dead）
    crc: int = None

    @property
    def fired(self) -> bool:
        """这一发是否真的落在卡上（无判决 = 卡被打死或根本没走到开火）。"""
        return bool(self.verdict)

    @property
    def progress(self) -> bool:
        """有没有"反应"：**判决词，或者 DSOV 离开基线**。

        lost/dead 不算反应 —— 它们不是深度证据（今天的 over 语义是 DSOV=0 + CRC 坏或掉链，
        而 lost 只是没读回来）。把它们当反应会导致"死了一堆却以为在进步"。

        为什么**不**把 CRC 读脏算进来（2026-11-21 用真台账验出来的）：把 `CRC != 0x55` 当成反应
        之后，99 发里有 30 发"有反应"，横跨 `delay=54/75`、`vd=339..374`、值有 `0x56/0x57/0x75`
        四种且下一发又自己回到 `0x55`。那不是"摸到边界"，那是**我们趁控制器还在忙的时候读**——
        是判决可靠性问题（见 dirty_read）。而 `DSOV` 99 发一次没动过，它才是往窗口走的证据。
        """
        if self.verdict in ("over", "hit", "goldilocks"):
            return True
        return self.dsov is not None and self.dsov != HEALTHY_DSOV

    @property
    def dirty_read(self) -> bool:
        """判决当时 CRC 不是基线值：**这一发的判决要打折**，但它不是深度证据。

        它说的是"读得太早"，不是"打得太深"。当成反应会冻掉加深，当成无反应又会高估这一发 ——
        所以它单独占一条判据，两边都不许借用。
        """
        return self.crc is not None and self.crc != HEALTHY_CRC

    @property
    def telemetry(self) -> str:
        """总结里引用这一发的遥测；没读到的那些要显式说"没读到"，不能印个 0 冒充合法零。"""
        d = "-" if self.dsov is None else f"0x{self.dsov:x}"
        c = "-" if self.crc is None else f"0x{self.crc:x}"
        return f"DSOV={d} CRC={c}"


def parse_verdicts(text: str) -> list:
    """台账文本 → 发数记录。读不懂的行静默跳过：台账是给人 also 看的。"""
    out = []
    for m in (LINE.match(l) for l in text.splitlines()):
        if not m:
            continue
        g = m.groupdict()
        out.append(Shot(g["t"], int(g["vd"]), int(g["pw"]), int(g["delay"]),
                        int(g["pulses"]), _normalize(m.string, g["verdict"]),
                        g.get("slip") or "",
                        dsov=_seen(DSOV_SEEN, m.string), crc=_seen(CRC_SEEN, m.string)))
    return out


# --------------------------------------------------------------------------- 包络

@dataclass(frozen=True)
class Rung:
    """一档坐标。档位序号就是"危险等级"：0 最安全，越大越深。"""
    id: int
    note: str
    pw: int
    vd: int
    delay: int
    td: int = -60
    pulses: int = 3
    gap: int = 150

    def key(self):
        return (self.id, self.vd, self.pw, self.delay, self.td, self.pulses)


# --------------------------------------------------------------------------- 一条经验，不是一个数
# **配方每张卡都可能不一样**，所以这里不把某一次的命中值写成宇宙律；写进来的是"怎么知道一组
# 参数没在 work"的判据。今天付的学费就是这个判据本身：
#
#   我借了另一份文档里**作者台**的 `delay=117 / pw=198`（`20261119-…md` §2），在**本卡本台**上
#   连打 60+ 发：`DSOV` 从未离开 1、`CRC` 从未被打脏，而坐标从 374 到 366 都会掉链、slip 47 与
#   236 都会死，四次硬死全在这一族。
#
# 这就是文档里定义过的 **over**（`over = DSOV=0 且 CRC 坏 或 PCIe 链路丢`，`docs/ADAPTIVE.md`），
# 而 over 的处置是"变浅、不许在该态连打"（`20261119-…md` §4）。我却把它当随机死亡，在同一族里
# 一路加深 —— 换句话说：**零反应 + 掉链 = 标定/相位问题，再加发数只会买到更多 over。**
#
# 于是规则是**按族冻结**（族 = pw × 相位档），而且判据只用**事故**，不用"有没有反应"：
#
#   **命中本身是概率事件。** 同一个几何可能打几十发才开一次窗，所以"发了 N 发零反应"不能反推
#   "配方错"—— 那样会把概率当因果，把唯一能打的边缘区冻掉。反过来，`poison`（控制器被打进半
#   枚举）与 `dead`（要拔电）是确定性事故：它们告诉你这个形状会伤卡，与运气无关。
#
# 真实台账（116 发）就是这么分开的：
#   * pw=222 相位档 40：56 发，poison+dead=**0**（丢过 37 发链）—— 边缘区，放开使劲打；
#   * pw=198 相位档 100：39 发，poison+dead=**5** —— 冻结（今天四次拔电都在这族）；
#   * pw=198 相位档 80：5 发，poison+dead=**4** —— 冻结，不用等第 12 发。
#
# 我在这个判据上犯过一次方向性错误，记下来避免重犯：一度把 `lost` 也当事故，结果把 pw=222 那
# 最安全的一族冻掉了。lost 的深度从 vd=339 一路铺到 vd=380（最浅那发），**与深度无关**，它是
# 链路/台架现象，不是配方证据；只在总结里当链路健康报数，停批由 crack.sh 的"连续 lost 停批"管。
FREEZE_AFTER = 12                  # "打很多了没反应"里那个"很多"（也是升档门槛）
FREEZE_FATAL = 2                   # 零反应 + 换来的是拔电，才算标定失败
POISON_AFTER = 3                   # 发数少但战绩一边倒：三发里两发把卡打死就够说明相位不对
POISON_FATAL_RATE = 2 / 3
# **只有 poison / dead 算族级硬证据。** `lost`（链路丢）不算：本台真实台账里 pw=222 那一族 56 发
# 丢了 37 发链却零毒零死，而 lost 的深度从 vd=339 一直铺到 vd=380（最浅那发）—— 与深度无关的
# 东西不能当深度判据。它更像链路重训练/台架的问题，判据留在 crack.sh 的"连续 lost 停批"那里。
# 文档给 over 的两个分支（DSOV=0 且 CRC 坏 / 链路丢）的处置都是"变浅"，都不等于"重标 delay"；
# 真正指向"波形落在错相位上"的是 poison（0x87400080 那种半枚举态）—— 今天 8 发全在 pw=198 那一族。
FATAL_CLASS = ("poison", "dead")
OVER_CLASS = ("over", "lost", "dead", "poison")   # 只用于总结里的链路健康统计

# 这张卡**没被打之前**的遥测基线（2026-11-21 两次独立零发实测：06:32 window.sh 只读抓证据、
# 06:53 readtrip.sh 判决链重放，都是 `DSOV=1 CRC=0x55 FBP_DEFECTIVE=0x2d`）。"有没有反应"就是
# 相对这两个数定义的：DSOV 离开 1（往 0 走=往命中走）或 CRC 离开 0x55（被打脏=over 的前兆）。
# 写在这里而不是散在判据里，是因为换一张卡这两个数就变了 —— 变的是常量，不是判据。
HEALTHY_DSOV = 1
HEALTHY_CRC = 0x55
DSOV_SEEN = re.compile(r"\bDSOV=0x([0-9a-fA-F]{1,8})")
CRC_SEEN = re.compile(r"\bCRC=0x([0-9a-fA-F]{1,8})")


def _seen(pat: re.Pattern, line: str):
    """从判决行里取原始遥测；没写就是 None（不能当 0 —— 合法零和"没读到"必须分得开）。"""
    m = pat.search(line)
    return int(m.group(1), 16) if m else None

# 相位按 20 µs 归族：本台的相位精度本来就只有几十 µs（slip 动辄 ±5..240），把 84 和 92 当成两
# 个独立"族"是装作比数据更精细，结果就是四发全毒的战绩被拆成四个 n=1 的族，谁都冻不掉。
PHASE_BIN = 20


def phase_bucket(delay: int) -> int:
    return (int(delay) // PHASE_BIN) * PHASE_BIN


# 有本台标定记录的相位档（提示用，不是白名单 —— 配方每张卡不一样）。54 与 45 同一个档：
#   * 54 = 2026-11-16 本台架打出 Goldilocks 那一发（CMP170HX 经验 §3.3，crack.sh 默认值）；
#   * 45 = 2026-11-20 同一族（运行目录 20261120-*crack-0.3.0-3x222*）。
# 这个档曾经的战绩是"60+ 发零 poison 零 dead（丢过 20+ 发链）"，所以两天下来的拔电看似全在别处。
# **2026-11-21 这个结论作废**：同一族连着两次把卡打成硬死（vd=374 与 vd=368，都是拔电后的第一发），
# 于是它现在被 family_freeze 规则 A 冻着。留着这条记录是因为它仍然记着"本台架唯一打出过命中的相位"
# —— 冻结说的是"别再原样重打"，不是"这一族从未 work 过"。
CAL_BUCKETS = {40: "本台架打过 60+ 发，2026-11-16 在这里打出过 Goldilocks；"
                   "2026-11-21 起它不再是零硬史（两次硬死都是拔电后的第一发）"}

MIN_SHOTS_TO_ESCALATE = FREEZE_AFTER   # 同一个数两种用途：攒够才升档 / 攒够仍无反应就冻结


def family_stats(shots: list, pw: int, delay: int) -> dict:
    """一个族（pw × 相位档 = 单发能量 × 相位）到今天为止的战绩。

    **已知缺口（2026-11-24，第三张卡）**：族键里没有 `pulses`，而脉冲数是独立的一维致死量。
    那张卡在 `pw=222` 相位档 40 这一族里同时有 `3×222@vd=380 -> dead` 与 `1×222@vd=380 ->
    under`（同深度、同 td、同 gap，只差脉冲数；命中的那发在相位档 80，也是 `1×222`）。所以本
    函数汇总出的"这一族的事故率"混合了两种不同形状 —— 用它做决定前得回看运行目录里的逐发
    坐标，别把族号当形状号。把 pulses 收进族键是判据变更（会改冻结行为），要先动
    host/tests/test_envelope.py。
    """
    fam = [s for s in shots if s.fired and s.pw == pw and phase_bucket(s.delay) == phase_bucket(delay)]
    return {"n": len(fam),
            "responded": any(s.progress for s in fam),
            "fatal": sum(1 for s in fam if s.verdict in FATAL_CLASS),
            "lost": sum(1 for s in fam if s.verdict == "lost"),
            "dirty": sum(1 for s in fam if s.dirty_read)}


def family_freeze(shots: list, pw: int, delay: int) -> str:
    """这一族是不是**会伤卡**、该停手（而不是"它没反应所以再深点试试"）。

    两条规则都不含具体电压，只看**确定性事故**（poison/dead）：
      A. 发数攒够（>=12）且有 >=2 发把卡打进 poison/dead；
      B. 发数少但一边倒（>=3 发里 >=2/3 是事故）。
    为什么不用"零反应"当判据：**命中是概率事件**，同一几何可能几十发才开一次窗，用反应率反推
    配方会把边缘区冻掉（今天真的错过一次）。事故不是概率事件 —— 会打出毒态的形状继续打就是继续
    买拔电。`lost` 不参与判据（见 FATAL_CLASS 注释），只在总结里报链路健康。
    """
    st = family_stats(shots, pw, delay)
    if not st["n"] or st["responded"] or not st["fatal"]:
        return ""
    rate = st["fatal"] / st["n"]
    why = ""
    if st["n"] >= FREEZE_AFTER and st["fatal"] >= FREEZE_FATAL:
        why = f"{st['n']} 发里有 {st['fatal']} 发把卡打进 poison/dead"
    elif st["n"] >= POISON_AFTER and rate >= POISON_FATAL_RATE:
        why = f"才 {st['n']} 发就有 {st['fatal']} 发（{rate:.0%}）把卡打进 poison/dead"
    if not why:
        return ""
    drop = f"（该族另有 {st['lost']} 发丢链：与深度无关，不算事故）" if st["lost"] else ""
    return (f"pw={pw} 相位档≈{phase_bucket(delay)} 这一族：{why} —— 事故不是运气，这个形状会伤卡。"
            + drop + _swap_advice(shots))


def _swap_advice(shots: list) -> str:
    """"换形状再打"该换成什么 —— 从台账现算，不写死。

    这里曾经写死成"零事故的那族是 pw=222"。2026-11-21 那天 pw=222 用两次拔电把这个结论作废了，
    而提示照旧输出，等于拿着过期地图指挥开火。 widths 的战绩会变，所以让它每次都从数据里长出来。
    """
    fired = [s for s in shots if s.fired]
    bad = {s.pw for s in fired if s.verdict in FATAL_CLASS}
    good = sorted({s.pw for s in fired} - bad)
    parts = []
    if bad:
        parts.append("打出过事故的宽度：" + "、".join(f"pw={p}" for p in sorted(bad)))
    if good:
        parts.append("本台账里零硬史的宽度：" + "、".join(f"pw={p}" for p in good) + "（发数少不等于安全，只是还没打过）")
    return ("换形状再打：" + "；".join(parts)) if parts else ""

# 从安全侧往死机侧排。`vd` 这一栏是**这一档允许到达的最深点**（floor，vd 越小越深）：板载
# campaign 一次扫一片网格（minv..maxv），要审的是"这批里最深的那一发"，逐条列坐标写不完也审不住。
#
# 整张表都待在**本台架唯一有战绩的相位族**（pw=222、delay≈45/54，2026-11-20 那 11 发 0 over；
# 2026-11-16 的 Goldilocks 也在同一相位档）：今天被打过 25 发的 pw=198/delay=117 那一族已经被
# family_freeze 冻住，所以它连一格功课都攒不上 —— 这正是我想要的效果：**借来的相位不算数**。
# 换一张卡、换一组相位时改的是这张表的 delay 栏，判据本身不用动。
RUNGS = (
    Rung(0, "标定族的浅侧：先在这里攒够发数", 222, 338, 54, td=-40),
    Rung(1, "2026-11-16 在本台架打出过 Goldilocks 的那一发", 222, 334, 54, td=-40),
    Rung(2, "比命中间深一档：必须先在最深处见过反应", 222, 330, 54, td=-40),
    # 加宽只许待在浅侧：pw 是最强致死量，动它的同时必须把深度还回去（时长轴与深度轴不一起动）。
    Rung(3, "加宽 260 但只许在浅侧", 260, 338, 54, td=-40),
    # td>0 一族永远进不去（见 FATAL），留在表里是记录：2026-11-21 上午我把它当"更浅"放了进去。
    Rung(4, "释放后轨还低着（td>0）：那一发四秒后把卡带走", 222, 334, 54, td=10),
)

# 坐标本身的一票否决（与"这一族有没有 work"无关，是形状本身的危险）。
FATAL = (
    (lambda c: c.pw >= 400 and c.vd <= 357, "pw>=400 在 vd<=357 上打死过卡（356/450、357/400）"),
    (lambda c: c.td > 0, "td>0 = 释放后轨还低着，>~100us 就是卡死（td=+10 那发四秒后异步死）"),
)


@dataclass
class Verdict:
    ok: bool
    reason: str
    rung: Rung = None
    shots_at_or_below: int = 0


def _fatal_reason(coord) -> str:
    for test, why in FATAL:
        if test(coord):
            return f"{why}（本次 vd={coord.vd} pw={coord.pw} delay={coord.delay} td={coord.td}）"
    return ""


def rung_of(shot):
    """把一发判决归到**唯一**一档：它的 pw + 它自己到达的最深点。

    归挡必须唯一，否则一档的功课能被另一档借用：pw=260/vd=362 那 12 发同时满足 rung1(下限
    362) 和 rung2(下限 354)，"每档都攒够 12 发"就变成一句话不用真的打够。今天想要的正是
    "浅档使劲打"的诚实计数，所以这里按覆盖它的最省力一档归一。
    """
    same_pw = [r for r in RUNGS if r.pw == shot.pw and r.td <= 0]
    cands = [r for r in same_pw if r.vd <= shot.vd]
    return min(cands, key=lambda r: r.id) if cands else None


def shots_for(shots: list, rung: Rung) -> int:
    """这一档实际打了几发（只算真落在卡上的；无判决的那些由 stop_hard/停批另行处理）。"""
    return sum(1 for s in shots if s.fired and not _fatal_reason(s) and rung_of(s) is rung)


def _covering(shots: list, pw: int, deepest_vd: int):
    """覆盖这次开火请求（pw 与最深点）的最省力一档；表里没有对应形状时给出原因。"""
    same_pw = [r for r in RUNGS if r.pw == pw and r.td <= 0]
    if not same_pw:
        return None, f"pw={pw} 不在包络表里：先在 host/ga100vfi/envelope.py 的 RUNGS 里登记它" \
                     f"（带它比哪一档更危险的理由），再来打"
    cands = [r for r in same_pw if r.vd <= deepest_vd]
    if not cands:
        return None, (f"最深点 {deepest_vd} 比表里 pw={pw} 的最低档 "
                      f"{min(r.vd for r in same_pw)} 还深：那是没登记过的深度，先登记并说明理由")
    return min(cands, key=lambda r: r.id), ""


def allowed_rungs(shots: list, min_shots: int = MIN_SHOTS_TO_ESCALATE) -> list:
    """返回当前**允许开火**的档位（安全侧连续攒够发数、且没有任何反应，才一格一格往前让）。

    三条规则，都是今天用发数/拔电换来的：
      * 最浅一档永远允许 —— 先攒发数，别一上来就挑深坐标；
      * 想进第 i 档，比它浅的**每一**档都得攒够 min_shots 发（不许跳档：跳档就是今天上午
        那次"以为 td=+10 更浅"的方向错误）；
      * 台账里出现过任何一次反应（DSOV/CRC 动过）就冻结加深 —— 摸到边了该停下精修。
    """
    out = [RUNGS[0]]
    progressed = any(s.progress for s in shots)
    for r in RUNGS[1:]:
        if progressed or _fatal_reason(r):
            break
        if not all(shots_for(shots, x) >= min_shots for x in RUNGS if x.id < r.id):
            break
        out.append(r)
    return out


def check(coord, shots: list, min_shots: int = MIN_SHOTS_TO_ESCALATE,
          allow_unranked: bool = False, grid=None) -> Verdict:
    """这一次开火（一个 pw + 一片 vd 网格里的**最深点**）现在能不能打。

    审的对象是一次 campaign 请求，不是单个坐标：板载 ladder 自己会在 minv..maxv 里扫，逐条
    列坐标既写不完也审不住。所以取三个量：pw（最强致死量）、最深 vd、delay/td。
    """
    why = _fatal_reason(coord)
    if why:
        return Verdict(False, f"已知致死坐标：{why}", None, 0)
    # 族级冻结排在"形状没登记"之前：`--allow-unranked` 绕的是"这个宽度我没登记过"，不该顺带绕开
    # "这一族的战绩已经证明它相位不对" —— 人工放行是我替自己担保形状，不是替一族的历史担保。
    fr = family_freeze(shots, coord.pw, coord.delay)
    if fr:
        return Verdict(False, fr, None, family_stats(shots, coord.pw, coord.delay)["n"])
    r, why = _covering(shots, coord.pw, coord.vd)
    if r is None:
        if not allow_unranked:
            return Verdict(False, why, None, 0)
        return Verdict(True, f"未登记形状 pw={coord.pw}，人工放行：{why}", None, 0)
    if r not in allowed_rungs(shots, min_shots):
        need = min_shots * len([x for x in RUNGS if x.id < r.id])
        got = sum(shots_for(shots, x) for x in RUNGS if x.id < r.id)
        return Verdict(False, f"{r.note} 还不许打：浅档只攒了 {got} 发，要 {need} 发且无反应",
                       r, got)
    burned = [s for s in shots if s.fired and rung_of(s) is r and not _fatal_reason(s)]
    # 禁火禁在**坐标**上，不是禁在档上。05:25 打死卡的是 vd=366/pw=198/d=117 这一个点；按档锁会
    # 把整个最安全档（pw=198）一起锁死，那等于逼我去打更宽更深的档 —— 和"贴着安全侧使劲打"方向
    # 正好相反，是最危险的失效模式。
    #
    # 范围要覆盖整片被扫到的网格：ladder 会在 minv..maxv 里自己走，只审最深点等于放过中间那些
    # 打死过卡的坐标（死过一次的位置不会因为这次起点浅就变安全）。
    lo, hi = (grid if grid else (coord.vd, coord.vd))
    killer = [s for s in shots if s.fired and s.pw == coord.pw and s.delay == coord.delay
              and lo <= s.vd <= hi and s.verdict in ("dead", "poison")]
    if killer:
        ks = ", ".join(f"vd={s.vd}->{s.verdict}" for s in killer[-4:])
        return Verdict(False, f"这批网格里有打死过卡的坐标（pw={coord.pw} d={coord.delay}："
                              f"{ks}）—— 改深度范围绕开它，不许原样重打", r, len(burned))
    same = [s for s in burned if (s.vd, s.pw, s.delay) == (coord.vd, coord.pw, coord.delay)]
    last = same[-1].verdict if same else ""
    if last in ("dead", "poison"):
        return Verdict(False, f"同一个坐标（vd={coord.vd} pw={coord.pw} d={coord.delay}）最近一发是 "
                              f"{last} —— 换坐标（改深度或换档），不许原样重烧", r, len(burned))
    # 禁火按坐标（上面 killer 那条就是全部判据）：同一坐标打死/毒过一次就永远不许重打（规则 4
    # 「不许同一坐标白烧重试」），并且被扫的整片网格里不许含这种坐标。
    # 族级规则（family_freeze）已经在最前面判过 —— 它数的不是"死了几次"而是**有没有反应**：
    # 死卡在本卡上太钝（三发里死一发），拿死亡当反馈只会把我推向"更浅"这种没有信息量的方向；
    # "发了 N 发、DSOV/CRC 一次没动、还净是掉链"才是明确的标定失败信号，而且它给的下一步是
    # "回有标定的相位"，不是"更激进"。
    note = ""
    if phase_bucket(coord.delay) not in CAL_BUCKETS:
        note = (f"（提示：相位档 {phase_bucket(coord.delay)} 没有本台标定记录，"
                f"有记录的是 {sorted(CAL_BUCKETS)}）")
    return Verdict(True, f"允许：{r.note}（pw={r.pw}，最深 {r.vd}，已打 {len(burned)} 发）{note}",
                   r, len(burned))


# ------------------------------------------------------------------- 健康区随机扫
#
# 为什么要有这个：命中是概率事件，而"往深里走一条阶梯"把每次下注都押在**悬崖**上 —— 今天所有
# poison 都在 354..358，而卡反复活着回来的地方在 366 以上。既然命中不跟深度走，正确做法是**在健
# 康区里铺开发很多发**，用随机去撞那个窗口，而不是一发一发往死过人的坐标上蹭。
#
# 三条约束（每条都是今天用拔电换来的）：
#   1. 随机只在 HEALTHY_BOX 里走 —— 域的边界是粗筛，不是安全保证；
#   2. 每个候选坐标都必须过 check()：族冻结、按坐标禁火、rung 功课，一条都不许绕；
#   3. 生成器不许自己发明 pw：宽度是最强致死量，换宽度是另一件事（由 RUNGS 登记）。
HEALTHY_BOX = (362, 380)                            # vd 越小越深
SWEEP_DELAY_DOMAIN = tuple(range(40, 141, 4))       # 相位候选；冻掉的档由 family_freeze 现算
SWEEP_STEPS = (2, 4, 6)                             # 只板载 ladder 认得的那些步长
SWEEP_MIN_POINTS = 2
SWEEP_MAX_POINTS = 5


@dataclass(frozen=True)
class Coord:
    """check() 要看的四个轴。单独建一个类是为了让 sweep 能造真坐标，测试也一样。"""
    vd: int
    pw: int
    delay: int
    td: int = -60


@dataclass(frozen=True)
class Sweep:
    """一片被门禁核准过的随机几何；crack.sh 直接 eval 它。"""
    pw: int
    td: int
    delays: tuple
    lo: int
    hi: int
    step: int
    dropped: tuple = ()            # ((delay, 原因), …)：比申请的窄时必须有解释
    refine: tuple = ()             # 见过反应的相位档（非空 = 这一批是精修，不是铺撒）

    @property
    def ok(self) -> bool:
        return bool(self.delays)

    def as_shell(self) -> str:
        """给 crack.sh eval 的赋值。空扫不许静默：宁可让它拒绝开火。"""
        return (f'DELAYS="{",".join(str(d) for d in self.delays)}"\n'
                f'MINV={self.lo}\nMAXV={self.hi}\nSTEP={self.step}\nVD={self.hi}\n')

    def explain(self) -> str:
        d = "; ".join(f"delay={x}：{w}" for x, w in self.dropped)
        head = (f"精修已见反应的相位档 {list(self.refine)}：" if self.refine else "随机扫：")
        return (f"{head}pw={self.pw} delays={list(self.delays)} vd {self.lo}..{self.hi} "
                f"step {self.step}" + (f"（丢掉：{d}）" if d else ""))


def reacted_buckets(shots: list, pw: int) -> set:
    """这个宽度下**已经看见反应**的相位档。它们是接下来唯一该打的地方。

    包络总结里那句"有反应 → 停手，在这档里单轴精修"如果只由人执行，下一批就会被随机扫带去别的
    族 —— 而反应是 111 发才买到的东西，跑掉一次就是再烧一次拔电的预算。
    """
    return {phase_bucket(s.delay) for s in shots
            if s.pw == pw and s.verdict != "void" and s.progress}


def last_reacted_vd(shots: list, pw: int):
    """最近一次反应所在的那个 vd：精修时窗口的锚点。没见过反应返回 None。

    取"最近"而不是最深：反应是概率事件，最深那发可能只是运气，而最近那一发的其余条件（同一电源
    周期、同一波形）就是下一发最该复刻的东西。
    """
    hit = [s for s in shots if s.pw == pw and s.verdict != "void" and s.progress]
    return hit[-1].vd if hit else None


def healthy_sweep(shots: list, pw: int = 222, td: int = -40, box=HEALTHY_BOX,
                  n_delays: int = 4, rng=None, min_shots: int = MIN_SHOTS_TO_ESCALATE,
                  allow_unranked: bool = False) -> Sweep:
    """在健康区里随机取一片坐标，**每个候选都过 check()**，只留门禁放行的。

    随机的是三样：相位候选（从域里洗牌取，冻掉的档直接被 check() 打掉）、电压窗口的落点与宽度、
    ladder 步长。不随机的是 pw 与 td —— 那两根轴动一次就是换一个配方，不是一次抽签。

    有一条优先级压过随机：**只要某个相位档见过反应，候选就只从那些档里取**（`refine`）。随机扫
    的作用是在还没有任何反应的区里铺开找边；边都摸到了还到处抽签，就是把唯一的信号扔掉。这一条
    不放宽也不静默降级：反应档全被门禁挑掉时返回空扫（拒绝开火），让人来决定，而不是偷偷去扫别的族。
    """
    r = rng or random.Random()
    lo0, hi0 = box
    step = r.choice(SWEEP_STEPS)
    pts = r.randint(SWEEP_MIN_POINTS, SWEEP_MAX_POINTS)
    span = (pts - 1) * step
    ref = reacted_buckets(shots, pw)
    anchor = last_reacted_vd(shots, pw) if ref else None
    if hi0 - span < lo0:
        why = f"健康区 {lo0}..{hi0} 放不下 {pts}×{step}"
        if anchor is not None:
            why += f"，而精修必须盖住出过反应的 vd={anchor}"
        return Sweep(pw, td, (), lo0, hi0, step,
                     ((("精修" if anchor is not None else "域"), why),))
    if anchor is None:
        hi = r.randrange(lo0 + span, hi0 + 1, step)
    else:
        # **精修窗口必须盖住出过反应的那个 vd**。只把相位钉在同一档还不够：随机挑一个 374..380
        # 的窗口同样"在同一档"，却离那发 `DSOV=0`（vd=372）两步远，等于换了个不相干深度重抽签。
        # 放不下就拒绝开火 —— 不静默把窗口挪开，那等于随机器自己决定不看那个点。
        hi_lo, hi_hi = max(lo0 + span, anchor), min(hi0, anchor + span)
        if hi_hi < hi_lo:
            return Sweep(pw, td, (), lo0, hi0, step,
                         (("精修", f"健康区 {lo0}..{hi0} 里放不下盖住 vd={anchor} 的 {pts}×{step} 窗口"),))
        hi = r.randrange(hi_lo, hi_hi + 1, step)
    lo = hi - span
    pool = tuple(d for d in SWEEP_DELAY_DOMAIN if phase_bucket(d) in ref) if ref \
        else SWEEP_DELAY_DOMAIN
    cand = [d for d in pool if not family_freeze(shots, pw, d)]
    r.shuffle(cand)
    kept, dropped = [], []
    for d in cand:
        v = check(Coord(vd=lo, pw=pw, delay=d, td=td), shots, min_shots=min_shots,
                  allow_unranked=allow_unranked, grid=(lo, hi))
        if v.ok:
            kept.append(d)
        else:
            dropped.append((d, v.reason))
        if len(kept) >= n_delays:
            break
    return Sweep(pw, td, tuple(sorted(kept)), lo, hi, step, tuple(dropped), tuple(sorted(ref)))


# --------------------------------------------------------------------------- 每批总结

def summary(shots: list, label: str = "") -> str:
    """每批的机器总结（「每次要总结」）。空台账也要能出，别在关键时候抛异常。

    重点不是"死了几次"，是**每个族的反应率**：本卡上死亡太钝（要烧三次才死一次），拿死亡当反馈
    会把我推向"更浅更安全"这种没有意义的方向；而"DSOV/CRC 动没动"是文档里唯一的直接度量。
    """
    by = {}
    for s in shots:
        by[s.verdict or "无判决"] = by.get(s.verdict or "无判决", 0) + 1
    lines = [f"== 总结 {label}：{len(shots)} 发 =="]
    lines.append("   判决分布：" + (", ".join(f"{k}={v}" for k, v in sorted(by.items())) or "无"))
    slips = [s.slip for s in shots if s.slip]
    if slips:
        lines.append(f"   slip 范围：{slips[0]} … {slips[-1]}（最后 5 笔：{', '.join(slips[-5:])}）")
    resp = [s for s in shots if s.progress]
    if resp:
        lines.append(f"   有没有反应：**有**（{len(resp)} 发 DSOV 动了）—— 停手，在这档里单轴精修")
        for s in resp[:6]:
            lines.append(f"      delay={s.delay:>4} vd={s.vd} pw={s.pw} "
                         + (f"DSOV {HEALTHY_DSOV:x}->{s.dsov:x}" if s.dsov is not None
                            and s.dsov != HEALTHY_DSOV else f"判成 {s.verdict}")
                         + f"（{s.telemetry}）")
    else:
        lines.append(f"   有没有反应：无（DSOV 从未离开 {HEALTHY_DSOV:x}）。命中本来就是概率事件："
                     "继续在已放开的档里使劲打，别用发数反推配方")
    dirty = [s for s in shots if s.dirty_read]
    if dirty:
        vals = "/".join(sorted({f"0x{s.crc:02x}" for s in dirty}))
        lines.append(f"   判决可靠性：{len(dirty)} 发读到的 CRC 不是 {HEALTHY_CRC:02x}（{vals}），"
                     "且下一发又自己变回去。这不算反应，算**我们读得太早** —— 这些发的 under 要打折；"
                     "要治它得改判决会话的读时机，不是改波形")
    # 逐族战绩（pw × 相位档）：这才是决定"继续打 / 回标定档 / 升档"的那张表。
    # 报的是**硬事故数**与**丢链数**两栏：前者决定冻结，后者只是链路健康，混在一起过一次方向性错误。
    fams = sorted({(s.pw, phase_bucket(s.delay)) for s in shots if s.fired})
    for pw, buck in fams:
        st = family_stats(shots, pw, buck)
        fr = family_freeze(shots, pw, buck)
        tag = "有本台标定记录" if buck in CAL_BUCKETS else "无标定记录"
        lines.append(f"   族 pw={pw} 相位≈{buck}（{tag}）：{st['n']} 发，硬事故={st['fatal']}，"
                     f"丢链={st['lost']}，脏读={st['dirty']}，反应={'有' if st['responded'] else '无'} → "
                     + (f"**冻结**：{fr}" if fr else
                        ("有反应，精修它" if st["responded"] else
                         ("见过毒态：别原样重打这个形状" if st["fatal"] else
                          "零事故：放开使劲打"))))
    lost = by.get("lost", 0)
    if lost:
        lines.append(f"   链路健康：{lost} 发 lost（读不回来）。它不算事故也不算反应，但一个电源周期"
                     "里能打的发数在掉就是它在掉 —— 那是台架/链路，与配方无关")
    nxt = allowed_rungs(shots)[-1] if allowed_rungs(shots) else None
    lines.append("   下一档：" + (f"rung {nxt.id} {nxt.note}（vd={nxt.vd} pw={nxt.pw} "
                              f"delay={nxt.delay} td={nxt.td}）" if nxt else "只许停在最浅一档"))
    return "\n".join(lines)


# --------------------------------------------------------------------------- 命令行


# --------------------------------------------------------------------------- 历史（两种台账都读）

# crack.sh 的运行目录是自描述的：20261121-050133-crack-0.3.2_3x198_366_d117
# 判决行来自其中的 agent.log： "... session ladder attempt 1 delay=117 vd=366 ... -> under"
RUN_DIR = re.compile(r"crack-[^/]*?(?P<pulses>\d+)x(?P<pw>\d+)_(?P<vd0>\d+)")
# 判决词**不列白名单**：写的一方加一个新词（`dead`、`nojudge`…），读的一方若列清单就会把它
# 静默丢掉 —— 今天"板子打了但判决 0 行"的那一发就是被这样吞掉的：它在台账里根本不存在，于是
# 刚打死卡的那一族在门禁里依然显示零事故。解析出来再分类，别在正则里预先否决。
AGENT_LINE = re.compile(r"attempt (?P<attempt>\d+) delay=(?P<delay>-?\d+) vd=(?P<vd>-?\d+)"
                        r".*?-> (?P<verdict>[a-z_]+)")

# `dead` 被两种事件共用过（2026-11-21 真实台账）：
#   * 卡被波形打死、PEX_RST 也叫不回，只能断电 —— 这才叫事故；
#   * 判决作废：桥 retrain 失败，板子把 PEX_RST 拉 0.3 s 卡就回总线了，判决本身不可信。
# 后者台架**自己就恢复了**，把它记成事故会让"靠 PEX_RST 救回来"的每一次都污染族战绩（本台账里
# pw=222 因此被凭空扣上 5 发事故，门禁拒绝了一个其实零硬死的形状）。读的时候按行内证据降级，
# 不改历史文件：作废就是作废，它等于"打过但没判决"，不是"卡被打死"。
VOID_MARKS = ("判决作废", "relink=")


def _normalize(line: str, verdict: str) -> str:
    if verdict == "dead" and any(k in line for k in VOID_MARKS):
        return "void"
    return verdict


def load_history(runs_glob: str) -> list:
    """把历次 crack.sh 运行目录读成发数记录。

    为什么必须读目录名：**判决行里不写 pw**，pw 只在目录名里。包络的门禁恰恰是按 pw 分档
    的（它是最强致死量），所以只读 agent.log 会得到"所有发都是同一个未知宽度"，攒够发数
    这件事就永远是假的。
    """
    import glob
    out = []
    seen = set()
    for d in sorted(glob.glob(runs_glob)):
        m = RUN_DIR.search(os.path.basename(d))
        if not m:
            continue
        pulses, pw = int(m.group("pulses")), int(m.group("pw"))
        for name in ("agent.log", "ladder.log"):
            try:
                with open(os.path.join(d, name), encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except OSError:
                continue
            for line in lines:
                mm = AGENT_LINE.search(line)
                if not mm:
                    continue
                g = mm.groupdict()
                v = _normalize(line, g["verdict"])
                # 同一次事故可能被写好几遍（判决 409 之后代理会重试；agent.log 与 ladder.log
                # 也各有一份副本）。发数按 (目录, attempt, 判决) 归一：一次事故 = 一发。
                key = (d, g["attempt"], v)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Shot("", int(g["vd"]), pw, int(g["delay"]), pulses, v,
                                dsov=_seen(DSOV_SEEN, line), crc=_seen(CRC_SEEN, line)))
    return out


def load_shots(ledger: str = "", runs_glob: str = "") -> list:
    """台账优先、运行目录只补台账没有的部分 —— 顺序与去重都是判据的一部分。

    两条都要写死，因为它们各自会让门禁变成假的：

    1. **同一发不能数两遍。** verdicts.tsv 是判决代理每发实时写的（带 pw、带时间），运行目录里
       的 agent.log 是同一次判决的副本。两边直接相加 = 发数翻倍 = "浅档已攒够 12 发"是假的，
       于是升档判据形同虚设，我就可能在毫无功课的情况下往更深的一档打。
    2. **顺序 = 新旧。** 判"这一档最近一发是不是刚把卡打死"看的是列表尾部。历史上写反过一次
       （运行目录追加在台账之后），结果拿一条老毒态否决了当天安全档的正常开火 —— 那种误否决的
       代价是发数堆不起来，而发数正是安全裕量。台账是 agent 当场追加的，它才是"最新"。
    """
    shots = []
    if ledger and os.path.exists(ledger):
        with open(ledger, encoding="utf-8", errors="replace") as f:
            shots = parse_verdicts(f.read())
    led_n = len(shots)
    if runs_glob:
        hist = load_history(runs_glob)
        # 台账非空时，运行目录只补"台账里完全没有的那些年份/坐标"：判决行没有日期可比，
        # 所以宁可少算也不要重数 —— 少算只会让我晚一点升档（保守），重数会让我提前升档（危险）。
        if led_n:
            known = {(s.vd, s.pw, s.delay, s.verdict) for s in shots}
            extra = [s for s in hist if (s.vd, s.pw, s.delay, s.verdict) not in known]
            ignored = len(hist) - len(extra)
            if ignored:
                print(f"   （运行目录里 {ignored} 发与台账坐标重合，按台账为准不重数）")
            # 补来的老发数只能**接在前面**：尾部才是"最近"，判"上一次刚把卡打死"看的就是尾部。
            shots = extra + shots
        else:
            shots = hist
    return shots


def main(argv=None):
    """开火入口的门禁：python -m ga100vfi.envelope --ledger … --vd 366 --pw 198 --delay 117

    退出码 0 = 允许；3 = 不许打（打印为什么）。每批的机器总结总是先打出来 ——
    「每次要总结」不能依赖我记得写。
    """
    import argparse
    ap = argparse.ArgumentParser(prog="python -m ga100vfi.envelope")
    ap.add_argument("--ledger", default="/root/logs/verdicts.tsv")
    ap.add_argument("--runs", default="",
                    help="crack.sh 运行目录的 glob（pw 只在目录名里，判决行不带）："
                         "$GA_BASE/logs/*-crack-*")
    ap.add_argument("--vd", type=int)
    ap.add_argument("--minv", type=int)
    ap.add_argument("--maxv", type=int)
    ap.add_argument("--pw", type=int)
    ap.add_argument("--delay", type=int)
    ap.add_argument("--td", type=int, default=-60)
    ap.add_argument("--pulses", type=int, default=3)
    ap.add_argument("--allow-unranked", action="store_true")
    ap.add_argument("--label", default="")
    ap.add_argument("--delays", default="",
                    help="campaign 的 delays= 列表（逗号分隔）。给了它就**逐个 delay 都审**，"
                         "而不是只看第一个")
    ap.add_argument("--sweep", action="store_true",
                    help="不审固定几何：从健康区里随机生成一片由门禁核准的坐标。"
                         "此时 stdout 只输出 crack.sh 要 eval 的赋值，总结与解释走 stderr")
    ap.add_argument("--ndelays", type=int, default=4, help="--sweep：要几个相位候选")
    ap.add_argument("--seed", type=int, default=None, help="--sweep：随机种子（复现同一批）")
    a = ap.parse_args(argv)

    shots = load_shots(a.ledger, a.runs)

    # 随机扫模式：stdout 只给赋值。总结走 stderr —— 否则 crack.sh eval 的时候会把总结的中文行
    # 当成 shell 代码吃进去，那种错会在开火之前就把脚本崩掉。
    if a.sweep:
        sw = healthy_sweep(shots, pw=a.pw or 222, td=a.td, n_delays=a.ndelays,
                           rng=random.Random(a.seed), allow_unranked=a.allow_unranked)
        print(sw.explain(), file=sys.stderr)
        for d, why in sw.dropped:
            print(f"   丢候选 delay={d}：{why}", file=sys.stderr)
        if not sw.ok:
            print("   [!!] 健康区里没有门禁放行的坐标 → 不起批："
                  "先攒浅侧功课，或给出更宽的健康区并说明理由", file=sys.stderr)
            return 3
        print(sw.as_shell(), end="")
        return 0

    print(summary(shots, a.label))
    if a.vd is None:
        return 0
    # 审的是这次 campaign 里**最深的那一发**：板载 ladder 会在 minv..maxv 里自己扫，只报
    # 起点等于没审（起点浅、往下掉到最深点的那几发才是会打死卡的那几发）。
    deepest = min(v for v in (a.vd, a.minv, a.maxv) if v is not None)
    highest = max(v for v in (a.vd, a.minv, a.maxv) if v is not None)
    # 每个 delay 点都得单独审。以前只审 `delays=` 的第一个点，于是多点批次里后面那些点等于
    # 没有门禁 —— 随机扫一批就是四个点，那正是最容易"其中一个点早打死过人"的形状。
    dl = [int(x) for x in re.split(r"[,\s]+", a.delays) if x] if a.delays else [a.delay]
    rc = 0
    for d in dl:
        v = check(Rungish(deepest, a.pw, d, a.td), shots,
                  allow_unranked=a.allow_unranked, grid=(deepest, highest))
        print(f"== 门禁：vd={a.vd}..{a.minv if a.minv is not None else deepest}"
              f"（最深 {deepest}）pw={a.pw} delay={d} td={a.td} -> "
              f"{'可以打' if v.ok else '不许打'} ==")
        print("   " + v.reason)
        rc = rc or (0 if v.ok else 3)
    return rc


class Rungish:
    """check() 只读四个轴，命令行与测试都用它，不必为一次判断造完整 Rung。"""
    __slots__ = ("vd", "pw", "delay", "td")

    def __init__(self, vd, pw, delay, td):
        self.vd, self.pw, self.delay, self.td = vd, pw, delay, td


if __name__ == "__main__":
    raise SystemExit(main())
