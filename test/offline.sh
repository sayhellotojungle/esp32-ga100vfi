#!/bin/bash
# 离线回归的一键入口（Windows 那边 PowerShell 会把嵌套引号撕碎，所以固化在这里）。
# CRLF 先清一遍：仓库里被 Windows 编辑器碰过的 .py/.sh 会带 \r，python 能跑但 bash 不能。
set -uo pipefail
cd "$(dirname "$0")/.."
sed -i 's/\r$//' host/tests/*.py host/ga100vfi/*.py bench/*.sh
bash -n bench/*.sh || exit 1
OUT=$(mktemp)
( cd host && timeout 500 python3 -m unittest discover -s tests ) >"$OUT" 2>&1
RC=$?
grep -E '^(Ran |OK|FAILED)' "$OUT" || tail -20 "$OUT"
# 测试自己会往 stdout 打判决样例（early 的三档、timeline 的表），那些不是结论，别当结论读。
[ -s "$OUT" ] && grep -cE '^(FAIL|ERROR):' "$OUT" | sed 's/^/失败用例数: /'
rm -f "$OUT"
exit $RC
