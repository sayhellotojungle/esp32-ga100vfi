"""timeline.py 的回归：两份报告都必须从**真日志形状**里得出可判读的数字。

这个工具存在的理由是：固件免费记了逐笔 `st act/want/got/dur`，而我们引用"A 类时刻不确定"十几次,
却从没按 act 分组统计过一次；40 发一批的坐标重复也是事后才发现的。所以这里钉的不是格式化输出，
而是**统计口径**：分布要分_act_算、坐标要按 PMBus 码算、没盖章的行要自己说明。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ga100vfi import timeline as tl

BOARD_LOG = """
glitch vd=372 td=-40 vr=750 tr=-20 tv=0 fast=1 hold=0 pulses=2 pw=222 gap=150 delay=64 revert=0
st 0 act=10 want=-2100 got=-2092 dur=94us
st 1 act=1 want=-1100 got=-1074 dur=104us
st 2 act=3 want=-100 got=-96 dur=88us
st 3 act=1 want=24 got=118 dur=320us
st 4 act=4 want=0 got=231 dur=2us
st 5 act=1 want=224 got=260 dur=102us
st 6 act=8 want=704 got=1002 dur=410us
vs t=+412us v=753mV
vs t=+712us v=70mV
vs t=+1112us v=594mV
glitch done pwd=1 e1=0xc2fa writes_failed=0 slip=268 reset_slip=231 diag_ok=1
glitch vd=372 td=-40 vr=750 tr=-20 tv=0 fast=1 hold=0 pulses=2 pw=222 gap=150 delay=64 revert=0
st 0 act=4 want=0 got=8 dur=2us
glitch done pwd=1 e1=0xc2fa writes_failed=0 slip=19 reset_slip=8 diag_ok=1
"""

AGENT_LOG = """
agent: board=http://192.168.67.117 bdf=0000:01:00.0
session 7 attempt 1 delay=64 vd=372 code=60(375mV) codes=1 t_cfg=1.4s t_bar0=4.2s stable=yes FUSECTRL=0xe0040000 DSOV=0x00000001 CRC=0x00000055 PRIV=0x0000001e FPF=0xffffffff -> unde
session 7 attempt 2 delay=64 vd=374 t_cfg=1.1s t_bar0=4.0s FUSECTRL=0xe0040000 DSOV=0x00000001 CRC=0x00000057 -> unde
session 7 attempt 3 delay=64 vd=376 code=60 codes=1 t_cfg=4.6s t_bar0=4.9s stable=NO FUSECTRL=0xe0040000 DSOV=0x00000001 CRC=0x00000075 -> unde
session 7 attempt 4 delay=64 vd=366 code=59(369mV) stable=yes t_cfg=1.2s t_bar0=3.9s FUSECTRL=0xe0040000 DSOV=0x00000000 CRC=0x00000055 -> unde
session 7 attempt 5 delay=64 vd=372 code=60 stable=yes t_cfg=1.3s t_bar0=4.1s FUSECTRL=0xe0040000 DSOV=0x00000001 CRC=0x00000055 -> lost
"""


class Timing(unittest.TestCase):
    def setUp(self):
        self.d = tl.parse_board_log(BOARD_LOG)

    def test_per_act_stats_are_separated_and_count_the_worst_writer(self):
        # 1002-704=298 µs 的收尾写与 231 µs 的释放沿是两个不同的问题；只有分开算才看得出来
        # "抖的是哪一步"。（这张表就是"要不要动固件优先级"的唯一依据。）
        steps = self.d["steps"]
        self.assertEqual(steps[8]["slip"], [298])
        self.assertEqual(steps[8]["dur"], [410])
        self.assertEqual(steps[4]["slip"], [231, 8])
        self.assertEqual(steps[1]["dur"], [104, 320, 102])

    def test_reset_slip_distribution_survives_two_shots(self):
        self.assertEqual(sorted(self.d["reset_slips"]), [8, 231])
        self.assertEqual(self.d["shot_headers"], 2)

    def test_rail_samples_are_kept_as_pairs(self):
        self.assertEqual(min(v for _t, v in self.d["rails"]), 70)
        self.assertEqual(len(self.d["rails"]), 3)

    def test_report_names_the_act_it_blames(self):
        text = tl.report_timing(BOARD_LOG)
        self.assertIn("GA_VMIN", text)
        self.assertIn("GA_RST_REL", text)
        self.assertIn("最低 70 mV @ +712µs", text)   # 坑有多深、出现在释放后多久
        self.assertIn("753", text)                     # 前置基线：解码可信度的锚
        self.assertIn("reset_slip", text)

    def test_an_empty_log_says_so_instead_of_looking_clean(self):
        # "没有 st 行" 与 "散布为 0" 是两件事：前者不能拿来证明时间表是稳的。
        self.assertIn("没有", tl.report_timing("nothing here"))


class Reanchor(unittest.TestCase):
    """0.5.9 的重锚：表必须能区分"我们的准点率"与"卡把总线拉了多久"。

    0.5.8 之前这两个数混在同一列 reset_slip 里，于是那份"释放沿抖 200 µs"的结论被读成
    "我们的执行器不行"，而真相是前一笔写阻塞了 212 µs（释放沿只是排在它后面 4 µs）。
    混着的代价不是难看，是**方向错**：我们会去改调度器，而该改的是几何。
    """

    REANCHORED = """
