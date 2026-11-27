#!/usr/bin/env python3
"""修复一次被 PowerShell 撕掉引号的 sed 造成的字符级损伤。

事故：`wsl -e bash -lc '... sed -i "s/\\r$//" ...'` 这种写法里，PowerShell 5.1 会把传给原生程序的
**双引号整对吃掉**（同一条命令里 `grep -E "^(OK|FAILED)"` 也变成了裸的 `^(OK|FAILED)`，当场被 bash
当语法错）。于是 sed 收到的脚本不是 `s/\\r$//` 而是 `s/r$//` —— 它把每一行结尾的字面 `r` 删掉了：
`raw: str` 变成 `raw: st`、`import urllib.error` 变成 `import urllib.erro`。症状是 ImportError /
SyntaxError，看起来像"我改坏了逻辑"，实际是少了一个字母。

已跟踪文件按 HEAD 逐行比对能精确找出来（HEAD 行 == 工作区行 + "r" 就是唯一特征，误报需要我恰好
只删过一个行尾的 r）。未跟踪文件没有基准，只列候选给人看。
"""
import re
import subprocess
import sys

ROOT = "."
SCOPE = ["host", "bench", "tools", "test"]
FIX = "--fix" in sys.argv


def git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, check=True, capture_output=True).stdout


tracked = set(git("ls-files", *SCOPE).decode().splitlines())
files = sorted(git("ls-files", "--others", "--exclude-standard", *SCOPE).decode().splitlines())
files = sorted(set(files) | tracked)

fixed, suspect = [], []
for f in files:
    try:
        cur = open(f"{ROOT}/{f}", encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        continue
    if f in tracked:
        head = git("show", f"HEAD:{f}").decode("utf-8", "replace").splitlines()
        headset = set(head)
        changed = False
        for i, line in enumerate(cur):
            if line + "r" in headset:
                suspect.append((f, i + 1, line, line + "r"))
                if FIX:
                    cur[i] = line + "r"
                    changed = True
        if changed:
            open(f"{ROOT}/{f}", "w", encoding="utf-8", newline="\n").write(
                "\n".join(cur) + "\n")
            fixed.append(f)
    else:
        for i, line in enumerate(cur):
            m = re.search(r"([A-Za-z_][A-Za-z_0-9]*)\s*$", line)
            if m and m.group(1) in {"st", "erro", "res", "numbe", "counte", "paramete",
                                    "cente", "answe", "layou", "Registe", "Installe",
                                    "Powere", "cance", "compil", "interprete", "Text",
                                    "Read", "Write", "Hub", "ete", "tog"}:
                suspect.append((f, i + 1, line.rstrip(), line.rstrip() + "r?"))

for f, n, cur, guess in suspect:
    print(f"{f}:{n}: {cur!r}   ->  {guess!r}")
print(f"-- 可疑行 {len(suspect)} 处" + ("，已修复（含未跟踪文件的候选需人工确认）" if FIX else "，未改动"))
print("改动过的文件:", ", ".join(fixed) or "无")
