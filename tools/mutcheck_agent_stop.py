"""变异验证：把判决代理刚修的几处防护分别改坏，测试必须 FAIL。

不这么跑一遍就不知道测试是不是真在看着这段逻辑。基线必须先绿，否则"测试咬住了"
这句话毫无意义。

⚠️ 为什么在临时副本里改，而不是在仓库里原地改：上一版就是原地改 + 恢复，进程被杀的
时候 `host/ga100vfi_agent.py` 留在了改坏的状态（`stopping = False`），而它已经部署到判决
机上了 —— 仓库里留坏码，下一次部署就会把坏码带回台架。现在整个流程只读仓库、只写临时
目录，并在结束时用 sha256 断言仓库文件一个字节都没动过。

⚠️ 为什么每条 pytest 都带看门狗与超时：把 stopping 改成 False 之后，run_forever 的测试
失去了唯一退出条件，poll=0 下无限循环并把捕获的 stdout 吃满内存 —— 上一版就是这么把
宿主机器冻住的。变异体让测试挂住 ≠ 测试咬住了，那种情况必须报"要修测试"。
"""
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOST = REPO / "host"
AGENT = "ga100vfi_agent.py"

RUNNER = ("import faulthandler,sys,pytest;"
          "faulthandler.dump_traceback_later(30,exit=True);"
          "sys.exit(pytest.main(sys.argv[1:]))")

MUTATIONS = [
    ("A 停火不先于判决（stopping 恒 False）",
     [(r"stopping = bool\(self\.max_lost\) and streak >= self\.max_lost",
       "stopping = False")]),
    ("B 被自己停掉的战役回 409 时丢掉判决行",
     [(r"            if not stopping:      #.*?\n                self\.pending = None\n                return False",
       "            if True:\n                self.pending = None\n                return False")]),
    ("C CLI 默认阈值回到 3",
     [(r'--max-lost", type=int, default=1', '--max-lost", type=int, default=3')]),
    # 本轮修复的本体：停火跟在判决之后 = 板子收判决的同一次轮询里已经开出下一发。
    # 必须两步：先把判决前的停火块整段摘掉，再把停火插到判决行打印之后。上一版只替换
    # 前半段，把 try/except 撕开成非法语法，收集阶段就报错 —— 那不算验证。
    ("D 停火跟在判决之后（顺序倒回来）",
     [(r"        if stopping:\n            print\(f\"agent: 连续.*?\n            self\.lost_stopped = True\n"
       r"            self\.stop_board\(\)\n", ""),
      (r"        self\.lost_streak = streak\n        return True",
       "        if stopping:\n            self.lost_stopped = True\n            self.stop_board()"
       "\n        self.lost_streak = streak\n        return True")]),
]


def apply_mutations(text, steps):
    """Apply every (pattern, repl) of one mutation; all must hit exactly once."""
    for pattern, repl in steps:
        text, n = re.subn(pattern, repl, text, count=1, flags=re.S)
        if n != 1:
            return None, n
    return text, len(steps)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_suite(workdir: Path):
    """跑副本里的测试。返回 (rc, 摘要)；超时/死循环返回 rc=None，绝不算咬住。"""
    base = [sys.executable, "-c", RUNNER, "tests", "-q", "-p", "no:cacheprovider"]
    try:
        r = subprocess.run(base + ["--collect-only", "-q"], cwd=workdir,
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None, "收集阶段超时"
    if r.returncode != 0:
        return None, "收集阶段就失败：" + (r.stdout + r.stderr)[-400:]
    try:
        r = subprocess.run(base, cwd=workdir, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return None, "跑超时：变异体让测试进入死循环 —— 不算咬住，测试缺退出上界"
    out = r.stdout + r.stderr
    if "Timeout" in out or "dump_get_thread_id" in out or "deadlock" in out.lower():
        return None, "死循环（faulthandler 已倒栈）：不算咬住"
    lines = [l for l in out.splitlines() if "passed" in l or "failed" in l or "error" in out]
    return r.returncode, (lines[-1] if lines else out[-300:])


def main():
    before = sha(HOST / AGENT)
    work = Path(tempfile.mkdtemp(prefix="ga100vfi-mutcheck-",
                                 dir=r"C:\Users\jungle\AppData\Local\Temp\opencode"))
    try:
        host_copy = work / "host"
        shutil.copytree(HOST, host_copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        pristine = (host_copy / AGENT).read_text(encoding="utf-8")

        rc, summary = run_suite(host_copy)
        print(f"基线（副本）: rc={rc}  {summary}")
        if rc != 0:
            print("[!!] 基线不绿，变异验证无意义 —— 先修测试"); return 1

        bad = 0
        for label, steps in MUTATIONS:
            new, n = apply_mutations(pristine, steps)
            if new is None:
                print(f"[!!] 变异 {label} 有锚点没命中（第 {n + 1} 步）—— 代码变了，这个脚本过期了")
                bad += 1
                continue
            # 语法先自检：一个语法都过不了的变异体会死在收集阶段，那什么也没验证
            try:
                compile(new, AGENT, "exec")
            except SyntaxError as exc:
                print(f"[!!] 变异 {label} 自身不合法：{exc}  —— 这条不算验证")
                bad += 1
                continue
            (host_copy / AGENT).write_text(new, encoding="utf-8", newline="\r\n")
            rc, summary = run_suite(host_copy)
            (host_copy / AGENT).write_text(pristine, encoding="utf-8", newline="\r\n")
            if rc is None:
                print(f"{label}: [!!] {summary}")
                bad += 1
            elif rc == 0:
                print(f"{label}: rc=0  {summary}  [!!] 测试没咬住，这条防护等于没测")
                bad += 1
            else:
                print(f"{label}: rc={rc}  {summary}  [ok] 测试咬住了")

        rc, summary = run_suite(host_copy)
        print(f"副本还原后: rc={rc}  {summary}")
        after = sha(HOST / AGENT)
        if after != before:
            print(f"[!!] 仓库文件被改动了：{before[:12]} → {after[:12]}（不该发生）")
            return 1
        print(f"[ok] 仓库 host/{AGENT} 全程未改动（sha256 {after[:12]}…）")
        return 1 if (bad or rc != 0) else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
