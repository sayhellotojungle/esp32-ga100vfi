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


def make_tar(path):
    files = tracked("host") + tracked("bench")
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


def main():
    if not HOST:
        sys.exit("BENCH_HOST is required (set BENCH_HOST=<判决机IP>). The repo deliberately "
                 "carries no bench address.")
    if not PW:
        sys.exit("BENCH_PW is required: the bench password is not stored in the repo")
    check_versions()
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
for f in {prefix}/bench/*.sh; do
  bash -n "$f" || {{ echo "!! 语法错误 $f"; exit 7; }}
done
echo "   [ok] 全部脚本语法通过"
echo "== 自检：红线 1 的防护必须在场 =="
# 措辞要能同时适合两棵树：0.2.x 用 require_unpoisoned()，2f385b0 树直接在 classify() 里
# raise Poisoned。检查的是「防护存在」而不是「防护叫什么名字」。
grep -q Poisoned {prefix}/host/ga100vfi/classify.py || {{ echo "!! classify 里没有 Poisoned"; exit 7; }}
grep -q 0xBADF0000 {prefix}/host/ga100vfi/classify.py || {{ echo "!! classify 里没有 FUSECTRL poison 判据"; exit 7; }}
# 第三种态（实测 0x00000000）比 poison 更早出现，判据缺了它等于没有：见 §七乙。
grep -q STATE_UNKNOWN {prefix}/host/ga100vfi/classify.py || {{ echo "!! classify 里没有第三种态判据"; exit 7; }}
grep -q 'delay=' {prefix}/host/ga100vfi_agent.py || {{ echo "!! 判决行不带坐标，跨轮台账会瞎"; exit 7; }}
# 开火入口必须检查板上固件版本，否则这一轮日志无法回答"哪版固件打的"。
grep -q 'EXPECT_FW=' {prefix}/bench/crack.sh || {{ echo "!! crack.sh 没有固件版本闸门"; exit 7; }}
echo "   [ok] poison 防护、第三种态、带坐标判决都已部署"
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
