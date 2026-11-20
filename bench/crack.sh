#!/bin/bash
# 开火总入口：几何闸门 → 三道预检 → 板载 campaign → 跨轮台账。
#
# 这一版脚本存在的理由是它替你做掉三件「不做就会白烧发数」的事：
#
#  1. 只轮询 /api/status，不轮询 /api/campaign。2f385b0 这版固件 GET /api/campaign
#     不带 op= 永远回 "op=start or op=stop required"，拿它等 running/goldilocks
#     既等不到跑完、也等不到命中。
#  2. 开火前校验网格几何。point_budget_locked() 给每个 delay 点 max/ndelays 次机会，
#     而电压 ladder 要在**同一电压点攒够 batch 个判决**才挪一格。max=20 + 6 个 delay
#     点 + batch=4 ⇒ 每点 3 次，攒不满一个 batch ⇒ 20 发全打在 vd=300，275/250 那段
#     根本没采过。这种浪费从判决流里看不出来，只能开火前算。
#  3. 每条判决带坐标（delay/vd）落盘，并累进跨轮总表 $GA_BASE/logs/ledger.txt。
#     一个电源周期只买得到「打到第一次 poison 为止」的几发，不累计就等于每次重新猜。
#
# 用法（默认即档案 §3.3 命中几何：3×222µs@334，锚在复位释放前 94µs，不用再传波形）：
#   BOARD=http://<设备IP> bash bench/crack.sh
# 加密某个电压段：BOARD=http://<设备IP> MAX=24 BATCH=3 VD=325 MINV=300 MAXV=350 STEP=25 bash bench/crack.sh
# ⚠️ 2026-11-20 前的"文档配置"（1 发 pw=15000 @300mV，delays=0..8000）已定性为
#    档案定义的 over（欠压横跨整个启动窗），两轮各一发 poison —— 别再传那组参数。
set -uo pipefail
# 板子地址不给默认值：写死任何一台台架的地址，都会让下一个用它的人去打别人的卡。
if [ -z "${BOARD:-}" ]; then
  echo "[!!] BOARD 未设置：BOARD=http://<设备IP> bash $0（地址取板子串口横幅或 /api/status 的 net.ip）" >&2
  exit 2
fi
# 判决机上的部署目录由 tools/deploy_bench.py 决定（默认 $HOME/ga100vfi，可用
# BENCH_PREFIX / GA_BASE 覆盖）。写死 /root/... 会让非 root 用户的台架无处可寻。
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
export BDF=${BDF:-0000:01:00.0}    # 示例默认值；换槽位用 BDF= 覆盖，lspci -d 10de: 现取。
# export 是必须的：下面的 poison 预检块是 python heredoc，只认环境里的 BDF，否则你传的
# BDF=… 会被忽略而悄悄打到默认槽位。
HOST=$GA_BASE/host
BENCH=${BENCH:-$GA_BASE/bench}
TS=$(date +%Y%m%d-%H%M%S)
# 断电冷启后卡要几秒才枚举出来；等它，别因为「现在还没有」就判死。0=不等。
WAIT_CARD=${WAIT_CARD:-120}

# 波形默认 = 档案（CMP170HX解锁经验与踩坑总结.md §3.3）唯一命中过的几何：
#   g 334 -40 750 -20 0 0 0 3 222 150 54 0
# 旧默认（1 发 × 15ms @ 300mV）已定性为"把熔丝控制器按在欠压里 14ms"= 档案定义的
# over，两轮各一发 poison —— 它不配再当默认。搜索几何（vd/minv/maxv/step/batch/
# delays/max）随便调；波形几何要改，先拿出比档案更强的证据。
TD=${TD:--40}; VR=${VR:-750}; TR=${TR:--20}; PW=${PW:-222}; GAP=${GAP:-150}
PULSES=${PULSES:-3}; WAIT=${WAIT:-8000}
VD=${VD:-334}; MINV=${MINV:-284}; MAXV=${MAXV:-384}; STEP=${STEP:-25}
BATCH=${BATCH:-2}; MAX=${MAX:-12}; DELAYS=${DELAYS:-54}

