"""One command: pack host/ + bench/ from this repo and install them on the judging host.

Why this exists: the bench's /tmp is wiped on every reboot, and twice now a campaign
refused to start because a helper script that lived in /tmp had vanished. Anything the
run depends on has to live in the repo and be reinstalled by one reproducible command.

usage (from Windows, needs paramiko):
    set BENCH_HOST=<判决机IP>                    # required: no bench address lives in git
    set BENCH_USER=<判决机用户>                  # defaults to root
    set BENCH_PW=<判决机口令>                    # required, never stored
    set BENCH_PREFIX=<部署目录>                  # optional; defaults to <home>/ga100vfi
    python tools/deploy_bench.py

Host, password and install prefix have no defaults on purpose. A hard-coded bench address
in the repo reads like an instruction, and the next person runs it against a machine that
is not theirs.

The install step self-checks the two guards that are easiest to lose silently: the poison
gate in the verdict chain, and the verdict coordinates in the agent's log lines. A deploy
that "succeeded" without them is worse than one that fails here.
"""
import io
import os
import posixpath
import subprocess
import sys
import tarfile
from pathlib import Path

import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
HOST = os.environ.get("BENCH_HOST")      # no default: see the module docstring
USER = os.environ.get("BENCH_USER", "root")
PW = os.environ.get("BENCH_PW")          # no default: keep the bench password out of git
PREFIX = os.environ.get("BENCH_PREFIX")  # None -> <remote home>/ga100vfi


