#!/bin/bash
# 部署前的最小检查：全部脚本过 bash -n，并把 CRLF 显式报出来（部署脚本会 tr -d '\r'，
# 但仓库里带着 CRLF 的话，任何绕过部署直接跑的人都会拿到 "bad interpreter"）。
cd "$(dirname "$0")/.." || exit 1
fail=0
for f in bench/*.sh; do
  if ! bash -n "$f"; then
    echo "SYNTAX FAIL: $f"
    fail=1
  fi
  if grep -q $'\r' "$f"; then
    echo "CRLF: $f"
    fail=1
  fi
done
[ $fail -eq 0 ] && echo "bash -n + CRLF: OK ($(ls bench/*.sh | wc -l) 个脚本)"
exit $fail
