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
STATUS_JSON=$(curl -s -m 6 "$BOARD/api/status" 2>/dev/null)
FWTAG=$(printf '%s' "$STATUS_JSON" | \
        python3 -c 'import json,sys;print(json.load(sys.stdin).get("net",{}).get("build","unknown").split()[0])' 2>/dev/null)
# 版本闸门：板子必须自报 build，且等于仓库 main/board.h 的 GA_FW_VERSION。
# 一轮日志之后能不能复盘，全看这一栏；nofw/unknown 就是"没人能证明板上是哪份镜像"。
# EXPECT_FW 必须与 board.h 同步（tools/deploy_bench.py 在部署时检查三者一致）：
# host/pyproject.toml、bench/crack.sh、main/board.h。升固件版本时三处一起升，
# 少升一处就是部署会被闸门挡下 —— 而挡下之后必须有人去修，不能绕过去继续开火。
EXPECT_FW=${EXPECT_FW:-0.4.0}
if [ "$EXPECT_FW" != "any" ] && [ "${FWTAG:-}" != "$EXPECT_FW" ]; then
  echo "[!!] 板子自报固件 = ${FWTAG:-<无 build 字段>}，期望 $EXPECT_FW。"
  echo "     先 BOARD=http://<设备IP> bash bench/ota.sh 刷到本次构建（IMG=… EXPECT=<sha前缀>），再来开火。"
  echo "     确认无误可临时绕过：EXPECT_FW=any bash $0 …（绕过的代价是这轮日志不可追溯）"
  exit 3
fi
# ---- 速率闸门：板子实际生效的 I²C 速率必须等于配方所标的速率 ----
# 波形配方是 1 MHz 的算术（CHANGELOG 0.3.1）：一笔 16 位 PMBus 写 36 µs，档案配方的 `delay`
# 就是对着这个宽度标的。总线自己降级到 400/100 kHz 之后，每一发的 dip 边沿都被拉长，打出来的
# 判决与台账里前面那一百多发不是同一个实验 —— 而这件事板子自己知道（`board.baud` 是实际生效值，
# 不是请求值），所以这条判据是免费的。0.4.0 换了 I²C 引脚，这条从"有用"升级成"必查"：接错线、
# 忘了接上拉、线长了，第一个症状就是这里降级，而不是"打不中"。
BAUD=$(printf '%s' "$STATUS_JSON" | \
       python3 -c 'import json,sys;print(json.load(sys.stdin).get("board",{}).get("baud",0))' 2>/dev/null)
EXPECT_BAUD=${EXPECT_BAUD:-1000000}
if [ "$EXPECT_BAUD" != "any" ] && [ "${BAUD:-0}" != "$EXPECT_BAUD" ]; then
  echo "[!!] 板子实际 I²C 速率 = ${BAUD:-读不到} Hz，期望 $EXPECT_BAUD Hz（请求值见 /api/status 的 board.requested_baud）。"
  echo "     降级速率下每一发的沿宽与档案不可比：先查上拉/线长/接线（0.4.0 起 SDA=GPIO6、SCL=GPIO7）。"
  echo "     确要以降级速率开火：EXPECT_BAUD=any bash $0 …（并在台账里另记实际速率）"
  exit 3
fi
# ---- 健康区随机扫（SWEEP=random）：几何由包络算，不由我拍 ----
# 为什么需要它：命中是概率事件，而"阶梯往深走一格"等于把每一发都押在悬崖上 —— 今天所有 poison
# 都在 354..358，而卡反复活着回来的地方在 366 以上。所以改成**在健康区里铺开发很多发**，用随机
# 去撞窗口。`healthy_sweep` 只在健康深度带里随机，而且**每个候选坐标都过一遍 check()**：域只是
# 粗筛，族冻结与按坐标禁火才是判决。
# 必须排在 DIR= 之前：随机扫改的就是 VD/MINV/MAXV/STEP/DELAYS，而运行目录名要带扫出来的几何。
if [ "${SWEEP:-off}" = "random" ]; then
  SWE="${TMPDIR:-/tmp}/ga-sweep-$$.env"
  (cd "$HOST" && python3 -m ga100vfi.envelope --sweep \
      --runs "$GA_BASE/logs/*-crack-*" --ledger "$GA_BASE/../logs/verdicts.tsv" \
      --pw "$PW" --td "$TD" --ndelays "${NDELAYS:-4}" ${SEED:+--seed "$SEED"} \
      ${ALLOW_UNRANKED:+--allow-unranked}) > "$SWE" \
    || { echo "[!!] 健康区里没有门禁放行的坐标 → 一发不打（先攒浅侧功课，或改 HEALTHY_BOX 并说明理由）"; exit 3; }
  grep -E '^(DELAYS|MINV|MAXV|STEP|VD)=' "$SWE" > "$SWE.f" && . "$SWE.f"
  rm -f "$SWE" "$SWE.f"
  [ -n "${DELAYS:-}" ] || { echo "[!!] 随机扫没给出 delay → 不起批"; exit 3; }
  echo "== 随机扫（包络生成并核准）：delays=$DELAYS vd ${MAXV}..${MINV} step ${STEP} =="
