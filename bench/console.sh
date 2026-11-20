#!/bin/bash
# UART0 控制台助手：把命令逐条发给板子并把答复收回来。
#
# 为什么需要它：2f385b0 这一版固件没有 /api/lock，POST /api/cmd 只回
# "queued; check console for result" —— 控制器（VRM）的真实状态**只能从串口看**。
# 没有它，预检就无从判断 pwd，而「锁着开火」= 每发都在 ensure_unlocked() 门前 return，
# PEX_RST 根本不动，判决只是对一张没被碰过的卡回 under。
#
# 用法：bash console.sh z            # 只读：打印 cml/pwd/e1/vout
#       bash console.sh "@w 25 03" u z   # 先清故障，再交密码，再回读
set -uo pipefail
PORT=${PORT:-/dev/ttyUSB0}
OUT=${OUT:-/tmp/con.gen.log}

# ModemManager 会把 USB 串口当调制解调器"探测"，探测期间写入会被吞掉。
systemctl stop ModemManager 2>/dev/null || true
fuser -k "$PORT" 2>/dev/null || true
sleep 0.5
stty -F "$PORT" 115200 cs8 -cstopb -parenb raw -echo -hupcl 2>/dev/null || exit 1

( timeout "$((3 + 3 * $#))" cat "$PORT" > "$OUT" 2>/dev/null ) & LISTENER=$!
sleep 0.4
for c in "$@"; do printf '%s\r\n' "$c" > "$PORT"; sleep 1.2; done
wait "$LISTENER" 2>/dev/null
# 板子开机时会刷一堆 "ERR invalid/unknown command" 的残留，过滤掉免得盖住真答复
tr -d '\r' < "$OUT" | grep -vE '^ERR (invalid or overlong|unknown) command$' | tail -30
