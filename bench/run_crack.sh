#!/bin/bash
# 薄壳：只做一件事 —— 确认没有第二个进程正在碰卡，然后用仓库正式入口开火。
#
# 为什么它必须在仓库里而不是某台机器的 /root：判决机每次重启清空 /tmp，而 /root 里的私人脚本
# 不进版本库 —— 今天判据散在一个私人外壳里，直接后果是同一批几何在两台机器上手抄出两个版本。
#
# 为什么这里**不留任何几何默认值**：波形/搜索几何只能有一个出处（bench/crack.sh + 档案），
# 外壳再留一份默认值，就会出现"我传的 PW=222 被外壳悄悄改成 198"这种查一天的事。
# 要改默认就改 bench/crack.sh，那里有包络门禁与测试兜着。
#
# 用法：
#   BOARD=http://<设备IP> bash bench/run_crack.sh                      # crack.sh 的默认几何
#   BOARD=http://<设备IP> SWEEP=random NDELAYS=4 MAX=8 bash bench/run_crack.sh
#   BOARD=http://<设备IP> DELAYS=64 VD=372 MINV=372 MAXV=372 BATCH=4 MAX=4 bash bench/run_crack.sh
set -uo pipefail
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
PIDF=/run/ga-firingd.pid
[ -n "${BOARD:-}" ] || { echo "[!!] BOARD 未设置（地址取板子串口横幅或 /api/status 的 net.ip）"; exit 2; }

for _ in $(seq 1 12); do
  [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null || break
  echo "   连发器（pid $(cat "$PIDF")）还在跑，等它落完这一发…"
  sleep 5
done
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  echo "[!!] 连发器还活着 → 不开火（两个 BAR0 参与者就是今天死机的形状）"; exit 5
fi
if pgrep -f 'python3 -m ga100vfi_agent|bash bench/crack\.sh|/root/firingd\.sh' 2>/dev/null; then
  echo "[!!] 已有 agent/crack/连发器在跑 → 不开火"; exit 5
fi

# TOT：整批的墙钟上限。它存在的理由不是"怕慢"，而是"卡死时必须有个人在门口等"——
# 没有 timeout 的话，一个卡住的 curl 会把这一批挂在无人看的终端上，而板载 campaign 还在发。
export BOARD
exec timeout "${TOT:-620}" bash "$GA_BASE/bench/crack.sh"