def check_versions():
    """The four places that name a version must say the same thing.

    board.h drives the banner and /api/status; crack.sh refuses to fire unless the board
    reports that version; pyproject names the host package; firmware/ zips are packaged
    from board.h. A drift here means the firing gate compares against a version nobody
    builds, which is how a log stops being traceable.
    """
    import re

    board = (REPO / "main" / "board.h").read_text(encoding="utf-8")
    m = re.search(r'#define\s+GA_FW_VERSION\s+"([^"]+)"', board)
    if not m:
        sys.exit("main/board.h has no GA_FW_VERSION")
    fw = m.group(1)

    crack = (REPO / "bench" / "crack.sh").read_text(encoding="utf-8")
    m = re.search(r'EXPECT_FW=\$\{EXPECT_FW:-([^}]*)\}', crack)
    if not m:
        sys.exit("bench/crack.sh has no EXPECT_FW gate: the firing entry point does not "
                 "check which firmware is on the board")
    if m.group(1) != fw:
        sys.exit(f"bench/crack.sh expects {m.group(1)}, main/board.h builds {fw}")

    py = (REPO / "host" / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', py, re.M)
    if not m:
        sys.exit("host/pyproject.toml has no version")
    if m.group(1) != fw:
        sys.exit(f"host/pyproject.toml says {m.group(1)}, main/board.h builds {fw}")

    print(f"== 版本一致性 ==\n   [ok] board.h / crack.sh / pyproject 均为 {fw}")
    return fw


def tracked(prefix):
    """Files under a directory that belong in the deploy bundle, without build droppings.

    -z because git quotes non-ASCII path components and the quoted form is not a path.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", prefix],
        cwd=REPO, check=True, capture_output=True).stdout
    out = [f.decode("utf-8") for f in out.split(b"\0") if f]
    return [f for f in out if "__pycache__" not in f and not f.endswith(".pyc")]


# Not under host/ or bench/, but required by README §2.4 on the judging host: capture 路径
# 出的快照没有 fingerprint，`ffcalc verify`（认卡闸门）会直接 FAIL，没有这个工具就补不了身份。
EXTRA_FILES = ["tools/attach_identity.py"]


def make_tar(path):
    files = tracked("host") + tracked("bench") + EXTRA_FILES
    for f in EXTRA_FILES:
        if not (REPO / f).exists():
            sys.exit(f"{f} 不在仓库里：README §2.4 的认卡闸门（ffcalc verify）在判决机上跑不起来")
    if not files:
        sys.exit("nothing to pack: is this a git checkout with host/ and bench/?")
    with tarfile.open(path, "w:gz") as tf:
        for f in sorted(files):
            tf.add(REPO / f, arcname=f)
    print(f"packed {len(files)} files, {Path(path).stat().st_size} bytes -> {path}")
    return files


def mkdirs(self, remote_dir):
    if not remote_dir or remote_dir == "/":
        return
    try:
        self.stat(remote_dir)
        return
    except IOError:
        pass
    parent = posixpath.dirname(remote_dir)
    if parent != remote_dir:
        mkdirs(self, parent)
    try:
        self.mkdir(remote_dir)
    except IOError:
        pass


paramiko.SFTPClient.mkdir_posix = mkdirs


def remote_home(c):
    """Where the deploy goes when BENCH_PREFIX is not set: the login account's home."""
    out = c.exec_command("echo \"$HOME\"")[1].read().decode("utf-8", "replace").strip()
    if not out.startswith("/"):
        sys.exit("cannot resolve the remote home directory; set BENCH_PREFIX explicitly")
    return out


def check_policy():
    """每发的节奏与配方必须有唯一出处，而且那个出处要在部署包裡。

    这一步在打包之前跑，因为它拒绝的是"这一批根本不该打"：判决机上那层入口以前自带
    CARD_SETTLE_S=25 / REV=1 / SWIN=1200（仓库改了不生效、批次悄悄不是同一发），而 crack.sh
    现在直接问 shot_policy 要数 —— 文件不在或值被写死在别处，开火就会拿不到规程（SP 会拒绝开火），
    最坏是拿一份旧默认值跑掉一个电源周期。让它在部署时就炸。
    """
    py = sys.executable
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    # encoding 必须写死：Windows 上 text=True 会按本地代码页（这里 gbk）解 stdout，规程表里的中文
    # 直接把读线程炸掉、r.stdout 变成 None（症状是"部署器 TypeError"，跟规程本身无关）。
    kw = dict(capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    r = subprocess.run([py, "-m", "ga100vfi.shot_policy", "--check-scripts", str(REPO)],
                       cwd=REPO / "host", **kw)
    sys.stdout.write(r.stdout or "")
    if r.returncode:
        sys.stderr.write(r.stderr or "")
        sys.exit("规程自检不过（上面那几行）：把这些写死值改回从 shot_policy 取值，再来部署")
    r = subprocess.run([py, "-m", "ga100vfi.shot_policy", "--get", "card_settle_s"],
                       cwd=REPO / "host", **kw)
    if r.returncode or not r.stdout.strip():
        sys.exit("shot_policy 读不出 card_settle_s（键名或文件坏了）：不开火")
    print(f"   [ok] 规程可读、别处无写死值（card_settle_s={r.stdout.strip()}）")


def main():
    if not HOST:
        sys.exit("BENCH_HOST is required (set BENCH_HOST=<判决机IP>). The repo deliberately "
                 "carries no bench address.")
    if not PW:
        sys.exit("BENCH_PW is required: the bench password is not stored in the repo")
    check_versions()
    check_policy()
    tar = Path(os.environ.get("TEMP", "/tmp")) / "ga100vfi-bench.tar.gz"
    make_tar(tar)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PW, timeout=15,
              allow_agent=False, look_for_keys=False)
    home = remote_home(c)
    prefix = PREFIX or posixpath.join(home, "ga100vfi")
    staging = posixpath.join(home, "deploy")
    print(f"== 部署目标 ==\n   {USER}@{HOST}:{prefix}   （暂存 {staging}）")
    s = c.open_sftp()
    s.mkdir_posix(staging)
    s.put(str(tar), posixpath.join(staging, "ga100vfi-bench.tar.gz"))
    s.close()

    install = rf"""
set -e
mkdir -p {prefix}
# 部署是"覆盖"，不是"同步"：仓库里删掉/改名的文件不会自己从机器上消失（曾留下已改名的
# con.sh 和一个早就没用的 cupcheck.sh，下一个脚本就照着旧名字跑）。先清再解。
rm -rf {prefix}/bench
find {prefix}/host -name __pycache__ -type d -prune -exec rm -rf {{}} + 2>/dev/null || true
tar -xzf {staging}/ga100vfi-bench.tar.gz -C {prefix}
# 从 Windows 过来的文本带 CRLF，bash 会把 \r 当成命令的一部分 —— 落地即规范化
find {prefix}/bench -name '*.sh' -exec sed -i 's/\r$//' {{}} +
find {prefix}/bench -name '*.sh' -exec chmod +x {{}} \;
# 老脚本用过的 /tmp 名字留作转发，避免任何还在用旧路径的习惯落空
for f in console crack window mkledger poison_check card_probe link_retrain; do
  [ -f {prefix}/bench/$f.sh ] && printf '#!/bin/bash\nexec bash {prefix}/bench/%s.sh "$@"\n' "$f" \
    > /tmp/.$f.sh && chmod +x /tmp/.$f.sh
done
echo "== 自检：bash -n 语法 =="
for f in {prefix}/bench/*.sh {prefix}/bench/judge-entry/*.sh; do
  bash -n "$f" || {{ echo "!! 语法错误 $f"; exit 7; }}
done
echo "   [ok] 全部脚本语法通过（含 judge-entry：判决机外壳现在也在仓库里）"
echo "== 自检：每发的节奏与配方只有 shot_policy 一个出处 =="
(cd {prefix}/host && python3 -m ga100vfi.shot_policy --check-scripts {prefix}) \
  || {{ echo "!! 部署副本里有写死值、或入口层自带配方：这一批的数值来源不可信，禁止开火"; exit 7; }}
(cd {prefix}/host && python3 -m ga100vfi.shot_policy --get card_settle_s >/dev/null) \
  || {{ echo "!! crack.sh 要的规程键读不出来"; exit 7; }}
grep -q 'SP()' {prefix}/bench/crack.sh \
  || {{ echo "!! crack.sh 里那个取值函数没了：它会安静地依赖各自的默认值，第 6 个出处就是这么来的"; exit 7; }}
test -f {prefix}/bench/judge-entry/batch.sh \
  || {{ echo "!! 判决机外壳没装上（还在 /root/mig-card2 里那份？）"; exit 7; }}
echo "   [ok] 规程闸门通过"
echo "== 自检：红线 1 的防护必须在场 =="
# 措辞要能同时适合两棵树：0.2.x 用 require_unpoisoned()，2f385b0 树直接在 classify() 里
# raise Poisoned。检查的是「防护存在」而不是「防护叫什么名字」。
grep -q Poisoned {prefix}/host/ga100vfi/classify.py || {{ echo "!! classify 里没有 Poisoned"; exit 7; }}
grep -q 0xBADF0000 {prefix}/host/ga100vfi/classify.py || {{ echo "!! classify 里没有 FUSECTRL poison 判据"; exit 7; }}
# 第三种态（实测 0x00000000）比 poison 更早出现，判据缺了它等于没有：见 docs/UNLOCK_AND_POISON.md §四。
grep -q STATE_UNKNOWN {prefix}/host/ga100vfi/classify.py || {{ echo "!! classify 里没有第三种态判据"; exit 7; }}
grep -q 'delay=' {prefix}/host/ga100vfi_agent.py || {{ echo "!! 判决行不带坐标，跨轮台账会瞎"; exit 7; }}
# BAR0 闸门（bar0.require_mapped）：映射之前必须先用只读证据确认"桥的链路层活着 + device
# id 是健康的 20c2"。今天判决机整机死了四次，每一次都是把 MMIO 打向一块链路已经不存在的
# 卡。05:01 那次尤其说明问题：我升了固件版本却忘了同步版本闸门的其他两处，部署在
# check_versions() 就退出了，而我的开火命令是 PowerShell 链里 deploy 之后的下一条 ——
# 它不会因为上一条失败就停下，于是我用一份**没有闸门的旧 bar0.py** 开了火。
# 所以这条自检的意义是：闸门没部署上去 = 部署失败，而不是"少了一个可有可无的文件"。
grep -q 'def require_mapped' {prefix}/host/ga100vfi/bar0.py || {{ echo "!! bar0.py 没有 require_mapped 闸门 —— 这台机器开 BAR0 之前不会先看链路层/device id，禁止这一版上线"; exit 7; }}
grep -q 'Bar0Unavailable' {prefix}/host/ga100vfi_agent.py || {{ echo "!! agent 不认 Bar0Unavailable：闸门一拦就变异常，判决落不了盘"; exit 7; }}
# 开火入口必须检查板上固件版本，否则这一轮日志无法回答"哪版固件打的"。
grep -q 'EXPECT_FW=' {prefix}/bench/crack.sh || {{ echo "!! crack.sh 没有固件版本闸门"; exit 7; }}
# README §2.4 的认卡闸门：capture 快照没有 fingerprint，ffcalc verify 会 FAIL。缺这个工具，
# 现场只剩两种坏选择——跳过 verify，或手搓一个哈希（输入集不同则与规范不可比）。
test -f {prefix}/tools/attach_identity.py || {{ echo "!! 缺 tools/attach_identity.py：verify 闸门补不了身份"; exit 7; }}
grep -q ingest.fingerprint {prefix}/tools/attach_identity.py || {{ echo "!! attach_identity 没调用 ffcalc 自己的指纹算法"; exit 7; }}
grep -q 'len(rows) != 512' {prefix}/tools/attach_identity.py || {{ echo "!! attach_identity 不检查 512 行完整性"; exit 7; }}
echo "   [ok] poison 防护、第三种态、带坐标判决、认卡工具都已部署"
echo "== 自检：四态判据在机器上真跑一遍 =="
cd {prefix}/host && python3 -c "
from ga100vfi import classify as c
want = {{c.FUSE_IDLE: 'idle', 0xBADF510E: 'poisoned',
        0xFFFFFFFF: 'unreachable', 0x00000000: 'unknown', 0xE00C0000: 'unknown'}}
for ctrl, state in want.items():
    got = c.fusectrl_state(ctrl)
    assert got == state, (hex(ctrl), got, state)
class Bar:
    def __init__(self, v): self.v = v
    def rd32(self, off): return self.v
    def wr32(self, off, value): raise AssertionError('不该发生写')
for ctrl in (0xBADF510E, 0x00000000):
    try:
        c.classify(Bar(ctrl))
    except c.FuseControllerStop:
        continue
    raise SystemExit('!! classify 没有拦下 ' + hex(ctrl))
print('   [ok] idle/poisoned/unreachable/unknown 全部正确，两种危险态都拒绝写 DSOV')
"
echo "== 可用脚本 =="
ls {prefix}/bench
"""
    stdin, stdout, stderr = c.exec_command(install, timeout=300)
    stdin.close()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    c.close()
    print(out, end="")
    if err:
        print("[stderr]", err)
    print(f"[exit {rc}]")
    return rc


if __name__ == "__main__":
    sys.exit(main())