fi

DIR=$GA_BASE/logs/$TS-crack-${FWTAG:-nofw}-${PULSES}x${PW}_${VD}
mkdir -p "$DIR"

# 开火前的最后一道代码闸门：判决机上**真正会被执行的那份**判决代码必须带 BAR0 闸门。
# 为什么放在这里而不是只写在部署脚本里：今天四次整机死机全部出自同一类动作 —— 把 MMIO
# 打向一块链路已经不存在的卡；而 05:01 那次是因为部署被版本闸门挡下后我照样开了火
# （PowerShell 链不会因为上一条失败就停下），用的就是没闸门的旧 bar0.py。
# 结论：开火入口自己验货，验不过就不许打，而不是指望有人记得部署成功了。
for f in ga100vfi/bar0.py ga100vfi_agent.py; do
  [ -f "$HOST/$f" ] || { echo "[!!] 判决代码缺失：$HOST/$f"; exit 3; }
done
grep -q 'def require_mapped' "$HOST/ga100vfi/bar0.py" || {
  echo "[!!] $HOST/ga100vfi/bar0.py 没有 require_mapped 闸门";
  echo "     映射 BAR0 之前不会先看上游桥 dll_active 和 device id —— 这正是四次整机死机的动作。";
  echo "     先跑 python tools/deploy_bench.py（它会断言这条），别在这一版上开火。"; exit 3; }
grep -q 'Bar0Unavailable' "$HOST/ga100vfi_agent.py" || {
  echo "[!!] agent 不认 Bar0Unavailable：闸门一拦就变成异常，这一发会既没判决又留下悬着的映射"; exit 3; }

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
    # 节点在、但读回全 1 = 链路已经没了。这才是台架最常见的形态（不是"没枚举"），所以
    # 请板子按 PEX_RST 的兜底必须挂在这一支上，否则就是白等 WAIT_CARD 秒（实测发生过一次：
    # 05:25 那发 vd=366/pw=198 判 dead 之后，这里空转了整整 120 s 才报"等不到卡"）。
    if [ "${PEX_TRIED:-0}" = 0 ] && [ $((deadline - SECONDS)) -le "$WAIT_CARD" / 2 ]; then
      PEX_TRIED=1
      echo "     （config 全 1 → 请板子按住 PEX_RST 2 s 再释放；rescan/Retrain 一律不碰）"
      printf 'j' | curl -s -m 10 -X POST --data-binary @- "$BOARD/api/cmd" >/dev/null 2>&1
      sleep 2
      printf 'J' | curl -s -m 10 -X POST --data-binary @- "$BOARD/api/cmd" >/dev/null 2>&1
      sleep 3
    fi
  else
    echo "     $BDF 尚未枚举（还有 $((deadline - SECONDS)) s）"
    # 中途不写桥：`echo 1 > /sys/bus/pci/rescan` 与对桥发 Retrain 实测 4/4 没用，并且有两次
    # 把整台判决机一起挂死（2026-11-21，见 docs/UNLOCK_AND_POISON.md 与本文件顶部的红线）。
    # 真正叫得回卡的是**板子按住 PEX_RST 2 s 再放**（PERST# 是卡的正规复位，实测能把毒态与
    # 掉链的卡都拉回来，不需要人碰螺丝刀）。所以这里改成请板子做一次，然后再等。
    if [ "${PEX_TRIED:-0}" = 0 ] && [ $((deadline - SECONDS)) -le "$WAIT_CARD" / 2 ]; then
      PEX_TRIED=1
      echo "     （请板子按住 PEX_RST 2 s 再释放 —— 不用 rescan，那玩意挂死过整机）"
      printf 'j' | curl -s -m 10 -X POST --data-binary @- "$BOARD/api/cmd" >/dev/null 2>&1
      sleep 2
      printf 'J' | curl -s -m 10 -X POST --data-binary @- "$BOARD/api/cmd" >/dev/null 2>&1
      sleep 3
    fi
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "[!!] 等不到卡。先分清是哪一种，再决定动不动螺丝刀："
    echo "     bash $BENCH/card_probe.sh      # 设备在不在、桥链路宽度、有没有残留驱动"
    echo "     （不要碰对上游桥的任何写入：实测 4/4 没用，并且两次把整台判决机一起挂死）"
    echo "     已确认的判据：VRM 应答 + 板子没按住复位 + 桥发不出链路 = 卡 PHY 没 POR，"
    echo "     热重启不清它，必须拔 12V 与 3V3/VDD33 ≥30 s。"
    exit 3
  fi
  sleep 3
