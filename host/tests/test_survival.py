"""每批发完必须自动回答"卡活到第几秒、相对谁"。

这条统计 2026-11-26 第一次做出决定性结论（第一次开 BAR0 的时刻往后推 16 s，卡的死亡时刻也跟着
往后推），当时它只是 temp 脚本里的一段 python —— 那种东西的寿命等于那一次对话。测试钉的是：
合成数据下它能算出死亡时刻，而且**同时**给出相对那一发坑与相对我们最后一次读的两个差值。
"""
import datetime as dt
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from ga100vfi import survival


def write(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def rows_around(start: dt.datetime, alive_until: float, step: float = 0.25, span: float = 120):
    """从 start 起每 step 一条；alive_until 之后一律 ff ff ff ff（卡离链的样子）。"""
    out, t, n = [], start, int(span / step)
    for i in range(n):
        t = start + dt.timedelta(seconds=i * step)
        good = (t - start).total_seconds() <= alive_until
        out.append("%s,%s" % (t.strftime("%H:%M:%S.%f"),
                              "de 10 c2 20 " if good else "ff ff ff ff "))
    return out


class Survival(unittest.TestCase):
    def run_dir(self, tmp, executing="06:04:49", t_bar0=20.2, nver=1):
        d = os.path.join(tmp, "20261126-060336-crack-0.5.9-2x222_372")
        os.makedirs(d, exist_ok=True)
        write(os.path.join(d, "campaign.csv"),
              ["%s,waiting,1,64,372,None,0,0,0,0,False" % executing,
               "%s,executing,1,64,372,None,1,0,0,0,False" % executing,
               "%s,stopped,1,64,372,None,1,0,0,0,False" % executing])
        agent = ["agent: board=http://x bdf=0000:01:00.0"]
        for i in range(nver):
            agent.append("session 1 attempt %d delay=64 vd=372 code=60(375mV) t_cfg=0.1s "
                         "t_bar0=%ss FUSECTRL=0xe0040000 DSOV=0x00000001 CRC=0x00000055 -> under"
                         % (i + 1, t_bar0))
        write(os.path.join(d, "agent.log"), agent)
        return d

    def test_同时给出相对坑与相对读的两个差值(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = dt.datetime(2026, 11, 26, 6, 4, 49)
            csv = os.path.join(tmp, "presence7.csv")
            write(csv, rows_around(origin, alive_until=27.0))     # +27 s 之后离链
            run = self.run_dir(tmp, executing="06:04:49", t_bar0=20.2)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(0, survival.main(["--run", run, "--csv", csv]))
            txt = out.getvalue()
            self.assertIn("死亡 - 我们的读", txt, "少了那个真正说明问题的差值")
            self.assertIn("死亡 - 那一发坑", txt)
            self.assertIn("+25", txt)
            self.assertIn("没活过 +30s", txt)
            # 死亡 +27.25 s（表里显示 27.2），最后一次读在 +20.2 s ⇒ 差 7.05（显示 7.1）
            self.assertIn("死亡 - 我们的读 = 7.1 s", txt)
            self.assertIn("死亡 - 那一发坑   = 27.2 s", txt)

    def test_活下来时必须说出来(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = dt.datetime(2026, 11, 26, 6, 4, 49)
            csv = os.path.join(tmp, "presence7.csv")
            write(csv, rows_around(origin, alive_until=119.0))    # 整段在场
            run = self.run_dir(tmp, executing="06:04:49", t_bar0=4.0)
            out = io.StringIO()
            with redirect_stdout(out):
                survival.main(["--run", run, "--csv", csv])
            self.assertIn("**卡活下来**", out.getvalue())

    def test_config_only那批没有读也要能报(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = dt.datetime(2026, 11, 26, 6, 4, 49)
            csv = os.path.join(tmp, "presence7.csv")
            write(csv, rows_around(origin, alive_until=60.0))
            run = self.run_dir(tmp, nver=0)                       # agent.log 里一条判决都没有
            out = io.StringIO()
            with redirect_stdout(out):
                survival.main(["--run", run, "--csv", csv])
            self.assertIn("一发 BAR0 都没开", out.getvalue())
            self.assertIn("**卡活下来**", out.getvalue())

    def test_没样本要说人话而不是假装判据成立(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv = os.path.join(tmp, "presence7.csv")
            write(csv, ["not-a-time,de 10 c2 20 "])
            out = io.StringIO()
            with redirect_stdout(out):
                rc = survival.main(["--run", tmp, "--csv", csv])
            self.assertEqual(3, rc)
            self.assertIn("判据不成立", out.getvalue())


if __name__ == "__main__":
    unittest.main()
