"""安全包络与升级策略的行为测试：这几条就是「别反复犯同一个错」的代码化。

每条都对应 2026-11-21 真实付出过代价的一次判断（写在这里，是为了让我下次没机会再靠直觉）。

有一条元规则：包络里**不写具体电压/相位当判据**（配方每张卡都可能不一样），只写"怎么知道一组
参数没在 work"。所以本文件里的相位族、深度、比例都是从判决机台账真实战绩里抄来的形状。
"""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ga100vfi import envelope as env


def shot(vd=340, pw=222, delay=54, verdict="under", t="05:00:00", pulses=3, slip="slip=50/5"):
    """默认坐标 = 本台架唯一有战绩的相位族（pw=222、delay≈45/54）的浅侧。"""
    return env.Shot(t, vd, pw, delay, pulses, verdict, slip)


class Rungish:
    """check() 只读四个轴，测试里懒得造完整 Rung。"""
    def __init__(self, vd, pw, delay, td):
        self.vd, self.pw, self.delay, self.td = vd, pw, delay, td


class Parse(unittest.TestCase):
    def test_a_real_verdicts_line(self):
        s = env.parse_verdicts("05:12:44\tvd=354 pw=198 d=117 p=3 -> under slip=48/5\n")
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0].vd, s[0].pw, s[0].delay, s[0].verdict), (354, 198, 117, "under"))
        self.assertEqual(s[0].slip, "slip=48/5")
        self.assertTrue(s[0].fired)

    def test_junk_lines_do_not_become_shots(self):
        self.assertEqual(env.parse_verdicts("HALT 卡被打死\n\nrandom text\n"), [])

    def test_a_shot_without_a_verdict_is_not_counted_as_fired(self):
        # 04:42:42 那种"无判决(rc=3)"的行：卡被打死了，物理上打过了，但没有任何判据。
        s = env.Shot("04:42:42", 355, 198, 0, 1, "", "")
        self.assertFalse(s.fired)
        self.assertFalse(s.progress)


class PhaseFamily(unittest.TestCase):
    """相位按档归族：本台精度本来就只有几十 µs，装作精细等于谁都冻不掉。"""

    def test_delays_within_one_bucket_are_the_same_family(self):
        self.assertEqual(env.phase_bucket(84), env.phase_bucket(92))     # 今天那簇毒态
        self.assertEqual(env.phase_bucket(45), env.phase_bucket(54))     # 本台两个标定记录

    def test_a_foreign_calibration_is_a_different_family(self):
        self.assertNotEqual(env.phase_bucket(117), env.phase_bucket(45))

    def test_the_calibrated_bucket_is_recorded(self):
        # 零事故的那一相位档必须登记在案：delay≈45/54（本台架 Goldilocks 与昨天那批同档）
        self.assertIn(env.phase_bucket(45), env.CAL_BUCKETS)


class Telemetry(unittest.TestCase):
    """反应只认**卡自己的遥测**，而且只认 DSOV；CRC 读脏是另一回事（判决可靠性）。

    两条都是 2026-11-21 用真台账换来的：
      * 判决词会漏 —— 06:55 那批 `DSOV=1 CRC=0x57` 代理仍判 under，总结于是宣称"CRC 未被打脏"；
      * 但把 `CRC != 0x55` 当反应又太松 —— 99 发里 30 发都脏过（0x56/0x57/0x75，下一发又变回去），
        那是判决会话读得太早，拿它冻结加深会把整场战役钉死。
    """

    @staticmethod
    def s(verdict="under", dsov=1, crc=0x55, **kw):
        a = dict(vd=374, pw=222, delay=64, pulses=2)
        a.update(kw)
        return env.Shot("06:55:00", a["vd"], a["pw"], a["delay"], a["pulses"], verdict,
                        "slip=49/4", dsov=dsov, crc=crc)

    def test_a_crc_excursion_is_a_dirty_read_not_a_reaction(self):
        s = self.s(crc=0x57)
        self.assertTrue(s.dirty_read, "读到脏 CRC 却不认账，判决就还是百分之百可信的假象")
        self.assertFalse(s.progress, "CRC 脏而 DSOV 没动 = 读得太早，不是摸到边界")

    def test_dsov_leaving_1_is_a_reaction(self):
        self.assertTrue(self.s(dsov=0).progress, "DSOV 往 0 走就是往命中走，必须算反应")

    def test_the_healthy_baseline_is_neither(self):
        self.assertFalse(self.s().progress)
        self.assertFalse(self.s().dirty_read)

    def test_unread_telemetry_is_neither_but_a_hard_verdict_is_a_reaction(self):
        """lost/dead 没读到遥测：不能拿"没读到"当"动了"，否则死一堆却以为在进步。"""
        s = self.s(verdict="lost", dsov=None, crc=None)
        self.assertFalse(s.progress)
        self.assertFalse(s.dirty_read)
        self.assertTrue(self.s(verdict="over", dsov=None, crc=None).progress)

    def test_one_reaction_freezes_deepening(self):
        """DSOV 动过就不是"再深一点试试"，而是停下精修。"""
        shots = [shot(vd=368, verdict="under") for _ in range(env.MIN_SHOTS_TO_ESCALATE)]
        self.assertGreater(len(env.allowed_rungs(shots)), 1, "前提：攒够发数本来是可以加深的")
        shots.append(self.s(dsov=0))
        self.assertEqual(env.allowed_rungs(shots), [env.RUNGS[0]],
                         "见过反应还允许加深，下一步就是拿 DSOV 去换一次拔电")

    def test_dirty_reads_alone_do_not_freeze_deepening(self):
        """脏读只打折判决，不许拿走加深资格 —— 否则本台账 30 发脏读会把战役永久钉在最浅档。"""
        shots = [shot(vd=368, verdict="under") for _ in range(env.MIN_SHOTS_TO_ESCALATE)]
        shots.append(self.s(crc=0x57))
        self.assertGreater(len(env.allowed_rungs(shots)), 1)

    def test_the_summary_names_the_number_that_moved(self):
        """总结必须点名是哪个遥测、从几动到几 —— 只说"有反应"等于没说。"""
        txt = env.summary([self.s(dsov=0), self.s(crc=0x57)])
        self.assertIn("DSOV 1->0", txt)
        self.assertIn("判决可靠性：1 发读到的 CRC 不是 55（0x57）", txt)

    def test_the_summary_still_names_the_baseline_when_nothing_moved(self):
        txt = env.summary([self.s(crc=0x55)])
        self.assertIn("无（DSOV 从未离开 1）", txt)
        self.assertIn("零事故：放开使劲打", txt)