done

echo "== 1. 卡能否判决（只读 config，开火前一次 BAR0 都不花）=="
# 为什么这里不再有 restore()+BAR0：正式入口原来在第 1 步就 bar0.restore()（写卡的 COMMAND
# 打开 mem decode）再 mmap 读 FUSECTRL —— 而那正是 2026-11-21 两次"板子一发都没打、判决机却
# 整机死掉"的现场。红线 1 要防的是 poison 态下读 BAR0，可 poison 本来就有纯 config 的指纹
# （健康只有 10de:20c2，毒态实测 0x2080 与 0x1061），所以这一问根本不需要花 BAR0。
# FUSECTRL 那一读留给开火之后的判决会话：agent 本来就要读 DSOV/CRC，不多这一次。
bash "$BENCH/poison_check.sh" || { echo "[!!] 发前闸门不过（rc=$?）：不开火"; exit 3; }

# 卡回到总线之后、第一发之前固定静置一段（只轮询 config，不碰 BAR0）。
# 依据：今天四次拔电后的**第一发**剖面全是歪的 —— 复位释放前的那几步（st 2 want=-1200、
# st 3/st 4 want=-1000）实测迟到 130~240 µs，06:08 那次还被 `glitch ERR hardware busy` 拒了
# 一次、由固件重试补上。也就是说第一发一直是"在卡与 VRM 没就绪时"执行的。坐标从 374 到 366
# 都死过、slip 47 与 236 都死过，剩下的共同量就是这个窗口。
CARD_SETTLE_S=${CARD_SETTLE_S:-25}
echo "     静置 ${CARD_SETTLE_S}s 再允许第一发（冷上电第一发的时间学一直是歪的）"
sleep "$CARD_SETTLE_S"

# 开火前的 PEX_RST 改成**显式选择**（PRE_PEX=1），默认不做。
# 它本来是防"冷上电熔丝控制器没起来 → FUSECTRL=0 → 判决 unknown → 波形白打"（05:20 实测过），
# 但代价是给每一批的第一发前面再摁一次硬复位。今天四次硬死全都有它，唯一活下来的 24 发那批也
# 有它 —— 它不足以致死，却是"第一发落在复位恢复窗口里"这个混杂量的制造者。默认拿掉它，白烧一发
# 的损失由 agent 承担（FUSECTRL=0 判 unknown，红线 2）；确认卡冷到需要复位时再显式 PRE_PEX=1。
if [ "${PRE_PEX:-0}" != "0" ]; then
  echo "     PRE_PEX=$PRE_PEX：开火前 PEX_RST 一次（默认不做，理由见上）"
  printf 'j' | curl -s -m 10 -X POST --data-binary @- "$BOARD/api/cmd" >/dev/null 2>&1
  sleep 2
  printf 'J' | curl -s -m 10 -X POST --data-binary @- "$BOARD/api/cmd" >/dev/null 2>&1
  sleep 3
fi

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
echo "     [ok] pid=$AGENT（每发判决带坐标；见 poison / 见反应（DSOV 动）即停战并退出）"

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
  python3 - "$DIR/agent.log" "$GA_BASE/logs/ledger.txt" "$BDF" "$GA_BASE/../logs/verdicts.tsv" <<'PY'