# 目录名自描述：固件（net.build 首词）+ 波形几何。以前所有目录都叫 crack2f385b0，
# 刷完固件后这名字开始说谎，而跨轮复盘全靠这些目录 —— 名字必须能回答"哪版固件、
# 什么波形、几发"这三个问题（0.2.x 那天的 20 发空发就是被一个不写字状的目录坑的）。
FWTAG=$(curl -s -m 6 "$BOARD/api/status" 2>/dev/null | \
        python3 -c 'import json,sys;print(json.load(sys.stdin).get("net",{}).get("build","unknown").split()[0])' 2>/dev/null)
# 版本闸门：板子必须自报 build，且等于仓库 main/board.h 的 GA_FW_VERSION。
# 一轮日志之后能不能复盘，全看这一栏；nofw/unknown 就是"没人能证明板上是哪份镜像"。
# EXPECT_FW 必须与 board.h 同步（tools/deploy_bench.py 部署时断言两者一致）。
EXPECT_FW=${EXPECT_FW:-0.3.0}
if [ "$EXPECT_FW" != "any" ] && [ "${FWTAG:-}" != "$EXPECT_FW" ]; then
  echo "[!!] 板子自报固件 = ${FWTAG:-<无 build 字段>}，期望 $EXPECT_FW。"
  echo "     先 BOARD=http://<设备IP> bash bench/ota.sh 刷到本次构建（IMG=… EXPECT=<sha前缀>），再来开火。"
  echo "     确认无误可临时绕过：EXPECT_FW=any bash $0 …（绕过的代价是这轮日志不可追溯）"
  exit 3
fi
DIR=$GA_BASE/logs/$TS-crack-${FWTAG:-nofw}-${PULSES}x${PW}_${VD}
mkdir -p "$DIR"

cd "$HOST" || { echo "[!!] 主机工具不在 $HOST"; exit 1; }
cmd() { curl -s -m 25 -X POST --data-binary "$1" "$BOARD/api/cmd" >/dev/null 2>&1; }