class Sweep(unittest.TestCase):
    """健康区随机扫：随机只许发生在健康区内，而且每个候选都得过门禁 —— 两条都要钉住。"""

    def test_a_fresh_ledger_still_gives_a_usable_sweep(self):
        for seed in range(12):
            sw = env.healthy_sweep([], pw=222, td=-40, rng=random.Random(seed))
            self.assertTrue(sw.ok, f"seed={seed} 扫不出几何")
            self.assertLessEqual(env.HEALTHY_BOX[0], sw.lo)
            self.assertLessEqual(sw.hi, env.HEALTHY_BOX[1])
            self.assertTrue(all(d % 4 == 0 for d in sw.delays), "delay 必须落在域里的格点上")

    def test_a_frozen_family_never_comes_back_in_a_sweep(self):
        """被规则 A 冻掉的相位档，不许靠"随机"再绕回来。"""
        shots = [shot(delay=54, verdict="poison"), shot(delay=54, verdict="dead")] + \
                [shot(delay=54, verdict="under") for _ in range(env.MIN_SHOTS_TO_ESCALATE)]
        for seed in range(12):
            sw = env.healthy_sweep(shots, pw=222, td=-40, rng=random.Random(seed))
            for d in sw.delays:
                self.assertNotEqual(env.phase_bucket(d), 40,
                                    f"seed={seed} 把冻掉的相位档又随机回来了")

    def test_a_band_containing_a_coordinate_that_killed_is_not_offered(self):
        """按坐标禁火对随机扫同样成立：64/362 打死过卡，delay=64 就不能配盖住 362 的窗口。"""
        shots = [shot(vd=362, delay=64, verdict="dead") for _ in range(3)]
        for seed in range(20):
            sw = env.healthy_sweep(shots, pw=222, td=-40, rng=random.Random(seed))
            if 64 in sw.delays:
                self.assertFalse(sw.lo <= 362 <= sw.hi,
                                 f"seed={seed} 把打死过卡的坐标又扫回来了：{sw.lo}..{sw.hi}")

    def test_as_shell_is_the_five_assignments_and_nothing_else(self):
        """crack.sh 直接 eval 它：多一个中文字都会把开火前的脚本崩掉。"""
        txt = env.healthy_sweep([], pw=222, td=-40, rng=random.Random(7)).as_shell()
        for line in txt.strip().splitlines():
            self.assertRegex(line, r"^(DELAYS|MINV|MAXV|STEP|VD)=\"?[A-Za-z0-9_\",-]*\"?$",
                             f"eval 不安全的行：{line!r}")

    def test_a_box_too_small_for_the_requested_points_is_refused_not_silently_deepened(self):
        """放不下就说放不下：悄悄把窗口挪深，等于随机器自己偷越健康区。"""
        sw = env.healthy_sweep([], pw=222, td=-40, box=(379, 380), rng=random.Random(1))
        self.assertFalse(sw.ok)
        self.assertTrue(sw.dropped, "拒绝必须带原因")

    # —— 见过反应之后，随机扫就只许在那一档里精修 ——
    #
    # 反应是 111 发才买到的东西（2026-11-21：delay=68 vd=372 → DSOV 1->0 + CRC=0x75）。总结里那句
    # "有反应 → 停手，在这档里单轴精修"如果不由代码执行，下一批 SWEEP=random 照样把候选撒遍
    # 40..140，等于把唯一的信号扔掉再花一发拔电预算去找第二次。
    @staticmethod
    def reacted(delay=68, vd=372):
        return env.Shot("07:12:00", vd, 222, delay, 2, "under", "slip=48/5", dsov=0, crc=0x75)

    def test_a_reaction_pins_every_later_sweep_to_that_phase_bucket(self):
        for seed in range(12):
            sw = env.healthy_sweep([self.reacted()], pw=222, td=-40, rng=random.Random(seed))
            self.assertTrue(sw.ok, f"seed={seed}")
            self.assertEqual(sw.refine, (60,))
            for d in sw.delays:
                self.assertEqual(env.phase_bucket(d), 60,
                                 f"seed={seed}：见过反应还去扫 delay={d}（档 {env.phase_bucket(d)}）")
            self.assertIn("精修", sw.explain())

    def test_a_refine_window_always_covers_the_coordinate_that_reacted(self):
        """同一档里换个不相干深度重抽签不叫精修：窗口必须盖住那发 `DSOV=0` 的 vd。"""
        for seed in range(12):
            sw = env.healthy_sweep([self.reacted(vd=372)], pw=222, td=-40, rng=random.Random(seed))
            self.assertTrue(sw.ok, f"seed={seed}")
            self.assertTrue(sw.lo <= 372 <= sw.hi,
                            f"seed={seed}：精修窗口 {sw.lo}..{sw.hi} 盖不住出反应的 vd=372")

    def test_a_box_that_cannot_cover_the_reacted_coordinate_refuses(self):
        """健康区整体比锚点更深时是拒绝，不是悄悄把窗口挪到没反应过的地方。"""
        for seed in range(6):
            sw = env.healthy_sweep([self.reacted(vd=372)], pw=222, td=-40, box=(374, 380),
                                   rng=random.Random(seed))
            self.assertFalse(sw.ok, f"seed={seed}：{sw.lo}..{sw.hi} 盖不住 vd=372 却照样起批")
            self.assertTrue(any("vd=372" in w for _, w in sw.dropped),
                            "拒绝必须点名它是为了哪个坐标（锚点）拒的")

    def test_a_reaction_does_not_send_the_sweep_wandering_when_that_bucket_is_blocked(self):
        """反应档全被门禁挑掉时是**拒绝开火**，不是悄悄去扫别的族。"""
        shots = [self.reacted()] + [shot(vd=v, delay=d, verdict="dead")
                                    for d in (60, 64, 68, 72, 76)
                                    for v in range(362, 381, 2)]
        for seed in range(8):
            sw = env.healthy_sweep(shots, pw=222, td=-40, rng=random.Random(seed))
            self.assertFalse(sw.ok, f"seed={seed} 越过了反应档：{sw.delays}")
            self.assertEqual(sw.delays, ())


