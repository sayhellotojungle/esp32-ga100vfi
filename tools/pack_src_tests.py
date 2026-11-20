#!/usr/bin/env python3
"""Pack the offline regression inputs (main/, test/, host/, tools/, arduino/) and run them.

The Windows workstation has no C compiler, so `python test/run_tests.py` cannot run there.
The judging host is Ubuntu with gcc, so the C suites run there against the same tree:

    set BENCH_HOST=<判决机IP> BENCH_USER=<判决机用户> BENCH_PW=<判决机口令>
    python tools/pack_src_tests.py --run

The exit code is the suite's, so this works as a release gate.
"""
import argparse
import posixpath
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = ["main", "test", "host", "tools", "partitions.csv"]
# The sketch copies are generated (gitignored) but run_tests.py verifies them with
# sync_arduino.py --check, so they have to travel with the tree.
EXTRA = ["arduino"]
OUT = Path(tempfile.gettempdir()) / "ga100vfi-src-tests.tar.gz"


def files():
    # -z keeps non-ASCII paths usable: git quotes them otherwise (this repo tracks a
    # Chinese-named markdown file), and the quoted form is not a path git accepts back.
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"] + INCLUDE,
                         cwd=ROOT, check=True, capture_output=True).stdout
    names = [p.decode("utf-8") for p in out.split(b"\0") if p]
    names = [f for f in names if "__pycache__" not in f and not f.endswith((".pyc", ".o"))]
    # The sketch copies are gitignored but run_tests.py verifies them with
    # sync_arduino.py --check, so walk them in explicitly.
    for d in EXTRA:
        base = ROOT / d
        for p in sorted(base.rglob("*")) if base.is_dir() else []:
            if p.is_file() and p.suffix in (".c", ".h", ".ino", ".csv"):
                rel = p.relative_to(ROOT).as_posix()
                if rel not in names:
                    names.append(rel)
    return names


def bench_paths(c, env_prefix=None):
    """Install/staging locations on the bench, derived from the login account's home.

    Nothing here has a hard-coded absolute default: the repo must not read like "the bench
    lives at this address", and a non-root account would otherwise be pointed at /root.
    """
    home = c.exec_command("echo \"$HOME\"")[1].read().decode("utf-8", "replace").strip()
    if not home.startswith("/"):
        sys.exit("cannot resolve the remote home directory; run the suite manually")
    prefix = env_prefix or posixpath.join(home, "ga100vfi")
    return posixpath.join(home, "deploy"), posixpath.join(prefix, "src-tests")


def run_remote(tar):
    """Upload, extract into a clean directory, and run the suite on the judging host.

    This exists because the C suites need a compiler and the workstation does not have
    one; the judging host does. The exit code is the suite's, so it works as a release gate.
    """
    import os
    try:
        import paramiko
    except ImportError:
        sys.exit("--run needs paramiko: python -m pip install paramiko")
    host = os.environ.get("BENCH_HOST")
    user = os.environ.get("BENCH_USER", "root")
    pw = os.environ.get("BENCH_PW")
    if not host:
        sys.exit("BENCH_HOST is required for --run (set BENCH_HOST=<判决机IP>); the repo "
                 "deliberately carries no bench address")
    if not pw:
        sys.exit("BENCH_PW is required for --run (the bench password is not stored in git)")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=user, password=pw, timeout=15, allow_agent=False,
              look_for_keys=False)
    staging, install = bench_paths(c, os.environ.get("BENCH_PREFIX"))
    c.exec_command(f"mkdir -p {staging}")[1].channel.recv_exit_status()
    s = c.open_sftp()
    try:
        s.put(str(tar), staging + "/" + tar.name)
    finally:
        s.close()
    cmd = (f"rm -rf {install} && mkdir -p {install} && "
           f"tar -xzf {staging}/{tar.name} -C {install} 2>/dev/null; "
           f"cd {install} && python3 test/run_tests.py --sanitizers")
    _, out, err = c.exec_command(cmd, timeout=900)
    for line in out:
        print(line.rstrip())
    for line in err:
        print("[stderr] " + line.rstrip())
    rc = out.channel.recv_exit_status()
    c.close()
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", action="store_true",
                    help="upload to the judging host (BENCH_HOST/BENCH_USER/BENCH_PW) and run there")
    args = ap.parse_args()
    names = files()
    if not names:
        sys.exit("nothing to pack")
    with tarfile.open(OUT, "w:gz") as tf:
        for f in names:
            tf.add(ROOT / f, arcname=f)
    print(f"{OUT.name}: {len(names)} files, {OUT.stat().st_size} bytes")
    if args.run:
        return run_remote(OUT)
    print(f"未上传（缺 --run 或缺 BENCH_HOST/BENCH_PW）。手工放置：把 {OUT.name} 传到判决机的 "
          f"$HOME/deploy/，解到 $HOME/ga100vfi/src-tests，然后在该目录执行 "
          f"python3 test/run_tests.py --sanitizers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
