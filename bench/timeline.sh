#!/usr/bin/env bash
# 一批日志的两次归因：
#   timing —— 按 act 分组的到达偏差 (got-want) 与总线阻塞 (dur)，加 reset_slip 分布；
#   shots  —— 按 **PMBus 码**清点这一批真正覆盖了几个物理坐标、脏读（stable=NO）比例、
#             以及 config 应答 / 第一次开 BAR0 的时刻分布。
#
# 为什么单独有个入口：这两张表第一次被真正需要（11-26 复盘 11-25 那 40 发）时，答案是"从没算过",
# 而数据一直躺在 ~/ga100vfi/logs/<批>/board.log 与 agent.log 里 —— 固件从 0.3.x 就在免费记
# `st act/want/got/dur`。从此的规矩：**引用"A 类时刻不确定"或"我们扫过这一带"之前，先给出这两张表。**
# crack.sh 每批结束会自动跑一次；自动版之前打过的批次用这个补。
#
# 用法（判决机上）：
#   bash bench/timeline.sh                                   # 最近一批
#   bash bench/timeline.sh ~/ga100vfi/logs/20261125-082842-crack-0.5.4-2x222_372
#   bash bench/timeline.sh <dir> --only shots
set -uo pipefail
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
HOST=$GA_BASE/host
args=("$@")
if [ ${#args[@]} -eq 0 ] || [[ "${args[0]}" == -* ]]; then
  dir=$(ls -1dt "$GA_BASE"/logs/*-crack-* 2>/dev/null | head -1)
  [ -n "$dir" ] || { echo "[!!] $GA_BASE/logs 下没有 *-crack-* 运行目录" >&2; exit 2; }
  echo "   （未指定批次，取最近一批：$dir）"
  args=("$dir" "${args[@]}")
fi
[ -d "$HOST" ] || { echo "[!!] 找不到 host 部署目录 $HOST（tools/deploy_bench.py 部署过吗）" >&2; exit 2; }
(cd "$HOST" && exec python3 -m ga100vfi.timeline "${args[@]}")
