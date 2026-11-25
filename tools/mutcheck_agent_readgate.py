"""变异验证：把判决代理刚加的"读闸门"分别改坏，测试必须 FAIL 且失败的必须是那条对应的测试。

对应 2026-11-20 的两处修复：
  1. 一发 lost 曾连开 15 次 BAR0 会话（waiting 段实测 28 s ≈ 15×2 s），违反红线 1；现在卡不在
     总线上一次都不许开，且每发最多 max_bar0_sessions 次。
  2. 判决行的寄存器曾跨发继承（lost 那一行印着上一发的 FUSECTRL/DSOV/CRC）；现在按 token 归零。

⚠️ 沿用 tools/mutcheck_agent_stop.py 的两条纪律：
  只在临时副本里改，结束时用 sha256 断言仓库文件一个字节都没动（上一版原地改 + 恢复，进程被
  杀掉时把坏码留在了工作树里，而它已经部署到判决机上了）；
  每条 pytest 带看门狗与超时 —— 变异体让测试挂住不等于测试咬住了，那种情况必须报"要修测试"。
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

# (标签, [(正则, 替换)…], 期望失败的测试名)
MUTATIONS = [
    ("A presence 闸门与次数上限一起拿掉（卡不在也照样开 BAR0）",
     [(r"            if sessions < self\.max_bar0_sessions and self\.present\(\):",
       "            if True:")],
     ["test_no_bar0_session_while_the_card_is_off_the_bus",
      "test_bar0_sessions_are_capped_per_shot"]),
    ("B 打完之后的静默被去掉（立刻读，卡还在跑 ROM）",
     [(r"        if self\.first_read_delay > 0:\n            time\.sleep\(self\.first_read_delay\).*?\n",
       "")],
     ["test_first_read_delay_lets_the_card_boot_before_we_read"]),
    ("C 默认值回到不安全（0 秒静默 + 15 次重读）",
     [(r"first_read_delay: float = 2\.0, max_bar0_sessions: int = 2",
       "first_read_delay: float = 0.0, max_bar0_sessions: int = 15")],
     ["test_read_gating_defaults_are_the_safe_ones"]),
    ("D 读数不按发归零（跨发继承，就是现场那个逐字节相同的 lost 行）",
     [(r"        if self\._regs_for != token:", "        if False:")],
     ["test_lost_line_never_inherits_the_previous_shots_registers"]),
    ("E regs_line 退回'照打不误'（读不到也印上一发的数）",
     [(r'        if not self\.last_regs:\n            return f"读数=无[^\n]*\n', "")],
     ["test_lost_line_never_inherits_the_previous_shots_registers"]),
]


def apply_mutations(text, steps):
    """一条变异里的所有 (pattern, repl) 必须各自命中一次，否则这条变异不算验证。"""
    for pattern, repl in steps:
        text, n = re.subn(pattern, repl, text, count=1, flags=re.S)
        if n != 1:
            return None, n
    return text, len(steps)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_suite(workdir: Path):
    """跑副本里的测试。返回 (rc, 摘要, 失败测试名集合)；超时/死循环 rc=None，绝不算咬住。"""
    base = [sys.executable, "-c", RUNNER, "tests", "-q", "-p", "no:cacheprovider"]
    # encoding 必须显式给：Windows 上默认按 cp936 解码，而测试输出里有中文判决行 —— 解码一崩，
    # 读取线程死掉，communicate() 就把 stdout 交回 None，看起来像"子进程什么都没写"。
    cap = dict(capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        r = subprocess.run(base + ["--collect-only", "-q"], cwd=workdir, timeout=120, **cap)
    except subprocess.TimeoutExpired:
        return None, "收集阶段超时", set()
    if r.returncode != 0:
        return None, "收集阶段就失败：" + ((r.stdout or "") + (r.stderr or ""))[-400:], set()
    try:
        r = subprocess.run(base, cwd=workdir, timeout=180, **cap)
    except subprocess.TimeoutExpired:
        return None, "跑超时：变异体让测试进入死循环 —— 不算咬住，测试缺退出上界", set()
    out = (r.stdout or "") + (r.stderr or "")
    # stdout 空 = 子进程被看门狗强杀的迹象之一，按"没有证据"处理，绝不算测试咬住。
    if not out.strip():
        return None, f"子进程一个字都没输出（rc={r.returncode}），疑似被强杀 —— 不算咬住", set()
    if "Timeout" in out or "deadlock" in out.lower():
        return None, "死循环（faulthandler 已倒栈）：不算咬住", set()
    failed = {l.split("::")[-1].split()[0] for l in out.splitlines() if l.startswith("FAILED")}
    lines = [l for l in out.splitlines() if "passed" in l or "failed" in l]
    return r.returncode, (lines[-1] if lines else out[-300:]), failed


def main():
    before = sha(HOST / AGENT)
    work = Path(tempfile.mkdtemp(prefix="ga100vfi-mutcheck-read-"))   # 系统临时目录：不写死任何一台机器的路径
    try:
        host_copy = work / "host"
        shutil.copytree(HOST, host_copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        pristine = (host_copy / AGENT).read_text(encoding="utf-8")

        rc, summary, _ = run_suite(host_copy)
        print(f"基线（副本）: rc={rc}  {summary}")
        if rc != 0:
            print("[!!] 基线不绿，变异验证无意义 —— 先修测试"); return 1

        bad = 0
        for label, steps, expected in MUTATIONS:
            new, n = apply_mutations(pristine, steps)
            if new is None:
                print(f"[!!] 变异 {label} 有锚点没命中（第 {n + 1} 步）—— 代码变了，这个脚本过期了")
                bad += 1
                continue
            try:
                compile(new, AGENT, "exec")
            except SyntaxError as exc:
                print(f"[!!] 变异 {label} 自身不合法：{exc} —— 这条不算验证")
                bad += 1
                continue
            (host_copy / AGENT).write_text(new, encoding="utf-8", newline="\r\n")
            rc, summary, failed = run_suite(host_copy)
            (host_copy / AGENT).write_text(pristine, encoding="utf-8", newline="\r\n")
            if rc is None:
                print(f"{label}: [!!] {summary}")
                bad += 1
                continue
            if rc == 0:
                print(f"{label}: rc=0  {summary}  [!!] 测试没咬住，这条防护等于没测")
                bad += 1
                continue
            missing = [t for t in expected if t not in failed]
            if missing:
                print(f"{label}: rc={rc}  {summary}  [!!] 该失败的没失败：{missing}")
                bad += 1
            else:
                print(f"{label}: rc={rc}  {summary}  [ok] 对应测试咬住了")

        rc, summary, _ = run_suite(host_copy)
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