IFS=',' read -r -a DELAY_LIST <<< "$DELAYS"
NDELAY=${#DELAY_LIST[@]}
VISITS=$((MAX / NDELAY))
echo "== 网格几何：${NDELAY} 个 delay 点 × ${MAX} 发 = 每点约 ${VISITS} 次；batch=${BATCH} =="
if [ "$VISITS" -lt "$BATCH" ]; then
  echo "[!!] 这组参数不会自适应：每点只有 ${VISITS} 发，攒不满一个 batch（${BATCH}），"
  echo "     ladder 一整个 campaign 都不会挪电压 —— 上一轮 20 发全打在 vd=300 就是这么来的。"
  echo "     改法：DELAYS=0 单点，或把 MAX 提到 $((NDELAY * BATCH)) 以上。"
  exit 3
fi

echo "== 0. 等卡回到 PCIe 上（只看配置空间，不碰 BAR0）=="
# 冷启之后设备的 sysfs 节点可能还不存在，或存在但读全 1。两种都算「还没回来」。
# 中途试一次 rescan：链路训练晚于内核枚举时，节点会漏建，这不是卡坏了。
CFG=/sys/bus/pci/devices/$BDF/config
deadline=$((SECONDS + WAIT_CARD))
while :; do
  if [ -e "$CFG" ]; then
    VD0=$(python3 -c '
import os, struct, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
v = struct.unpack("<I", os.pread(fd, 4, 0))[0]
os.close(fd)
print("0x%08x" % v)' "$CFG" 2>/dev/null)
    echo "     config-only: id=0x${VD0}"
    [ "$VD0" != "0xffffffff" ] && break
  else
    echo "     $BDF 尚未枚举（还有 $((deadline - SECONDS)) s）"
    [ $((deadline - SECONDS)) -le "$WAIT_CARD" / 2 ] && [ "${RESCAN_TRIED:-0}" = 0 ] && {
      RESCAN_TRIED=1
      echo "     （试一次 pci rescan：链路晚于枚举训练时节点会漏建）"
      echo 1 > /sys/bus/pci/rescan 2>/dev/null || true
    }
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "[!!] 等不到卡。先分清是哪一种，再决定动不动螺丝刀："
    echo "     bash $BENCH/card_probe.sh      # 设备在不在、桥链路宽度、有没有残留驱动"
    echo "     bash $BENCH/link_retrain.sh    # VRM 有无应答 + 对桥发 Retrain + rescan"
    echo "     已确认的判据：VRM 应答 + 板子没按住复位 + 桥发不出链路 = 卡 PHY 没 POR，"
    echo "     热重启不清它，必须拔 12V 与 3V3/VDD33 ≥30 s。"
    exit 3
  fi
  sleep 3
done

echo "== 1. 卡能否判决（config-only 先行，然后只读一次 FUSECTRL 判 poison）=="
# 红线 1（事故 logs/02）：控制器 poison（0xBADFxxxx）时继续读 BAR0 会挂死 PCIe 上游、
# 整机复位。这几天几次「卡掉总线」正是判决循环在 poison 态下读 BAR0 造成的，不是脉冲参数。
# 所以这里只读一个寄存器问一次；classify() 读六个再写一个，绝不许乱来。
python3 - <<'PY' || { echo "[!!] 不可判决：先按提示处理，不要开火"; exit 3; }
import os, struct, sys
from ga100vfi import bar0
from ga100vfi.classify import poisoned
bdf = os.environ.get("BDF", "0000:01:00.0")
fd = os.open(f"/sys/bus/pci/devices/{bdf}/config", os.O_RDONLY)
vd = struct.unpack("<I", os.pread(fd, 4, 0))[0]
os.close(fd)
if vd == 0xFFFFFFFF:
    print("   [!!] 配置空间全 1：卡不在 PCIe 上（断电 ≥30 s 再上电）"); sys.exit(1)
print(f"   config-only: id={vd & 0xFFFF:04x}:{(vd >> 16) & 0xFFFF:04x}")
if not bar0.restore(bdf, wait_s=1.0, settle_s=0.2):
    print("   [!!] 配置空间 restore 失败：卡不在 PCIe 上"); sys.exit(1)
with bar0.Bar0(bdf) as bar:
    if poisoned(bar):
        print("   [!!] FUSECTRL=0xBADFxxxx：控制器已 poison，禁止再读 BAR0。")
        print("       先重启判决机（POST 会拉一次 PERST#）；仍不可达再断电 ≥30 s。"); sys.exit(1)
    print("   [ok] 未 poison，BAR0 可读，判决链可用")
PY

echo "== 2. 锁：先判断；没锁直接打，有锁就解=="
# 判「有没有锁」只看 z 的 pwd（= STATUS_CML & 0x08）。
# 实测：故障锁住的控制器会拒绝密码，而清故障的 GA_CLR(0x03) 在时间线里，时间线又被
# ensure_unlocked() 拦在门外 —— 死结。所以有锁时先手动 CLEAR_FAULTS 再交密码。
# 反向血泪：pwd=0 时开火，每发都在门前 return，PEX_RST 根本不动，判决只是对没被碰过的卡
# 回 under（writes_failed=1 不是参数不对，是毛刺根本没打出去）。
# ⚠️ 2026-11-20 更正：这里曾经特意**不写** 0x46=0x1229，理由是"unlock_cmd 多写的这条会
#    把 cml 顶成 0x28、而两轮都是第 2 发 poison"。那是因果倒置。参考实现
#    (pico-ga100vfi firmware/pwmvid.c:224) 把 0x1229 写在 `u` 里，README 明说它的作用是
#    "stops devinit re-locking the VRM"：第 1 发释放 PEX_RST 后 GPU 跑 devinit，没有 0x1229
#    它就把 VRM 重新锁死，之后每发都死在 ensure_unlocked() 门口（03:13 那轮 12 发只成 1 发，
#    11 次 locked/unreachable 就是这么来的）。所以预检现在与每发的 ensure_unlocked 逐字节一致。
CON="$BENCH/console.sh"
[ -f "$CON" ] || CON=/tmp/.console.sh
if [ ! -f "$CON" ]; then
  # 判决机每次重启都会清空 /tmp，缺这个助手就现场重建，别让依赖文件挡住开火。
  echo "     （台架脚本未部署，现场重建 $CON）"
  mkdir -p "$BENCH" 2>/dev/null || BENCH=/tmp
  CON="$BENCH/console.sh"
  cat > "$CON" <<'CON_EOF'
#!/bin/bash
set -uo pipefail
PORT=${PORT:-/dev/ttyUSB0}
OUT=/tmp/con.gen.log
systemctl stop ModemManager 2>/dev/null || true
fuser -k "$PORT" 2>/dev/null || true
sleep 0.5
stty -F "$PORT" 115200 cs8 -cstopb -parenb raw -echo -hupcl 2>/dev/null || exit 1
( timeout "$((3 + 3 * $#))" cat "$PORT" > "$OUT" 2>/dev/null ) & L=$!
sleep 0.4
for c in "$@"; do printf '%s\r\n' "$c" > "$PORT"; sleep 1.2; done
wait "$L" 2>/dev/null
tr -d '\r' < "$OUT" | grep -vE '^ERR (invalid or overlong|unknown) command$' | tail -30
CON_EOF
fi
[ -e "${PORT:-/dev/ttyUSB0}" ] || { echo "[!!] 没有 ${PORT:-/dev/ttyUSB0}：拒绝盲打"; exit 3; }
Z1=$(bash "$CON" z 2>&1)
echo "$Z1" | grep -m1 'cml=' | sed 's/^/     判前: /'
case "$Z1" in
  *"pwd=1"*) ;;                                        # 没锁：什么都不发，直接打
  *"pwd=0"*) echo "     有锁 → CLEAR_FAULTS(0x03) → 密码 → 0x13=0 → 0x46=0x1229（与每发一致）"
             Z1=$(bash "$CON" "@w 25 03" "@w 25 f2 25 7c" z 2>&1)
             echo "$Z1" | grep -m1 'cml=' | sed 's/^/     解后: /'
             case "$Z1" in
               *"pwd=1"*) W=$(bash "$CON" "@w 25 13 00" "@w 25 46 29 12" "@r 25 46 2" 2>&1)
                          echo "$W" | grep -m1 'I2C' | sed 's/^ *//' | sed 's/^/     0x46 回读: /' ;;
             esac;;
  *) echo "[!!] 串口没有可读答复（线缆/噪声？），拒绝在不知道锁状态时开火"; exit 3;;