import os, re, sys, time, collections
log, ledger = sys.argv[1], sys.argv[2]
bdf = sys.argv[3] if len(sys.argv) > 3 else "0000:01:00.0"
verdicts = sys.argv[4] if len(sys.argv) > 4 else ""
# 判决行的**全部**读数都要留住（regs = `->` 之前那段，含 DSOV/CRC/PRIV/FPF/slip）：verdicts.tsv
# 的行尾带上它，包络才能从真值里也看见遥测，而不是只有运行目录那份看得到。
pat = re.compile(r"attempt (\d+) delay=(-?\d+) vd=(-?\d+)(.*?) -> (\w+)(.*)")
rows = []
for line in open(log, errors="replace"):
    m = pat.search(line)
    if m:
        a, d, v, regs, r, tail = m.groups()
        rows.append((a, d, v, r, regs.strip(), tail.strip()))
print("== 本轮逐发台账 ==")
for a, d, v, r, _, _ in rows:
    print(f"   #{a:>3}  delay={d:>5}  vd={v:>4}  -> {r}")
# 发数汇总与"0 发就退出"**必须放在两个补行之后**。以前它写在最前面，于是判决 0 行的那一轮
# （今天六次硬死里五次就是这个形状）在这里 sys.exit(0)，下面为它写的补行根本轮不到执行 ——
# 06:44 那批实测就是这样：卡当场被打死，台账还是报"本轮 0 发"。
# 一发把控制器打进 poison 时，代理会拒绝回传判决（那是红线 1 要求的），所以这里按板子
# 当时记录的坐标补一行 poison 事件 —— 「哪一组参数会打出 over」正是最值钱的信息。
#
# 坐标必须来自板子（final.json 的 campaign.delay / v_dip），不能拿"最后一条判决"的坐标
# 顶替：BATCH=1 时每一发都换一档电压，最后一条判决属于上一发。2026-11-20 第二张卡就这样
# 被记错过一次 —— 板子停在 v_dip=334，台账写成 339 poison，而 334/339 差一个 PMBus 码，
# 正是我们用来夹边界的那个数。板子读不到时才退回最后一条判决的坐标，并且标明是估的。
body = open(log, errors="replace").read()
if "POISON" in body and rows:
    d, v = rows[-1][1], rows[-1][2]
    note = ""
    try:
        import json
        with open(os.path.join(os.path.dirname(log), "final.json")) as fj:
            c = json.load(fj).get("campaign", {})
        if c.get("delay") is not None and c.get("v_dip") is not None:
            d, v = str(c["delay"]), str(c["v_dip"])
        else:
            note = "（坐标取自最后一条判决，未经板子确认）"
    except (OSError, ValueError) as exc:
        note = f"（final.json 不可用：{type(exc).__name__}，坐标取自最后一条判决）"
    if not any(r[3] == "poison" and r[1] == d and r[2] == v for r in rows):
        rows.append((str(int(rows[-1][0]) + 1), d, v, "poison", "", ""))
        print(f"   +1 发记录为 poison（over 的极端形式）: delay={d} vd={v} {note}")
# 板子打了、判决一行都没有 —— 今天六次硬死里五次就是这个形状。而这种发以前**不进台账**，
# 后果是"刚把卡打死的那一族"在包络里依然零事故，下一轮门禁照样放行同一个形状去打死第二次
# （本轮 pw=222 就是这样：它已经带走一次拔电，总表却还写着它零事故）。
# 判据必须看得见事故，所以按现场证据补一行，坐标取板子记在 final.json 里的那一对（与 poison
# 补行同一来源，不拿"上一条判决"顶替）：
#   * 批次结束时卡已从 config space 消失（端点自己关掉，PEX_RST 也叫不回）→ dead；
#   * 卡还在、只是判决没读回来 → nojudge（算打过，不算事故）。
if not rows:
    tries, d, v = 0, None, None
    try:
        import json
        with open(os.path.join(os.path.dirname(log), "final.json")) as fj:
            camp = json.load(fj).get("campaign", {})
        tries = int(camp.get("tries") or 0)
        d, v = camp.get("delay"), camp.get("v_dip")
    except (OSError, ValueError):
        pass
    if tries and d is not None and v is not None:
        try:                                  # 只读 config 头两字节，一次 BAR0 都不花
            with open(f"/sys/bus/pci/devices/{bdf}/config", "rb") as cf:
                gone = cf.read(2) == b"\xff\xff"
        except OSError:
            gone = True                       # 节点被内核摘掉，也是"卡不在"
        r = "dead" if gone else "nojudge"
        rows.append((str(tries), str(d), str(v), r, "", ""))
        print(f"   +1 发记录为 {r}：板子打了 {tries} 发而判决 0 行"
              f"（{'卡已从 config space 消失，PEX_RST 叫不回' if gone else '卡还在，判决没读回来'}）")
        # 同时按代理自己的格式回写 agent.log：包络与 mkledger 都从那里读发数，只写这份 5 列
        # 的 ledger.txt 谁也不认 —— 这正是本次漏洞的成因（发数存在，事故不可见）。
        try:
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"session cleanup attempt {tries} delay={d} vd={v} "
                        f"读数={'卡不在总线' if gone else '没读回'} -> {r}\n")
        except OSError:
            pass