class FamilyFreeze(unittest.TestCase):
    """今天最贵的一条经验：**命中是概率事件，所以判据只用确定性事故（poison/dead）**。

    "发了很多发零反应"不能反推配方错；"这一族会把卡打进毒态"可以。丢链两者都不是。
    """

    def test_the_39_shot_borrowed_phase_family_is_frozen(self):
        """真实战绩：pw=198 delay=117 今天 39 发，DSOV/CRC 从未动过，5 发 poison/dead。"""
        shots = [shot(pw=198, delay=117) for _ in range(30)]
        shots += [shot(pw=198, delay=117, verdict="lost") for _ in range(2)]
        shots += [shot(pw=198, delay=117, vd=v, verdict="poison") for v in (354, 356, 358, 357)]
        shots += [shot(pw=198, delay=117, vd=366, verdict="dead")]
        self.assertIn("这一族", env.family_freeze(shots, 198, 117))
        # 冻结必须能挡住开火，而且 --allow-unranked 也绕不过去（它绕的是"形状没登记"）
        v = env.check(Rungish(vd=374, pw=198, delay=117, td=-60), shots, allow_unranked=True)
        self.assertFalse(v.ok, "被冻结的族靠 allow_unranked 就又能打了")
        self.assertIn("相位", v.reason)

    def test_three_poison_shots_freeze_a_family_without_waiting_for_twelve(self):
        """真实战绩：pw=198 delay=84..96 只打了 3 发，三发全毒 —— 不需要攒到 12 发才承认失败。"""
        shots = [shot(pw=198, delay=d, verdict="poison") for d in (84, 92, 96)]
        self.assertIn("这一族", env.family_freeze(shots, 198, 90))

    def test_the_family_with_37_link_drops_and_zero_fatal_is_still_fireable(self):
        """真实战绩（也是我差点判反的一次）：pw=222 相位档 40 有 56 发、**37 发丢链**，但
        poison+dead=0。丢链与深度无关（vd 339..380 都丢过），所以它不能当冻结判据 —— 否则
        唯一零拔电的一族会被冻掉，而今天八发 poison 那一族反而放行。"""
        shots = [shot(vd=v, pw=222, delay=54) for v in (339, 340, 344, 361, 366, 370, 374)] * 2
        shots += [shot(vd=v, pw=222, delay=54, verdict="lost")
                  for v in (326, 334, 357, 358, 359, 360, 361, 362, 363, 364, 368, 380)] * 2
        st = env.family_stats(shots, 222, 54)
        self.assertGreater(st["lost"], st["n"] // 3, "这条测试要的就是丢链远多于事故这个形状")
        self.assertEqual(st["fatal"], 0)
        self.assertEqual(env.family_freeze(shots, 222, 54), "")
        self.assertTrue(env.check(Rungish(vd=340, pw=222, delay=54, td=-40), shots).ok)

    def test_a_link_drop_heavy_but_fatal_free_family_is_reported_not_frozen(self):
        """总结里必须把"丢链"当链路健康报出来，同时明确它不是事故、不冻结。"""
        shots = [shot(pw=222, delay=54) for _ in range(10)]
        shots += [shot(pw=222, delay=54, verdict="lost") for _ in range(6)]
        text = env.summary(shots, "链路健康")
        self.assertIn("链路健康", text)
        self.assertNotIn("**冻结**", text)

    def test_a_family_that_actually_moved_dsov_is_not_frozen_it_is_refined(self):
        """真 `over`（DSOV=0 且 CRC 坏）是**反应**，说明相位是对的：该精修，不该判它标定失败。"""
        shots = [shot(pw=222, delay=54, verdict="lost") for _ in range(14)]
        shots.append(shot(pw=222, delay=54, verdict="over"))
        self.assertEqual(env.family_freeze(shots, 222, 54), "")

    def test_a_family_with_too_few_shots_is_not_frozen(self):
        """两发事故不构成结论 —— 冻结的代价是换一族从零开始，别拿小样本当证据。"""
        shots = [shot(pw=260, delay=54) for _ in range(8)]
        shots += [shot(pw=260, delay=54, verdict="lost") for _ in range(2)]
        self.assertEqual(env.family_freeze(shots, 260, 54), "")


class FatalVeto(unittest.TestCase):
    """坐标形状本身的一票否决（与战绩无关）。"""

    def test_td_positive_is_never_allowed_even_as_the_last_rung(self):
        # 今天上午我把 td=+10 当成"更浅"放了进去，它其实是当时最危险的一档，一发之后卡死。
        deep = [r for r in env.RUNGS if r.td > 0]
        self.assertTrue(deep, "包络表里应保留 td>0 这一族作为记录")
        lots = [shot() for _ in range(200)]                     # 攒再多发也不许
        for r in deep:
            v = env.check(r, lots)
            self.assertFalse(v.ok, f"td={r.td} 竟然放行了")
            self.assertIn("td>0", v.reason)

    def test_wide_pulse_at_deep_voltage_is_fatal(self):
        # 今天 pw=450@356、pw=400@357 各打死一次，都是拔电才回来
        for vd, pw in ((356, 450), (357, 400), (354, 400)):
            v = env.check(Rungish(vd=vd, pw=pw, delay=54, td=-40), [])
            self.assertFalse(v.ok)
            self.assertIn("pw>=400", v.reason)

    def test_no_fatal_rule_is_a_hardcoded_phase_window(self):
        """经验：毒态簇 84..100 不该被写成"宇宙律"。它对**本台**是真的，但判据必须是战绩 ——
        否则换一张卡、重标一次相位，门禁会拿别人家的标定值挡住唯一能打的区域。"""
        for test, why in env.FATAL:
            self.assertNotIn("delay", why.replace("本次", ""))     # 允许出现在括号里的现场值
        self.assertTrue(env._fatal_reason(Rungish(vd=334, pw=222, delay=92, td=-40)) == "")

    def test_the_same_poison_cluster_is_blocked_by_its_own_record(self):
        """同一条：有战绩时它**必须**被挡住 —— 由 family_freeze 而不是硬编码窗口。"""
        poison = [shot(pw=222, delay=d, verdict="poison") for d in (84, 92, 96)]
        self.assertFalse(env.check(Rungish(vd=334, pw=222, delay=90, td=-40), poison).ok)


class Escalation(unittest.TestCase):
    """「贴着安全那边使劲打，打多了没有反应，才许往危险边缘靠」。"""

    def test_deeper_rungs_are_locked_while_the_shallow_one_is_under_fired(self):
        few = [shot(vd=env.RUNGS[0].vd, pw=env.RUNGS[0].pw) for _ in range(3)]
        self.assertEqual([r.id for r in env.allowed_rungs(few, min_shots=12)], [0])
        for r in env.RUNGS[1:]:
            if r.td > 0:
                continue                       # td>0 一族永远被 FATAL 先否决，理由不同
            v = env.check(r, few, min_shots=12)
            self.assertFalse(v.ok)
            self.assertIn("还不许打", v.reason)

    def test_escalation_opens_only_after_enough_shots_with_no_reaction(self):
        enough = [shot(vd=r.vd, pw=r.pw, delay=r.delay)
                  for r in env.RUNGS[:2] for _ in range(env.MIN_SHOTS_TO_ESCALATE)]
        ids = [r.id for r in env.allowed_rungs(enough)]
        self.assertGreater(len(ids), 2, f"攒够发了却不升档：{ids}")
        self.assertTrue(env.check(env.RUNGS[2], enough).ok)

    def test_any_reaction_freezes_further_depth(self):
        """DSOV/CRC 一旦动过，说明已经摸到边：该停下精修，而不是继续往深里走。"""
        enough = [shot(vd=r.vd, pw=r.pw, delay=r.delay)
                  for r in env.RUNGS[:3] for _ in range(env.MIN_SHOTS_TO_ESCALATE)]
        enough.append(shot(verdict="over"))
        self.assertFalse(env.check(env.RUNGS[3], enough).ok)

    def test_lost_and_dead_are_not_progress(self):
        """把"死了一堆"当成进步，就会一路加深去撞更深的死 —— 今天的 over 语义不是这样。"""
        shots = [shot(vd=r.vd, pw=r.pw, delay=r.delay, verdict="lost")
                 for r in env.RUNGS[:2] for _ in range(3)]
        self.assertTrue(any(not s.progress for s in shots))
        self.assertGreater(len(env.allowed_rungs(shots, min_shots=3)), 1)


class BurnOnce(unittest.TestCase):
    """禁火按坐标：所以测试里的坐标要跟被审那一档的下限对齐（下限从表里取，不抄数字）。"""

    FLOOR = env.RUNGS[0].vd

    def test_a_coordinate_that_just_killed_the_card_is_not_refired(self):
        shots = [shot(vd=self.FLOOR) for _ in range(6)]
        shots += [shot(vd=self.FLOOR, verdict="poison"), shot(vd=self.FLOOR, verdict="dead")]
        v = env.check(env.RUNGS[0], shots)
        self.assertFalse(v.ok)
        self.assertIn("绕开它", v.reason)

    def test_scattered_lost_do_not_lock_the_safest_rung_forever(self):
        """安全档上本来就有零散 lost。按历史总量禁火会把最安全的一档锁死，反而逼我往深处去。"""
        shots = [shot(vd=self.FLOOR) for _ in range(20)]
        for i in (3, 9, 14):
            shots[i] = shot(vd=self.FLOOR, verdict="lost")
        shots[-1] = shot(vd=self.FLOOR)                            # 最近一发是好的
        self.assertTrue(env.check(env.RUNGS[0], shots).ok)

    def test_a_dead_coordinate_locks_that_point_not_the_whole_safest_rung(self):
        """05:25 vd=366/pw=198/d=117 把卡打死到要拔电。锁应该锁这个点，不该把整个安全档锁死：
        整档锁死的唯一后果是逼我去打更宽更深的档，和"贴着安全侧使劲打"正相反。"""
        shots = [shot(vd=340) for _ in range(14)]
        shots.append(shot(vd=336, verdict="dead"))
        # 绕开那个点的浅网格：允许
        ok = env.check(Rungish(vd=340, pw=222, delay=54, td=-40), shots, grid=(340, 348))
        self.assertTrue(ok.ok, f"安全档被一个坐标连坐了：{ok.reason}")
        # 而"起点浅但网格会扫过它"照样不许 —— ladder 会自己走到那儿
        sweep = env.check(Rungish(vd=342, pw=222, delay=54, td=-40), shots, grid=(334, 342))
        self.assertFalse(sweep.ok)
        self.assertIn("打死过卡的坐标", sweep.reason)
        # 正着打那个点更不许
        same = env.check(Rungish(vd=336, pw=222, delay=54, td=-40), shots, grid=(336, 336))
        self.assertFalse(same.ok)

    def test_unregistered_pulse_width_must_be_registered_first(self):
        # pw 是最强致死量，所以"表里没有这个宽度"必须停下来问人，而不是就近取一档。
        v = env.check(Rungish(vd=340, pw=210, delay=54, td=-40), [])
        self.assertFalse(v.ok)
        self.assertIn("不在包络表里", v.reason)
        self.assertTrue(env.check(Rungish(vd=340, pw=210, delay=54, td=-40), [],
                                  allow_unranked=True).ok)

    def test_deeper_than_the_whole_table_is_refused_not_rounded(self):
        # 比最低一档还深 = 没登记过的深度：写清楚理由，别让它"看起来像那一档"。
        v = env.check(Rungish(vd=320, pw=222, delay=54, td=-40), [])
        self.assertFalse(v.ok)
        self.assertIn("还深", v.reason)

    def test_a_shallow_start_does_not_hide_a_deep_minv(self):
        """campaign 从浅处起、往 MINV 掉：审的时候必须取最深那一发，只报起点等于没审。"""
        shallow_start = env.check(Rungish(vd=340, pw=222, delay=54, td=-40), [])
        deep_grid = env.check(Rungish(vd=330, pw=222, delay=54, td=-40), [])   # 同 pw，minv 更深
        self.assertTrue(shallow_start.ok)
        self.assertFalse(deep_grid.ok)

    def test_a_rungs_homework_cannot_be_borrowed_by_another_rung(self):
        """同 pw 的两档（下限 338 / 下限 334）不能共用那 12 发，否则"每档攒够"是空话。"""
        r1, r2 = env.RUNGS[0], env.RUNGS[1]                    # pw 都是 222，下限 338 / 334
        self.assertEqual(r1.pw, r2.pw)
        shots = [shot(vd=r1.vd, pw=r1.pw) for _ in range(12)]
        self.assertEqual(env.shots_for(shots, r1), 12)
        self.assertEqual(env.shots_for(shots, r2), 0)

    def test_escalation_is_one_step_at_a_time(self):
        """攒够才让一格，不是一下放开整张表 —— 这就是"贴着安全侧使劲打，再往外挪"。"""
        few = [shot(vd=env.RUNGS[0].vd, pw=env.RUNGS[0].pw)
               for _ in range(env.MIN_SHOTS_TO_ESCALATE)]
        ids = [r.id for r in env.allowed_rungs(few)]
        self.assertEqual(ids, [0, 1], f"攒够最浅一档就同时放开好几档：{ids}")


class FrontShape(unittest.TestCase):
    """包络表本身必须是"从安全侧往死机侧"单调排的 —— 方向搞反过一次，代价是一次拔电。"""

    def test_rung_ids_are_ordered_and_unique(self):
        self.assertEqual([r.id for r in env.RUNGS], list(range(len(env.RUNGS))))

    def test_no_rung_is_both_deeper_and_wider_than_a_later_one(self):
        for i, a in enumerate(env.RUNGS):
            for b in env.RUNGS[i + 1:]:
                if b.td > 0:
                    continue                       # td>0 一族永远被 FATAL 挡着，不参与排序比较
                self.assertFalse(a.pw > b.pw and a.vd < b.vd,
                                 f"rung{a.id}({a.pw},{a.vd}) 比 rung{b.id}({b.pw},{b.vd}) 既更宽又更深")

    def test_the_safest_rung_sits_in_the_calibrated_phase_family(self):
        """第一档必须是本台有战绩的那个相位族 —— 借来的相位不配当起点（今天 25 发的教训）。"""
        self.assertIn(env.phase_bucket(env.RUNGS[0].delay), env.CAL_BUCKETS)
        self.assertLess(env.RUNGS[0].td, 0)
        self.assertEqual(env.RUNGS[0].pw, min(r.pw for r in env.RUNGS if r.td <= 0))

    def test_the_ladder_never_steps_backwards(self):
        """每一档至少要在一根轴上更危险（vd 越小越深）。曾经把 td=+10 当"更浅"排进去过，方向反了。"""
        live = [r for r in env.RUNGS if r.td <= 0]
        for i, a in enumerate(live):
            for b in live[i + 1:]:
                self.assertFalse(a.pw >= b.pw and a.vd <= b.vd,
                                 f"rung{b.id}({b.pw},{b.vd}) 比 rung{a.id}({a.pw},{a.vd}) 还更安全，"
                                 "阶梯不许往回走")

    def test_same_pulse_width_steps_deeper_one_notch_at_a_time(self):
        """同一 pw 的几档必须一档比一档深（vd 递减）：深度轴的"再深一点"是逐档买的，不是跳的。"""
        same = [r for r in env.RUNGS if r.td <= 0 and r.pw == env.RUNGS[0].pw]
        self.assertGreater(len(same), 1)
        self.assertEqual([r.vd for r in same], sorted((r.vd for r in same), reverse=True))
        self.assertLess(same[-1].vd, same[0].vd)


class Summary(unittest.TestCase):
    def test_empty_ledger_still_summarizes(self):
        text = env.summary([], "开机")
        self.assertIn("0 发", text)
        self.assertIn("下一档", text)

    def test_summary_reports_distribution_and_next_rung(self):
        shots = [shot() for _ in range(5)]
        shots += [shot(vd=336, verdict="poison")]
        text = env.summary(shots, "标定族")
        self.assertIn("under=5", text)
        self.assertIn("poison=1", text)
        self.assertIn("族 pw=222", text)
        self.assertIn("无（DSOV 从未离开 1", text)
        self.assertIn("下一档", text)

    def test_summary_names_the_freeze_instead_of_just_counting_losses(self):
        """总结里最该看见的是"这一族会伤卡"，不是"丢了几发链"。"""
        shots = [shot(pw=198, delay=117) for _ in range(12)]
        shots += [shot(pw=198, delay=117, verdict="lost") for _ in range(6)]
        shots += [shot(pw=198, delay=117, verdict="poison"), shot(pw=198, delay=117, verdict="dead")]
        text = env.summary(shots)
        self.assertIn("**冻结**", text)
        self.assertIn("链路健康", text)


class History(unittest.TestCase):
    """门禁读不到历史 = 每次看到的都是"0 发" = 永远只许最浅一档，或者更糟：形同虚设。"""

    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp()

    def _run(self, name, lines):
        d = Path(self.root) / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "agent.log").write_text("\n".join(lines), encoding="utf-8")
        return d

    def test_pulse_width_comes_from_the_run_directory_name(self):
        # 判决行里根本没有 pw，只有目录名 3x198_366 带它。
        self._run("20261121-050133-crack-0.3.2_3x198_366_d117", [
            "#1  session ladder attempt 1 delay=117 vd=366 读数=ok -> under"])
        s = env.load_history(str(Path(self.root) / "*-crack-*"))
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0].pw, s[0].vd, s[0].delay, s[0].pulses, s[0].verdict),
                         (198, 366, 117, 3, "under"))

    def test_shots_from_history_unlock_the_next_rung_and_a_missing_dir_does_not(self):
        r0 = env.RUNGS[0]
        lines = [f"#1  session ladder attempt {i} delay={r0.delay} vd={r0.vd} x -> under"
                 for i in range(1, env.MIN_SHOTS_TO_ESCALATE + 1)]
        self._run(f"20261121-060000-crack-0.3.2_3x{r0.pw}_{r0.vd}_d{r0.delay}", lines)
        shots = env.load_history(str(Path(self.root) / "*-crack-*"))
        self.assertEqual(len(shots), env.MIN_SHOTS_TO_ESCALATE)
        self.assertTrue(env.check(env.RUNGS[1], shots).ok)          # 攒够了 → 放开下一档
        self.assertFalse(env.check(env.RUNGS[3], shots).ok)         # 但只放开一格

    def test_shallower_than_the_floor_still_counts_as_homework(self):
        """今天那 24 发打在 vd=374（比 floor 浅得多）：浅发必须算最浅一档的功课，否则发数永远是 0。"""
        r0 = env.RUNGS[0]
        shots = [shot(vd=374, pw=r0.pw, delay=r0.delay) for _ in range(9)]
        self.assertEqual(env.shots_for(shots, r0), 9)

    def test_death_at_a_deep_coordinate_does_not_convict_the_shallow_area(self):
        """判据是坐标级的：336 打死过卡 → 禁 336 与扫过它的网格；比它浅的浅区照打。
        （今天真实教训：366 打死卡，而"更深的坐标"活着回来过 —— 死亡不是单调的前沿。）"""
        shots = [shot(vd=v) for v in (374, 370, 357, 340)]
        shots += [shot(vd=340, verdict="poison"), shot(vd=336, verdict="dead")]
        ok = env.check(Rungish(vd=344, pw=222, delay=54, td=-40), shots, grid=(344, 374))
        self.assertTrue(ok.ok, f"浅区被同族深处的死连坐了：{ok.reason}")
        deep = env.check(Rungish(vd=336, pw=222, delay=54, td=-40), shots, grid=(336, 374))
        self.assertFalse(deep.ok, "扫回打死过卡的坐标竟然放行")

    def test_a_shot_the_board_fired_but_nobody_judged_is_still_counted(self):
        """板子打了、判决 0 行 —— 六次硬死里五次是这个形状。它必须在台账里存在，否则刚打死卡的
        那一族显示"零事故"，下一轮门禁照样放它去打第二次。crack.sh 收尾时会补 `dead`/`nojudge`，
        而判决词不列白名单，所以这里两种都要能读回来。"""
        r0 = env.RUNGS[0]
        self._run(f"20261121-080000-crack-0.3.2_3x{r0.pw}_374_d{r0.delay}", [
            "agent: 卡连续 3.1s 不在 PCIe 上 —— 不等判决，立刻停战",
            f"session cleanup attempt 1 delay={r0.delay} vd=374 读数=卡不在总线 -> dead"])
        s = env.load_history(str(Path(self.root) / "*-crack-*"))
        self.assertEqual([(x.vd, x.pw, x.verdict) for x in s], [(374, r0.pw, "dead")])
        st = env.family_stats(s, r0.pw, r0.delay)
        self.assertEqual((st["n"], st["fatal"]), (1, 1), "硬死没被算成事故，门禁就看不见它")

    def test_a_nojudge_shot_is_visible_but_not_an_accident(self):
        """卡还在、只是判决没读回来：算打过（发数要诚实），不算事故（不该因此冻族）。"""
        r0 = env.RUNGS[0]
        self._run(f"20261121-080500-crack-0.3.2_3x{r0.pw}_374_d{r0.delay}",
                  [f"session cleanup attempt {i} delay={r0.delay} vd=374 读数=没读回 -> nojudge"
                   for i in range(1, 6)])
        st = env.family_stats(env.load_history(str(Path(self.root) / "*-crack-*")), r0.pw, r0.delay)
        self.assertEqual((st["n"], st["fatal"]), (5, 0))

    def test_a_hard_death_bans_that_coordinate_for_the_next_batch(self):
        """补出来的 dead 必须立刻生效：下一批的网格若扫回那个坐标，按坐标禁（规则 4）。"""
        r0 = env.RUNGS[0]
        shots = [shot(vd=374, pw=r0.pw, delay=r0.delay) for _ in range(9)]
        shots.append(shot(vd=374, pw=r0.pw, delay=r0.delay, verdict="dead"))
        sweep = env.check(Rungish(vd=378, pw=r0.pw, delay=r0.delay, td=-40), shots, grid=(374, 380))
        self.assertFalse(sweep.ok, "刚打死过卡的坐标下一批又被放行了")
        self.assertIn("打死过卡的坐标", sweep.reason)

    def test_the_swap_advice_is_computed_from_the_ledger_not_hardcoded(self):
        """提示不能写死"零事故的那族是 pw=222" —— 2026-11-21 pw=222 用两次硬死把这个结论作废了。
        写死的提示会过期，而过期的提示比没有提示更危险：它会把我推回去原样重打肇事形状。"""
        shots = [shot(delay=54, verdict="poison"), shot(delay=54, verdict="dead"),
                 shot(delay=54, verdict="under")] + \
                [shot(pw=260, vd=338, verdict="under") for _ in range(4)]
        fr = env.family_freeze(shots, 222, 54)
        self.assertIn("打出过事故的宽度：pw=222", fr)
        self.assertIn("pw=260", fr, "换形状的去处要从台账里挑，不是我随口报一个")
        self.assertNotIn("零事故的那族是 pw=222", fr)

    def test_the_verdict_lines_raw_telemetry_is_read_back_from_the_run_dir(self):
        """代理在判决行里写的 `DSOV=`/`CRC=` 必须进包络。06:55 那批的 `CRC=0x57` 就躺在
        agent.log 里，而包络当时代码只看判决词，于是那次唯一的反应被静默吞掉了。"""
        self._run("20261121-065500-crack-0.3.2_2x222_374_d64", [
            "session 3353287112 attempt 2 delay=64 vd=374 FUSECTRL=0xe0040000 "
            "DSOV=0x00000001 CRC=0x00000057 PRIV=0xffffffff FPF=0x00000000 -> under"])
        s = env.load_history(str(Path(self.root) / "*-crack-*"))
        self.assertEqual([(x.dsov, x.crc, x.verdict) for x in s], [(1, 0x57, "under")])
        self.assertTrue(s[0].dirty_read, "读到了脏 CRC 却不认账")
        self.assertFalse(s[0].progress, "DSOV 没动：脏读不许冒充反应")
        self.assertIn("判决可靠性", env.summary(s))

    def test_a_voided_verdict_is_not_an_accident(self):
        """`判决作废`（桥 retrain 失败，板子拉 PEX_RST 就把卡叫回来了）不是"卡被打死"。
        真实台账里 pw=222 被这种行扣上 5 发事故，门禁于是拒绝了一个零硬死的形状 —— 而它是本卡
        唯一零硬史的族。判据被台架恢复流程污染，比判据太松更糟。"""
        line = ("session ladder attempt 1 delay=54 vd=361 读数=无（桥 retrain 失败，靠板子 "
                "PEX_RST 0.3 s 才回总线 —— 熔丝链已重放，判决作废） relink=pexrst -> dead")
        s = env.load_history(str(Path(self.root) / "*-crack-*"))
        self._run("20261121-090000-crack-0.3.2_3x222_361_d54", [line])
        s = env.load_history(str(Path(self.root) / "*-crack-*"))
        self.assertEqual([x.verdict for x in s], ["void"], "判决作废被当成了硬死")
        st = env.family_stats(s, 222, 54)
        self.assertEqual((st["n"], st["fatal"]), (1, 0))

    def test_the_same_attempt_written_four_times_counts_as_one_shot(self):
        """判决 409 之后代理会重试，同一次事故在 agent.log 里可以出现 4 行。发数翻倍会让
        "浅档已攒够 12 发"变成假话。"""
        line = ("session ladder attempt 7 delay=54 vd=361 读数=ok -> under")
        self._run("20261121-090500-crack-0.3.2_3x222_361_d54", [line] * 4)
        st = env.family_stats(env.load_history(str(Path(self.root) / "*-crack-*")), 222, 54)
        self.assertEqual(st["n"], 1)

    def test_history_dir_does_not_double_count_the_ledger(self):
        """同一发在 verdicts.tsv 和运行目录 agent.log 里各有一份。相加=发数翻倍=升档判据是假的。"""
        r0 = env.RUNGS[0]
        line = f"05:00:00\tvd={r0.vd} pw={r0.pw} d={r0.delay} p=3 -> under slip=50/5"
        self._run(f"20261121-070000-crack-0.3.2_3x{r0.pw}_{r0.vd}_d{r0.delay}",
                  [f"#1  session ladder attempt 1 delay={r0.delay} vd={r0.vd} x -> under"])
        ledger = Path(self.root) / "verdicts.tsv"
        ledger.write_text(line + "\n", encoding="utf-8")
        shots = env.load_shots(str(ledger), str(Path(self.root) / "*-crack-*"))
        self.assertEqual(len(shots), 1, f"同一发被数了两遍：{len(shots)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CodeGrid(unittest.TestCase):
    """坐标的真实单位是 PMBus 码，不是 mV。与 test/test_glitch_plan.c::test_code_grid 钉同一张表。

    VOUT_COMMAND 的 LSB 是 6.25 mV：366/368/370 是同一个码 59，372/374/376/378 是同一个码 60。
    11-21 那次命中的 372→370→368→366 阶梯因此有三发是重复；而 300..380 这段合法带只有 14 个坐标。
    host 与固件各有一份取整实现（envelope.mv_to_code / glitch_plan.c 的 mv_to_code）：两边钉同一张
    表，是防止其中一份被"顺手改成 mV 刻度"的唯一闸门。
    """

    def test_the_ladder_table(self):
        for mv in (366, 368, 370):
            self.assertEqual(env.mv_to_code(mv), 59, f"{mv} 与 366 必须是同一个码")
        for mv in (372, 374, 376, 378):
            self.assertEqual(env.mv_to_code(mv), 60, f"{mv} 与 372 必须是同一个码")
        self.assertEqual(env.mv_to_code(380), 61)
        self.assertEqual(env.mv_to_code(380) - env.mv_to_code(300), 13)   # 全带只有 14 个坐标

    def test_code_to_mv_round_trips(self):
        # 梯子把位置存成 mV 的前提：码→mV→码 必须回到原码（0..160 = 整个合法量程）。
        for code in range(0, 161):
            self.assertEqual(env.mv_to_code(env.code_to_mv(code)), code)

    def test_one_code_is_one_physical_shot(self):
        self.assertEqual(env.mv_to_code(372), env.mv_to_code(375))
        self.assertEqual(env.code_to_mv(60), 375)
