set -uo pipefail
# 判决机上的"手动一批"外壳 —— **仓库里的唯一出处**。
#
# 为什么它现在在版本库里（以前它在判决机的 /root/mig-card2 下，不在版本库）：
#   * 改一个默认值要靠跨机器 sed —— 那次 sed 的引号被 PowerShell 吃掉，把七个文件的行尾字母删了；
#   * 它自己写着 `REV=${REV:-1}` `SWIN=${SWIN:-1200}`，与冻结配方**相反**，调用方忘 export 的那一批
#     就不是 11-21 那一发了（那几批恰好都显式传了 0，所以没被咬 —— 靠人记的规矩不算规矩）；
#   * `CARD_SETTLE_S=25` 被它写死在 env 前缀里，盖掉仓库里改成的 2（症状是"我明明改了"）。
# 现在由 tools/deploy_bench.py 推上去，并且部署前必过 `shot_policy --check-scripts`（含本文件的
# 纯透传检查 check_entry）。
#
# 用法（调用方负责给出全部自变量；本文件不给任何默认值）：
#   BOARD=http://<板子IP> BDF=0000:01:00.0 VD=372 DELAYS=64 MAX=60 ... bash bench/judge-entry/batch.sh
# 一次调用 = 一个批次：跑完就退出。不挂 cron、不挂循环、不 rescan（操作员红线）。
M=${MIG_DIR:-/root/mig-card2}
GA=${GA_BASE:-/root/ga100vfi}
HOST=$GA/host
export BOARD=${BOARD:?BOARD 未设置：BOARD=http://<板子IP>（地址取板子串口横幅或 /api/status 的 net.ip）}
export BDF=${BDF:?BDF 未设置（BDF=0000:01:00.0；换槽位由调用方给，这里不写死）}
# 外壳自己的节奏（进度打印用，不参与判决）也取自规程，别在这里拍数字。
POLL=$(cd "$HOST" && python3 -m ga100vfi.shot_policy --get batch_poll_s) \
  || { echo "[!!] 读不到规程：部署不完整，不开火"; exit 3; }
WAIT_MAX=$(cd "$HOST" && python3 -m ga100vfi.shot_policy --get batch_max_wait_s)
GRACE=$(cd "$HOST" && python3 -m ga100vfi.shot_policy --get fire_grace_s)

START=$(date +%s)
echo "== 现在 $(date +%H:%M:%S)（判决机 uptime $(cut -d' ' -f1 /proc/uptime) s）=="
head -c 4 "/sys/bus/pci/devices/$BDF/config" 2>/dev/null | od -An -tx1 | tr -s ' ' \
  | sed 's/^/   卡 config[0..3]:/' || echo "   卡节点不在"
# 这一条 burn status 会开一次 BAR0（熔丝块只读）。它是**批前**唯一的一次，且是窗口判据的来源；
# 真正"发前一次 BAR0 都不开"由 crack.sh 的闸门负责。
# 过滤要收紧：卡不在时 burn status 会把 traceback 的源码行打出来，而其中一行写着
# `"efp": self.rd(DSOV_STATUS)` —— 宽松的 `egrep 'dsov'` 会把它当成一条真读数（2026-11-26 上一批
# 就是这么"读到东西"却根本没有 window 行）。只认字段开头的行，加上异常行本身。
(cd "$HOST" && python3 -m ga100vfi.burn --bdf "$BDF" status 2>&1) \
  | egrep '^(fusectrl|crc|dsov|goldilocks window)|^[A-Za-z]+Error' | sed 's/^/   /'

{
  echo "$(date +%FT%T) 手动一批：vd=${VD:-?} delays=${DELAYS:-?} max=${MAX:-?} \
${PULSES:-?}x${PW:-?} gap=${GAP:-?} td=${TD:-?} swin=${SWIN:-?} revert=${REVERT:-?}（ENVELOPE_GATE=0）"
  echo "   前置：卡 $([ -e "/sys/bus/pci/devices/$BDF/config" ] && head -c 4 "/sys/bus/pci/devices/$BDF/config" | od -An -tx1 | tr -s ' ' || echo 无节点)"
} >> "$M/DECISIONS.txt"

echo "== 起：$(date +%H:%M:%S)  vd 起 ${VD:-?} 范围 ${MINV:-?}..${MAXV:-?} step ${STEP:-?} \
delays=${DELAYS:-?} max=${MAX:-?} ${PULSES:-?}x${PW:-?} gap=${GAP:-?} td=${TD:-?} \
swin=${SWIN:-?} revert=${REVERT:-?} =="
# 只有 ENVELOPE_GATE 由本外壳设定（操作员规则：包络门禁现在关掉）；其余环境变量一律继承，
# 所以配方/rhythm 的来源在调用方与 crack.sh，不在这里。
setsid nohup env ENVELOPE_GATE=0 bash "$GA/bench/crack.sh" > "$M/manual-batch.log" 2>&1 < /dev/null &
sleep "$GRACE"
egrep '设备回答|门禁|goldilocks|窗口抓取|交还控制权|没有任何一发' "$M/manual-batch.log" | head -6 | sed 's/^/   /'

for i in $(seq 1 $(( WAIT_MAX / POLL ))); do
  sleep "$POLL"
  pgrep -f 'bash .*crack.sh' >/dev/null || { echo "   [批已结束 +$((i * POLL))s]"; break; }
  export E=$((i * POLL))
  curl -s -m 5 "$BOARD/api/status" | python3 -c '
import json, os, sys
try:
    c = json.load(sys.stdin)["campaign"]
except Exception:
    print("   [+%ss] 板子没答" % os.environ["E"]); raise SystemExit
print("   [+%ss] phase=%s tries=%s under=%s over=%s hit=%s lost=%s goldilocks=%s"
      % (os.environ["E"], c["phase"], c["tries"], c["under"], c["over"], c["hit"],
         c.get("lost", 0), c["goldilocks"]))' 2>/dev/null
done

echo "== 批内时钟（crack.sh 的里程碑；【每周期只买到 2 发】这类问题就长在这几行里）=="
grep -a '\[钟' "$M/manual-batch.log" 2>/dev/null | sed 's/^/   /' \
  || echo "   （没有里程碑：crack.sh 可能根本没起来）"

D=$(find "$GA/logs" -maxdepth 1 -name '*crack-*' -newermt "@$START" 2>/dev/null | head -1)
echo
echo "== 结果（${D:-本批没留下运行目录}）=="
if [ -n "$D" ]; then
  python3 - "$D/agent.log" <<'PY'
import re, sys, collections
per = collections.defaultdict(collections.Counter)
seq = []
for ln in open(sys.argv[1], errors="replace"):
    m = re.search(r"attempt (\d+) delay=(-?\d+) vd=(-?\d+).*-> ([a-z_]+)", ln)
    if m:
        per[(int(m.group(3)), int(m.group(2)))][m.group(4)] += 1
        seq.append("%s:%s/%s" % (m.group(1), m.group(3), m.group(2)))
    elif "读数=" in ln:
        # 清理段补记的那一发（板子打了没人判）。以前这里把 vd 写死成 372 —— 于是任何批的
        # "没人判"那行都记在冻结坐标名下，台账凭空多出一发。现在从同一行里取。
        m2 = re.search(r"attempt (\d+) delay=(-?\d+) vd=(-?\d+).*读数=.*-> ([a-z_]+)", ln)
        if m2:
            per[(int(m2.group(3)), int(m2.group(2)))][m2.group(4)] += 1
            seq.append("%s:?/%s" % (m2.group(1), m2.group(2)))
        else:
            print("   [!!] 有一行 读数= 的记录取不到坐标，没计入分布：%s" % ln.strip()[:120])
for k in sorted(per):
    print("   vd=%-4s delay=%-4s %s" % (k[0], k[1], dict(per[k])))
print("   顺序：", " ".join(seq))
PY
  echo "   判决分布：$(grep -o '\-> [a-z_]*' "$D/agent.log" 2>/dev/null | sort | uniq -c | tr '\n' ' ')"
  echo "   干净读（CRC=0x55）：$(grep -c 'CRC=0x00000055' "$D/agent.log" 2>/dev/null) / $(grep -c 'attempt' "$D/agent.log" 2>/dev/null)"
  echo "   反应（DSOV 动过）：$(grep -c 'DSOV=0x00000000' "$D/agent.log" 2>/dev/null) 发"
  echo "   没花 BAR0 的判决（config-only）：$(grep -c 'bar0=0/' "$D/agent.log" 2>/dev/null) 发"
  grep -a '^glitch vd=' "$D/board.log" 2>/dev/null | tail -1 \
    | grep -o 'pulses=[0-9]* pw=[0-9]* gap=[0-9]* delay=[0-9-]* revert=[0-9]*' | sed 's/^/   板端时间表原文：/'
  echo "   板端实采到轨压样点：$(grep -ac '^vs ' "$D/board.log" 2>/dev/null) 个（swin=0 时应为 0）"
  grep -a '^vs ' "$D/board.log" 2>/dev/null | sed 's/^/      轨压 /'
  grep -a '^vs ' "$D/board.log" 2>/dev/null | grep -o 'v=-\?[0-9]*mV' | sed 's/v=//;s/mV//' \
    | sort -n | head -1 | sed 's/^/      采样最低点：/'
  grep -aE '^glitch done' "$D/board.log" 2>/dev/null | tail -1 | sed 's/^/      /'
fi
echo
# 「卡活到第几秒」：每一批都算，不靠谁临时想起来。它同时相对**那一发坑**与相对**我们最后一次开
# BAR0**摆两个差值 —— 后者才是"我们的读是不是凶手"的量（2026-11-26 那条决定性结论就是这么来的，
# 当时它只是 temp 脚本里的一段，差点就没了）。
echo "== 卡活到第几秒（host/ga100vfi/survival.py）=="
(cd "$HOST" && python3 -m ga100vfi.survival --csv "${PRESENCE_CSV:-$M/presence7.csv}" \
   --logs "$GA/logs") || echo "   （算不出来：presence 或运行目录缺失 —— 那就等于这批没测到寿命）"
echo
echo "== 打完的现场 =="
head -c 4 "/sys/bus/pci/devices/$BDF/config" 2>/dev/null | od -An -tx1 | tr -s ' ' \
  | sed 's/^/   卡 config[0..3]:/' || echo "   卡节点不在"
# 一个周期只花一次 BAR0 去读熔丝状态。若 crack.sh 的 survivor 闸门已经【就地】读过（卡还活着时读的
# 那一次才算数），这里绝不重读：重读只是在一个已死的卡上再花一次会话，还会让人误判成"读过两次"。
if [ -n "$D" ] && [ -s "$D/window-at-halt.txt" ]; then
  echo "   窗口读数（survivor 闸门就地读的，卡当时活着）："
  sed 's/^/     /' "$D/window-at-halt.txt"
else
  (cd "$HOST" && python3 -m ga100vfi.burn --bdf "$BDF" status 2>&1) \
    | egrep '^(fusectrl|crc|dsov|goldilocks window)|^[A-Za-z]+Error' | sed 's/^/   /'
fi
tail -3 "$M/manual-batch.log" | sed 's/^/   批日志: /'