if not rows:
    print("   本轮没有任何一发产生判决 —— 说明它根本没打出去（查 pwd/门），不是配方问题。")
    sys.exit(0)
print(f"== 本轮 {len(rows)} 发 ==")
# 累积台账：一次冷启只能打到第一次 poison，所以把这些稀缺的发数攒成一张总表。
new = not os.path.exists(ledger)
with open(ledger, "a") as f:
    for a, d, v, r, _, _ in rows:
        f.write(f"{time.strftime('%m-%d %H:%M:%S')}\t{d}\t{v}\t{r}\t{os.path.basename(log)}\n")
# **verdicts.tsv 是包络的真值**（"台账优先、运行目录只补差额"），而回滚后的代理只写 agent.log，
# 于是 2026-11-21 那天它从 05:25 起 5.5 小时一行没长 —— 真值成了化石，门禁的"浅档攒了几发"实际
# 全靠运行目录兜着，而运行目录是副本。补写这件事只能在这里做：判决行不写 pw，而门禁按 pw 分档，
# 只有本脚本知道 pw 与脉冲数。每批收尾写自己那几行（一次 trap EXIT 一次），所以不需要去重。
if verdicts:
    pw, pl = os.environ.get("PW", "?"), os.environ.get("PULSES", "?")
    try:
        with open(verdicts, "a") as f:
            for a, d, v, r, regs, tail in rows:
                extra = " ".join(x for x in (tail, regs) if x)
                f.write(f"{time.strftime('%H:%M:%S')}\tvd={v} pw={pw} d={d} p={pl} -> {r}"
                        + (f" {extra}" if extra else "") + "\n")
    except OSError as exc:
        print(f"   [!!] verdicts.tsv 写不进去（{exc}）：包络的真值这一批不会长")
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
# cleanup 里会 kill 这两个 pid，而门禁/闸门在任何一步 exit 都会触发 trap —— 不初始化就是
# `SAMP: unbound variable`（set -u 下实测发生过一次：门禁拒绝后 cleanup 半路崩，交还控制权
# 那段没跑完，卡留在被接管状态）。空值就是"还没起来"，kill 跳过即可。
AGENT=${AGENT:-}
SAMP=${SAMP:-}
trap cleanup EXIT

# ---- 包络门禁（host/ga100vfi/envelope.py）：审这次 campaign 的**最深点** ----
# 为什么在 crack.sh 里而不是在外面套壳：几何（pw/深度/td/delay）只有这里齐全，而"贴着安全
# 侧使劲打、攒够发数才往外挪一格"这条判断必须是代码 —— 今天上午我把 td=+10 当成"更浅更安全"
# 放了进去，那一发四秒后把卡带走；pw>=400@354/356/357、delay=84..100 各打死过卡。这些结论
# 从这里开始是 RUNGS + FATAL 两张表，由 host/tests/test_envelope.py 钉住，不再靠我记得。
# 审最深点而不是起点：ladder 会自己往深里掉，只审 VD= 等于没审。
if [ "${ENVELOPE_GATE:-1}" != "0" ]; then
  cd "$HOST" || { echo "[!!] 判决代码不在 $HOST：门禁不能跳过"; exit 3; }
  python3 -m ga100vfi.envelope --runs "$GA_BASE/logs/*-crack-*" \
      --ledger "$GA_BASE/../logs/verdicts.tsv" \
      --vd "$VD" --minv "$MINV" --maxv "$MAXV" --pw "$PW" \
      --delay "$(echo "$DELAYS" | cut -d, -f1)" --delays "$DELAYS" --td "$TD" \
      --label "本次 campaign" \
      || { echo "[!!] 包络门禁不放行 → 一发不打。要改判据就改 RUNGS/allowed_rungs 并跑测试"; exit 3; }
  cd "$GA_BASE" || exit 1
