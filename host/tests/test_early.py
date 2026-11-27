"""early 档：把"这一发没反应"拆成"什么都没打到"与"打早了"。

判据只有 row 18 翻没翻，而且必须与**本机自己的批前基线**比：RIR 内容随 SKU 与个体不同，
拿别人的 row 18 当参照等于抄别人的配方。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ga100vfi import early


def _cap(rows: dict) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump({"rows": {str(k): f"0x{v:08x}" for k, v in rows.items()}}, f)
    f.close()
    return f.name


BASE = {18: 0x12345678, 19: 0x00000000, 312: 0xA5A50000, 313: 0xC3C30000}


class Tiers(unittest.TestCase):
    def _report(self, pre, post, watch=18):
        return early.report(_cap(pre), _cap(post), watch)

    def test_identical_captures_are_nothing(self):
        text = self._report(BASE, dict(BASE))
        self.assertIn("early 档判读：nothing", text)
        self.assertIn("4 行逐字节同基线", text)

    def test_only_the_sense_row_flipping_is_early_and_points_later(self):
        post = {**BASE, 18: 0x12345679}
        text = self._report(BASE, post)
        self.assertIn("early 档判读：early", text)
        self.assertIn("相位偏早", text)
        self.assertIn("φ 往晚", text)
        self.assertIn("0x12345678->0x12345679", text)

    def test_a_dirtied_control_row_alongside_is_partial_not_early(self):
        post = {**BASE, 18: 0x12345679, 312: 0xA5A50001}
        text = self._report(BASE, post)
        self.assertIn("early 档判读：partial", text)
        self.assertIn("就地精修", text)

    def test_other_rows_moving_without_the_sense_row_is_not_nothing(self):
        # 这条是"为什么值得读 row 18"的反面证据：扰动发生了，但相位刻度没动 -> 不许读成 nothing
        post = {**BASE, 313: 0xC3C30002}
        text = self._report(BASE, post)
        self.assertIn("partial", text)
        self.assertIn("有扰动", text)
        self.assertNotIn("early 档判读：nothing", text)

    def test_a_missing_capture_is_nojudge_not_nothing(self):
        """缺凭证与"确认没打到"不是一件事：写成 nothing 就会把加深当成合理下一步。"""
        for args in ((BASE, {}), ({}, BASE)):
            text = early.report(*[a if isinstance(a, str) else _cap(a) for a in args])
            self.assertIn("nojudge", text)
            self.assertNotIn("逐字节同基线", text)

    def test_rows_read_on_one_side_only_are_not_counted_as_changed(self):
        # 批末少读了几行（卡中途掉链）时不许报"变了"，那是"没读到"
        post = {k: v for k, v in BASE.items() if k != 19}
        text = self._report(BASE, post)
        self.assertIn("nothing", text)

    def test_watch_row_is_configurable(self):
        # 换卡/换代时行号要能改，但改的人必须先抓自己的基线（docstring 里那条）
        post = {**BASE, 19: 0x00000001}
        text = self._report(BASE, post, watch=19)
        self.assertIn("early 档判读：early", text)

    def test_exit_code_demands_action(self):
        # 退码就是"要不要改动作"的信号：nothing 才许 0，其余（early/partial/nojudge）都要人动
        same = _cap(BASE)
        early_post = _cap({**BASE, 18: 0xFFFFFFFF})
        self.assertEqual(early.main([same, _cap(BASE)]), 0)
        self.assertEqual(early.main([same, early_post]), 3)
        self.assertEqual(early.main([same, early_post, "--watch", "19"]), 3)


class Contract(unittest.TestCase):
    def test_the_shape_burn_py_capture_actually_writes(self):
        """`burn.py capture -o x.json` 写的是 `{"ff_rows": {...}}`（十进制行号字符串 + 整数）。

        认错形状的后果不是报错，是**每批都判成 nojudge**，而那句话听起来像"卡不配合"，实际是
        我们的解析器不对 —— 所以这条按真形状钉住，不按我希望的形状。
        """
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"ff_rows": {"18": 305419896, "19": 0, "312": 2779013120},
                   "captured_at": "2026-11-26T04:20:00Z"}, f)
        f.close()
        loaded = early.load(f.name)
        self.assertEqual(loaded[18], 305419896)
        self.assertEqual(loaded[312], 2779013120)
        self.assertEqual(early.verdict(loaded, loaded)[0], "nothing")

    def test_an_unknown_shape_is_nojudge_rather_than_everything_changed(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"rows_v2": {"18": 1}}, f)       # 将来换了键名：要判"没凭证"，不是"全变了"
        f.close()
        self.assertEqual(early.load(f.name), {})

    def test_module_ships_the_row_the_docs_name(self):
        self.assertEqual(early.EARLY_ROW, 18)

    def test_a_real_baseline_parses(self):
        # 用真的那份烧写前基线做一次冒烟测试：真文件的形状（十进制键 / 整数值）必须能读
        p = Path(__file__).resolve().parents[2] / "cards"
        cards = list(Path(__file__).resolve().parents[3].glob("github/ffcalc/cards/*.json"))
        if not cards:
            self.skipTest("ffcalc 不在旁边")
        for c in cards:
            data = json.loads(c.read_text(encoding="utf-8"))
            rows = data.get("ff_rows") or data.get("rows")
            if not rows:
                continue
            f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
            json.dump({"rows": rows}, f)
            f.close()
            loaded = early.load(f.name)
            self.assertTrue(loaded, c.name)
            self.assertIn(18, loaded, f"{c.name} 没有 row 18，early 档对这张卡无凭证")
            return
        self.skipTest("cards/*.json 里没有 ff_rows")


if __name__ == "__main__":
    unittest.main()
