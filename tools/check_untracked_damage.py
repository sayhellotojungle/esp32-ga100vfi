#!/usr/bin/env python3
"""未跟踪文件里的 `s/r$//` 损伤检查（没有 HEAD 可比，只能靠"这个词加回 r 之后在同一个文件里出现过"）。

判据故意保守：只有当行尾 token 后面补一个 `r` 之后**在同一文件里作为完整 token 出现过**才算候选。
这样 `import urllib.erro`（文件别处有 `error`）、`raw: st`（别处有 `str`）会被抓出来，而正常以
`.../logs`、`...run` 结尾的行不会被误报。找到就交给人改，不自动写 —— 未跟踪文件是我今天手写的，
没有基准可回滚，自动改错了就再也找不回来。
"""
import re
import subprocess
import sys

DIRS = ["host", "bench", "tools", "test"]
out = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"] + DIRS,
                     check=True, capture_output=True).stdout.decode().splitlines()
TOKEN = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
hits = 0
for f in out:
    if not (f.endswith(".py") or f.endswith(".sh")):
        continue
    try:
        lines = open(f, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        continue
    everywhere = set()
    for ln in lines:
        everywhere.update(TOKEN.findall(ln))
    for i, ln in enumerate(lines):
        m = TOKEN.search(ln[::-1])          # 行尾 token（反向找第一个完整 token）
        if not m:
            continue
        tok = m.group(0)[::-1]
        if tok.endswith("r"):
            continue
        if tok + "r" in everywhere and len(tok) >= 2:
            print(f"{f}:{i + 1}: 行尾 {tok!r}（同文件别处有 {tok + 'r'!r}）: {ln.strip()[:90]}")
            hits += 1
print(f"-- 候选 {hits} 处；未改动（{len(out)} 个未跟踪文件扫过）")
sys.exit(0)