esac
case "$Z1" in
  *"pwd=1"*) echo "     [ok] 无锁（pwd=1）→ 开打。不发 w：w 把 VRM 交回 PWM-VID，"
             echo "          GPU 的 PWMVID 接管后 PMBus 的 VOUT_COMMAND 被压制，"
             echo "          永远打不出 Goldilocks。";;
  *) echo "[!!] 仍然 pwd=0：锁解不开。此时开火每发都是空发（PEX_RST 不会下拉），"
     echo "     只会买到一堆假的 under。请断电 ≥30 s 后再来。"; exit 3;;
esac

echo "== 3. 判决代理 =="
pkill -9 -f 'ga100vfi_age[nt]' 2>/dev/null
systemctl stop ga100vfi-agent 2>/dev/null
python3 -m ga100vfi_agent "$BOARD" --bdf "$BDF" > "$DIR/agent.log" 2>&1 &
AGENT=$!
sleep 2
kill -0 "$AGENT" 2>/dev/null || { echo "[!!] 代理起不来："; tail -5 "$DIR/agent.log"; exit 1; }
echo "     [ok] pid=$AGENT（每发判决带坐标；见 poison 即停战并退出）"

cleanup() {
  curl -s -m 8 "$BOARD/api/campaign?op=stop" > /dev/null 2>&1
  # RESTORE/`w` 内部也会写 0xF2（restore_locked）。锁着发它没有意义，且一次被拒的密码写
  # 会撤销已经有效的解锁 —— 所以只在 pwd=1 时才交还控制权，并且交还后立刻回读，
  # 把「w 是否反而锁死了控制器」记录下来，而不是让它静默地把下一轮的火药烧掉。
  if [ -f "$CON" ]; then
    PRE=$(bash "$CON" z 2>&1)
    case "$PRE" in
      *"pwd=1"*) curl -s -m 25 -X POST --data-binary 'w' "$BOARD/api/cmd" >/dev/null 2>&1
                 POST=$(bash "$CON" z 2>&1)
                 echo "     交还控制权前: $(echo "$PRE" | grep -m1 cml=)"
                 echo "     交还控制权后: $(echo "$POST" | grep -m1 cml=)"
                 case "$POST" in *"pwd=0"*) echo "     [!!] w 之后 pwd 变 0：这次交还反而撤销了解锁 —— 下次断电前不要再发 w";; esac;;
      *) echo "     跳过 w：控制器是锁的，发它只会再吃一次被拒的密码写";;
    esac
  fi
  kill -9 "$AGENT" "$SAMP" 2>/dev/null
  python3 - "$DIR/agent.log" "$GA_BASE/logs/ledger.txt" <<'PY'