else
  echo "[!!] ENVELOPE_GATE=0：跳过包络门禁（这一轮的深度没有代码把关）"
fi

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
  # 连续 lost 才停火，孤立的一发 lost 不停 —— 规则本身要有实测依据，不能比证据更保守。
  # 实测（2026-11-20）：**连着两发** lost 之后再打第三发，控制器才 poison。所以第一发 lost
  # 就停是把"链路没起来"当成了深度证据；而 lost 只是"这发没读回来"，板子每发 g 自带 PERST#，
  # 链路本来就由下一发的复位重新拉起。2026-11-21 实证：今天 lost 很多但卡都能被 PEX_RST
  # 叫回来，而"一发 lost 就冻整批"让 24 发的批次只打了 2 发 —— 贴着安全侧猛打的瓶颈就是它。
  # 真正不能碰的是 poison 与 config=ffff:ffff 的硬死，那两条各自有闸门。
  CONS_LOST=$(python3 - "$DIR/agent.log" <<'PY' 2>/dev/null
import re, sys
tail = []
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.search(r"-> (under|over|hit|lost|poison|goldilocks|unknown)", line)
            if m:
                tail.append(m.group(1))
except OSError:
    print(0); raise SystemExit
n = 0
for v in reversed(tail):
    if v != "lost":
        break
    n += 1
print(n)
PY
)
  case "${CONS_LOST:-0}" in
    0|1) ;;
    *) echo "     [!!] 连续 ${CONS_LOST} 发 lost（链路起不来）—— 停火，别再补发";
       curl -s -m 8 "$BOARD/api/campaign?op=stop" >/dev/null 2>&1; break;;
  esac
  case "$S" in
    *"lost="*) ;;      # 孤立 lost 交给上面的连续计数，不再在这里一票否决
  esac
  grep -qE -- '-> goldilocks' "$DIR/agent.log" 2>/dev/null && {
    echo "     [!!] 判决代理报告 GOLDILOCKS —— 窗口打开，别动，马上抓快照"; break; }
  case "$S" in
    *"running=0"*) echo "     设备已自行停止（跑完或早停）"; break;;
  esac
  sleep 3
done

curl -s -m 8 "$BOARD/api/status" > "$DIR/final.json" 2>/dev/null
# 板子侧时间学（每一次 glitch 的 st/want/slip 都在这里）。以前这两个文件是我 temp 里那个私人
# 观察器顺手落的，所以**只在它跑过的批次里有** —— 而它一不在，打死卡的那一发就变成无头案：
# 今天五次硬死的运行目录连一条波形记录都没留下，只能靠回忆说"第一发剖面是歪的"。仓库入口必须
# 自己带得动这份证据：一次 GET，落盘一次，不参与任何计数（观察器 v1 就是因为反复追加同一份日志
# 把同一发 glitch done 数了两遍）。板子重启会清空 /api/log，所以这份快照必须**每批结束就抓**。
curl -s -m 15 "$BOARD/api/log" > "$DIR/board.log" 2>/dev/null
echo "   board.log $(wc -l < "$DIR/board.log" 2>/dev/null || echo 0) 行"
cp "$DIR/campaign.csv" "$DIR/samples.csv" 2>/dev/null
echo "== 最终 /api/status 的 campaign 段 =="
python3 -c 'import json;print(json.dumps(json.load(open("'"$DIR"'/final.json"))["campaign"],ensure_ascii=False))' 2>&1
# 每批总结（规则：每次打完必须有一句人话总结 —— 靠我记得写，就一定会有几批白打）。
# 它同时回答三个问题：这批算不算"有反应"、这一档攒够发数没有、下一档允许打到多深。
echo "== 每批总结（host/ga100vfi/envelope.py）=="
(cd "$HOST" && python3 -m ga100vfi.envelope --runs "$GA_BASE/logs/*-crack-*" \
   --ledger "$GA_BASE/../logs/verdicts.tsv" \
   --label "$TS pw=${PULSES}x${PW} vd ${VD}..${MINV} delay=${DELAYS}") \
  || echo "   （总结没出来：判决代码或台账缺失，不影响本轮发数）"
echo "窗口抓取（只读）：bash $BENCH/window.sh    证据：$DIR"
echo "跨轮总表：       bash $BENCH/mkledger.sh"