glitch vd=372 td=-40 vr=750 tr=-20 tv=0 fast=1 hold=0 pulses=2 pw=222 gap=150 delay=64 revert=0
st 5 act=1 want=-104 got=-97 dur=212us
st 6 act=4 want=104 got=108 dur=6us
glitch done pwd=1 e1=0xc2fa writes_failed=0 slip=12 reset_slip=4 diag_ok=1 stretch_us=212 reanchor=1
"""

    def test_the_new_fields_are_parsed(self):
        d = tl.parse_board_log(self.REANCHORED)
        self.assertEqual(d["stretch"], [212])
        self.assertEqual(d["reanchor"], [1])
        self.assertEqual(d["reset_slips"], [4])

    def test_an_old_log_is_not_claimed_to_be_reanchored(self):
        # ≤0.5.8 的批次没有这两个字段：表不许顺手替它宣布"重锚开启"，也不许把 reset_slip
        # 当成我们的准点率（那一列当时是相对 t0 的，里面就是卡的拉伸）。
        d = tl.parse_board_log(BOARD_LOG)
        self.assertEqual(d["stretch"], [])
        self.assertEqual(d["reanchor"], [])
        text = tl.report_timing(BOARD_LOG)
        self.assertIn("≤0.5.8", text)
        self.assertNotIn("本批重锚开启", text)

    def test_a_reanchored_batch_reports_our_punctuality_and_the_cards_stretch_separately(self):
        text = tl.report_timing(self.REANCHORED)
        self.assertIn("本批重锚开启", text)
        self.assertIn("212", text)                    # stretch_us 的分布必须出现
        self.assertIn("卡侧", text)                    # 并明确那一份归卡，不归我们
        self.assertNotIn("≤0.5.8", text)

    def test_step_deviation_is_read_against_the_effective_plan_time(self):
        # 重锚之后固件报的 want 已经是"本轮真正该发生的时刻"（这里 want=104 而不是 0），
        # 表里 GA_RST_REL 的偏差就该按它算：4 µs，不是 108 µs。
        d = tl.parse_board_log(self.REANCHORED)
        self.assertEqual(d["steps"][4]["slip"], [4])


class Shots(unittest.TestCase):
    def setUp(self):
        self.rows = tl.parse_agent_log(AGENT_LOG)

    def test_coordinate_census_counts_codes_not_millivolts(self):
        # 372/374/376 是同一个码 60：三发只算一个坐标。这就是"230 发打不出 14 个坐标"的测量法。
        keys = {(r["code"], r["delay"]) for r in self.rows}
        self.assertEqual(keys, {(60, 64), (59, 64)})
        self.assertEqual(self.rows[1]["code"], 60)     # 老固件没写 code= 时代理自己算

    def test_unstamped_and_off_baseline_readings_are_counted_separately(self):
        self.assertEqual([r["stable"] for r in self.rows], [True, None, False, True, True])
        self.assertEqual([r for r in self.rows if r["crc"] not in (0x55, None)],
                         [self.rows[1], self.rows[2]])
        text = tl.report_shots(AGENT_LOG)
        self.assertIn("未盖章 1 发", text)
        self.assertIn("2 个 (码, delay) 坐标", text)
        self.assertIn("[!!]", text)                    # DSOV 离开基线 -> 必须喊出来

    def test_readiness_timings_are_carried(self):
        # t_cfg/t_bar0 记的是"卡答话有多快"与"我们何时第一次敢开 BAR0"。它们**不是**静默时长：
        # 见下面两条测试 —— 表必须自己说清这一点，否则一份幸存者分布就会被拿去调小下限（红线 1）。
        self.assertEqual([r["t_cfg"] for r in self.rows], [1.4, 1.1, 4.6, 1.2, 1.3])
        self.assertIn("config 应答时刻", tl.report_shots(AGENT_LOG))

    def test_the_bar0_column_says_it_measures_our_own_gate_not_the_cards_silence(self):
        # min(t_bar0) 恒等于代理的 FIRST_READ_DELAY：把它读成"卡 4 秒后才活过来"就会得出
        # "下限可以砍到 4 秒以内"这种结论，而那正好是红线 1 禁止的推理方向。
        text = tl.report_shots(AGENT_LOG)
        self.assertIn("第一次开 BAR0", text)
        self.assertIn("FIRST_READ_DELAY", text)

    def test_a_shot_whose_card_never_answered_is_counted_as_such(self):
        """没答话的那些发不在分布里 —— 表必须自己声明这是幸存者分布。

        今天实测：那批 60 发的 `config 应答时刻 p50=0.0s` 漂亮得可以直接建议"把静默下限调到 1 s"，
        而这 60 发恰好都是卡活到判决结束的；同一时刻 presence 采样里躺着 32 次掉链。一份只由活着的
        样本组成的分布，证明的正是需要它证明的反面。
        """
        log = AGENT_LOG.replace(" t_cfg=1.3s t_bar0=4.1s", " ")     # attempt 5：卡再没答话
        rows = tl.parse_agent_log(log)
        self.assertIsNone(rows[4]["t_cfg"])
        text = tl.report_shots(log)
        self.assertIn("1 发没有 config 应答时刻", text)
        self.assertIn("幸存者", text)
        line = next(l for l in text.splitlines() if "没有 config 应答时刻" in l)
        self.assertIn("[!!]", line, "这条声明必须自己带旗子：只在行首喊，别指望读者串行看")

    def test_a_pre_055_log_is_not_accused_of_a_card_that_never_answered(self):
        """整批没有这两列 = 代理太旧，不是卡没答话。缺字段与缺证据不能混成一句。"""
        import re
        old = re.sub(r" t_cfg=[\d.]+s t_bar0=[\d.]+s", " ", AGENT_LOG)
        self.assertNotIn("t_cfg=", old)
        text = tl.report_shots(old)
        self.assertIn("0.5.5 的代理", text)
        self.assertNotIn("幸存者", text, "没有字段的时候喊'卡没答话'会把人支去查一条不存在的故障")

    def test_a_complete_batch_says_so_instead_of_leaving_the_reader_to_guess(self):
        # 反向也要说：全批都有应答时不许悄悄省掉那句声明 —— 否则"没喊"就等于"没问题"，
        # 而这正是幸存者分布最容易被当成干净数据的那一刻。
        text = tl.report_shots(AGENT_LOG)
        self.assertIn("每发都有 config 应答", text)


class Entry(unittest.TestCase):
    def test_missing_logs_exit_2_not_an_empty_report(self):
        text, code = tl.report(Path("/nonexistent/run-dir"), "both")
        self.assertEqual(code, 2)
        self.assertIn("空报告", text)


if __name__ == "__main__":
    unittest.main()