import os, re, sys, time, collections
log, ledger = sys.argv[1], sys.argv[2]
pat = re.compile(r"attempt (\d+) delay=(-?\d+) vd=(-?\d+).* -> (\w+)")
rows = []
for line in open(log, errors="replace"):
    m = pat.search(line)
    if m:
        rows.append(m.groups())
print("== 本轮逐发台账 ==")
for a, d, v, r in rows:
    print(f"   #{a:>3}  delay={d:>5}  vd={v:>4}  -> {r}")
print(f"== 本轮 {len(rows)} 发 ==")
if not rows:
    print("   本轮没有任何一发产生判决 —— 说明它根本没打出去（查 pwd/门），不是配方问题。")
    sys.exit(0)
# 一发把控制器打进 poison 时，代理会拒绝回传判决（那是红线 1 要求的），所以这里按
# 板子当时记录的坐标补一行 poison 事件 —— 「哪一组参数会打出 over」正是最值钱的信息。
body = open(log, errors="replace").read()
if "POISON" in body and rows:
    d, v = rows[-1][1], rows[-1][2]
    if not any(r[3] == "poison" and r[1] == d and r[2] == v for r in rows):
        rows.append((str(int(rows[-1][0]) + 1), d, v, "poison"))
        print(f"   +1 发记录为 poison（over 的极端形式）: delay={d} vd={v}")
# 累积台账：一次冷启只能打到第一次 poison，所以把这些稀缺的发数攒成一张总表。
new = not os.path.exists(ledger)
with open(ledger, "a") as f:
    for a, d, v, r in rows:
        f.write(f"{time.strftime('%m-%d %H:%M:%S')}\t{d}\t{v}\t{r}\t{os.path.basename(log)}\n")
if new:
    print("   （跨轮台账已新建 —— 每一轮的发数都会累进同一张表）")
tab = collections.defaultdict(collections.Counter)
total = 0
# 台账有两个写者（这里追加 5 列：时间/delay/vd/判决/来源；mkledger.sh 重建 4 列：
# 轮次/delay/vd/判决）。列数不同是历史原因，读的时候必须都吃得下 —— 上一轮就是被
# 4 列行崩在这里（ValueError: not enough values to unpack），台账反而没打出来。
hist = []
for line in open(ledger):
    p = line.rstrip("\n").split("\t")
    if len(p) < 4:
        continue
    d, v, r = p[1], p[2], p[3]
    if not (d.lstrip("-").isdigit() and v.lstrip("-").isdigit()):
        continue
    hist.append((d, v, r, p[-1]))
    tab[(int(d), int(v))][r] += 1
    total += 1
if total:
    print(f"== 跨轮总表（累计 {total} 发）==")
else:
    print("== 跨轮总表（还没有数据）==")
print("   delay    vd   合计  under  over    hit  lost  poison  备注")
for (d, v) in sorted(tab):
    c = tab[(d, v)]
    n = sum(c.values())
    note = ""
    if c.get("poison") or c.get("over"):
        note = "<- 这附近就是边界（over=跌太深=poison）"
    if c.get("hit") or c.get("goldilocks"):
        note = "<== 有命中迹象，下一轮把这里加密采样"
    print(f"   {d:>5}  {v:>4}  {n:>4}  {c.get('under',0):>5}  {c.get('over',0):>5}"
          f"  {c.get('hit',0):>4}  {c.get('lost',0):>4}  {c.get('poison',0):>5}  {note}")
PY
}
trap cleanup EXIT

