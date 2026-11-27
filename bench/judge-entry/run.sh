set -uo pipefail
# 判决机上的"一批完整外壳"：板子自证 → 挂 presence → batch.sh → presence 报告 → 板子留痕。
# 与 batch.sh 一样：**仓库里唯一出处、纯透传**（不许自带波形/节奏默认值，见 shot_policy.check_entry
# 与这段注释上面的历史）。调用方（temp/r_fast.sh 那类）负责 export 冻结配方。
#   BOARD=http://<板子IP> bash bench/judge-entry/run.sh
GA=${GA_BASE:-/root/ga100vfi}
HOST=$GA/host
M=${MIG_DIR:-/root/mig-card2}
export BOARD=${BOARD:?BOARD 未设置：BOARD=http://<板子IP>}
export BDF=${BDF:?BDF 未设置（BDF=0000:01:00.0）}
CSV=$M/presence7.csv          # 名字沿用（temp/r_ev3.sh 与 temp/pull_runs.py 按它取证据）
CFG="/sys/bus/pci/devices/$BDF/config"
SP() { ( cd "$HOST" && python3 -m ga100vfi.shot_policy --get "$1" ); }
RATE=$(SP presence_rate_s) || { echo "[!!] 读不到规程：部署不完整"; exit 3; }
SPAN=$(SP presence_span_s)

echo "== 0. 板子自报（版本闸门在 crack.sh 里，这里只留证据）=="
curl -s -m 6 "$BOARD/api/status" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("   板子没答（它没开机 / 网络不通 / 地址不对）"); raise SystemExit(1)
b = d["board"]
print("   net.build = %s\n   boot=%s uptime=%ss baud=%s reanchor=%s"
      % (d["net"].get("build"), b["boot"], b["uptime_s"], b.get("baud"), b.get("reanchor")))
sys.exit(0 if int(b["uptime_s"]) >= 5 else 3)   # 刚起来的板子（uptime < 5 s）：URL 会被复位打断
' || { echo "[!!] 板子不在线或刚复位：先别开火"; exit 3; }
BOOT0=$(curl -s -m 6 "$BOARD/api/status" | python3 -c 'import json,sys;print(json.load(sys.stdin)["board"]["boot"])' 2>/dev/null || echo '?')
echo -n "   卡 config[0..3]:"; head -c 4 "$CFG" 2>/dev/null | od -An -tx1 | tr -s ' ' || echo " 节点不在"

echo
echo "== 1. presence 记录器（只读 config，$SPAN s 自动退出，不 rescan、不开 BAR0）=="
: > "$CSV"
# 这条 csv 是今天所有"卡活到第几秒"结论的唯一来源 —— 它不依赖判决，所以判决丢了它还在。
setsid nohup bash -c "P='$CFG'; O='$CSV'; end=\$(( \$(date +%s) + $SPAN )); while [ \"\$(date +%s)\" -lt \"\$end\" ]; do if [ -e \"\$P\" ]; then v=\$(hexdump -v -e '4/1 \"%02x \"' -n 4 -s 0 \"\$P\" 2>/dev/null | head -1); else v=node-gone; fi; printf '%s,%s\n' \"\$(date +%H:%M:%S.%3N)\" \"\${v:-read-fail}\" >> \"\$O\"; sleep $RATE; done" >/dev/null 2>&1 < /dev/null &
PRES=$!
sleep 3
echo "   已挂：$(wc -l < "$CSV") 行"

echo
echo "== 2. 这批的自变量（全部由调用方 export：本文件一个字都不给默认值）=="
for k in VD MINV MAXV STEP DELAYS MAX PULSES PW GAP TD VR TR SWIN REVERT BATCH PREWARM PREWARM_VD WAIT FIRST_READ_DELAY BAR0_SESSIONS CONFIRM_READS; do
  printf '   %s=%s\n' "$k" "$(eval echo \${$k:-\<未设\>})"
done
export PRESENCE_CSV=$CSV        # 让 batch.sh 的寿命统计用同一份 csv（别让它去猜路径）
bash "$GA/bench/judge-entry/batch.sh"
RC=$?

echo
echo "== 3. presence 掉链段（每段起止 + 时长，最多 14 段）=="
kill "$PRES" 2>/dev/null
python3 - "$CSV" <<'PY'
import datetime as dt, sys
rows = []
for ln in open(sys.argv[1], errors="replace"):
    try:
        t, v = ln.strip().split(",", 1)
        rows.append((dt.datetime.strptime(t, "%H:%M:%S.%f"), v))
    except Exception:
        pass
if not rows:
    print("   没采到样"); raise SystemExit
GOOD = "de 10 c2 20"
print("   样本 %d，覆盖 %s .. %s（%.0f s）" % (len(rows), rows[0][0].strftime("%H:%M:%S"),
      rows[-1][0].strftime("%H:%M:%S"), (rows[-1][0] - rows[0][0]).total_seconds()))
# 段落按"上一个不可达样本"合并：拿上一个好样本比，会把整批的孤立闪断并成一段假的长坑
segs, cur, lastbad = [], None, None
for t, v in rows:
    if v != GOOD:
        if lastbad is None or (t - lastbad).total_seconds() > 1.0:
            cur = [t, t]; segs.append(cur)
        else:
            cur[1] = t
        lastbad = t
for a, b in segs[:14]:
    print("      %s 掉 → %s（%.2f s）" % (a.strftime("%H:%M:%S.%f")[:-3],
          b.strftime("%H:%M:%S.%f")[:-3], (b - a).total_seconds() + 0.25))
bad = sum(1 for _, v in rows if v != GOOD)
print("   掉链事件 %d 次；不可达样本 %d / %d（%.1f%%）；最后一个样本 %s = %s" % (
    len(segs), bad, len(rows), 100.0 * bad / len(rows), rows[-1][0].strftime("%H:%M:%S"),
    "在场" if rows[-1][1] == GOOD else "已掉"))
PY

echo
echo "== 4. 板子现在（boot 从 $BOOT0 变成多少？涨了就是批中被咬）=="
curl -s -m 6 "$BOARD/api/status" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("   板子没答"); raise SystemExit
b = d["board"]
print("   net.build = %s\n   boot=%s uptime=%ss\n   复位留痕：%s"
      % (d["net"].get("build"), b["boot"], b["uptime_s"], b.get("reset", "")))' 2>&1
exit $RC
