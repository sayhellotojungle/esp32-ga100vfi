#!/bin/bash
# UART0 控制台助手：把命令逐条发给板子并把答复收回来。
#
# 为什么需要它：2f385b0 这一版固件没有 /api/lock，POST /api/cmd 只回
# "queued; check console for result" —— 控制器（VRM）的真实状态**只能从串口看**。
# 没有它，预检就无从判断 pwd，而「锁着开火」= 每发都在 ensure_unlocked() 门前 return，
# PEX_RST 根本不动，判决只是对一张没被碰过的卡回 under。
#
# 用法：bash console.sh z                 # 只读：打印 cml/pwd/e1/vout
#       bash console.sh "@w 25 03" u z    # 先清故障，再交密码，再回读
#       CON_GREP=I2C bash console.sh "@r 25 46 2"   # 换判据（默认认 cml=）
#
# 读法（2026-11-26 一天之内两次把整批 0 发葬在这里，形状都实测过）：
#   * ch341 在重新 stty 之后**第一笔写会被吞掉**：单发一次命令 = 赌运气；
#   * 上一轮没读完的字节留在驱动缓冲里，下一次调用先读到残留，于是答复被切成
#     "007e / =1 e1=0xc2ea vout=0xcml=…" 这种碎片（今天第二次读就是这个形状）；
#   * 固定 4 s 窗口 + 只发一遍：慢一点或早一点就整批判成"串口没有可读答复"，
#     crack.sh 于是 exit 3 —— 一整批 60 发死在一条**可以重试**的读上。
# 所以现在是：先清空残留 → 一个监听窗口内**反复重发**直到看见 CON_GREP → 收工。
# 每轮重发是幂等的（只读命令，或 CLEAR_FAULTS/密码这种重复无害的写）。
# 三轮都不成时把"收到了什么"原样吐出来：0 字节 = 线缆/适配器；非空 = 板子答了别的
# （ERR status read 之类）。下一步完全不同，不能让调用方靠猜。
set -uo pipefail
PORT=${PORT:-/dev/ttyUSB0}
OUT=${OUT:-/tmp/con.gen.log}
CON_TRIES=${CON_TRIES:-2}      # 外层：整轮重来（含重新清残留）——兜底=规程 console_tries
CON_REPEAT=${CON_REPEAT:-3}    # 内层：同一监听窗口内重发几遍——兜底=规程 console_repeat
CON_WAIT=${CON_WAIT:-3}        # 每轮监听总时长——兜底=规程 console_wait_s（批内由 crack.sh 从规程注入）
CON_GREP=${CON_GREP:-cml=}     # 什么算"答对了"
# 外层必须给够时间：三轮 × 每轮 6 s 再加清残留，最坏 ~22 s。调用方用 `timeout 20` 包它会得到
# 退码 124，而那**看起来正像**"板子不答话" —— 2026-11-26 那次"PERST 叫不回卡"就是这么被判掉的，
# 证据其实没成立（脉冲到底有没有打进板子都没看到）。所以超时由这里报给调用方，别自己猜。
echo "console: 预算 $((CON_TRIES * (CON_WAIT + 1) + 3))s（CON_TRIES=$CON_TRIES CON_WAIT=$CON_WAIT CON_REPEAT=$CON_REPEAT CON_GREP=$CON_GREP）" >&2

systemctl stop ModemManager 2>/dev/null || true
fuser -k "$PORT" 2>/dev/null || true
sleep 0.4
stty -F "$PORT" 115200 cs8 -cstopb -parenb raw -echo -hupcl 2>/dev/null || exit 1
# 清残留：上一轮（或开机噪声）留在驱动里的字节必须先读掉，否则本轮先收到半截旧答复。
timeout 0.5 cat "$PORT" > /dev/null 2>&1 || true
printf '\r\n' > "$PORT"          # 预热：吞掉 ch341 重建后的第一笔写
timeout 0.5 cat "$PORT" > /dev/null 2>&1 || true

for try in $(seq 1 "$CON_TRIES"); do
  : > "$OUT"
  ( timeout "$CON_WAIT" cat "$PORT" > "$OUT" 2>/dev/null ) & LISTENER=$!
  sleep 0.3
  for i in $(seq 1 "$CON_REPEAT"); do
    for c in "$@"; do printf '%s\r\n' "$c" > "$PORT"; done
    sleep 1.0
    grep -q "$CON_GREP" "$OUT" 2>/dev/null && break
  done
  wait "$LISTENER" 2>/dev/null
  if grep -q "$CON_GREP" "$OUT" 2>/dev/null; then
    # 板子开机时会刷一堆 "ERR invalid/unknown command" 的残留，过滤掉免得盖住真答复
    tr -d '\r' < "$OUT" | grep -vE '^ERR (invalid or overlong|unknown) command$' \
      | grep -a "$CON_GREP" | tail -6
    exit 0
  fi
  try=$((try + 1)); [ "$try" -le "$CON_TRIES" ] && sleep 0.6
done

bytes=$(wc -c < "$OUT" 2>/dev/null || echo 0)
echo "console: ${CON_TRIES} 轮都没有「$CON_GREP」答复（收到 ${bytes} 字节）"
[ "$bytes" -gt 0 ] && tr -d '\r' < "$OUT" | grep -vE '^ERR (invalid or overlong|unknown) command$' | tail -6
exit 2