echo "== 4. 板载 campaign =="
START="$BOARD/api/campaign?op=start&mode=1&vd=$VD&td=$TD&vr=$VR&tr=$TR&batch=$BATCH"
START="$START&pw=$PW&gap=$GAP&pulses=$PULSES&wait=$WAIT&step=$STEP&minv=$MINV&maxv=$MAXV"
START="$START&delays=$DELAYS&max=$MAX"
echo "     vd 起 $VD，范围 ${MINV}..${MAXV}，步长 $STEP，batch $BATCH，delays=$DELAYS，max=$MAX"
echo -n "     设备回答: "; curl -s -m 20 "$START"; echo

( while :; do
    curl -s -m 6 "$BOARD/api/status" \
      | python3 -c '
import json, sys, time
try:
    c = json.load(sys.stdin)["campaign"]
except Exception:
    print(time.strftime("%H:%M:%S") + ",no-answer"); raise SystemExit
print(",".join(str(x) for x in (time.strftime("%H:%M:%S"), c.get("phase"), c.get("tries"),
      c.get("delay"), c.get("v_dip"), c.get("step"), c.get("under"), c.get("over"),
      c.get("hit"), c.get("lost"), c.get("goldilocks"))))' 2>/dev/null
    sleep 3
  done ) > "$DIR/campaign.csv" 2>/dev/null &
SAMP=$!

echo "== 5. 盯到命中、跑完 ${MAX} 发，或设备停下来 =="
for i in $(seq 1 400); do
  S=$(curl -s -m 8 "$BOARD/api/status" 2>/dev/null | python3 -c '
import json, sys
try:
    c = json.load(sys.stdin)["campaign"]
except Exception:
    print("running=? phase=?"); raise SystemExit
print("running=%s phase=%s tries=%s delay=%s vd=%s u=%s o=%s hit=%s lost=%s%s%s" % (
    1 if c.get("running") else 0, c.get("phase"), c.get("tries"), c.get("delay"),
    c.get("v_dip"), c.get("under"), c.get("over"), c.get("hit"), c.get("lost"),
    " GOLDILOCKS=1" if c.get("goldilocks") else "",
    " reason=" + c["reason"] if c.get("reason") else ""))' 2>/dev/null)
  echo "     $S"
  # 三种停法，全部从这个固件真会答的东西里读：板子自报 goldilocks、代理的判决行、
  # 或板子自己 parking（running=0）。旧版盯的是 /api/campaign，它只会答
  # "op=start or op=stop required"，所以这三条一条都触发不了。
  case "$S" in
    *"GOLDILOCKS=1"*) echo "     [!!] 设备自报 GOLDILOCKS —— 窗口打开，别动，马上抓快照"; break;;
  esac
  # 一出现 lost 立刻停火：lost = 这一发之后 PCIe 链路没起来（DSOV 读回全 1）。
  # 2026-11-20 实测：连着两发 lost 之后继续打第三发，控制器就 poison 了 —— 链路没起来时
  # 再打一发等于在熔丝控制器半死的状态下再欠压一次。停火只是丢一发，不停火要重启判决机。
  case "$S" in
    *"lost=0"*) ;;
    *"lost="*) echo "     [!!] 出现 lost（链路没起来）—— 立即停火，不再补发";
               curl -s -m 8 "$BOARD/api/campaign?op=stop" >/dev/null 2>&1; break;;
  esac
  grep -qE -- '-> goldilocks' "$DIR/agent.log" 2>/dev/null && {
    echo "     [!!] 判决代理报告 GOLDILOCKS —— 窗口打开，别动，马上抓快照"; break; }
  case "$S" in
    *"running=0"*) echo "     设备已自行停止（跑完或早停）"; break;;
  esac
  sleep 3
done

curl -s -m 8 "$BOARD/api/status" > "$DIR/final.json" 2>/dev/null
cp "$DIR/campaign.csv" "$DIR/samples.csv" 2>/dev/null
echo "== 最终 /api/status 的 campaign 段 =="
python3 -c 'import json;print(json.dumps(json.load(open("'"$DIR"'/final.json"))["campaign"],ensure_ascii=False))' 2>&1
echo "窗口抓取（只读）：bash $BENCH/window.sh    证据：$DIR"
echo "跨轮总表：       bash $BENCH/mkledger.sh"
