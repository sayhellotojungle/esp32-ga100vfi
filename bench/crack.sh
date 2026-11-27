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
# 用法（默认即档案命中几何：3×222µs@334，锚在复位释放前 94µs，不用再传波形）：
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

# 波形默认 = 档案（docs/CMP170HX解锁经验与踩坑总结.md §3.3）唯一命中过的几何：
#   g 334 -40 750 -20 0 0 0 3 222 150 54 0
# 旧默认（1 发 × 15ms @ 300mV）已定性为"把熔丝控制器按在欠压里 14ms"= 档案定义的
# over，两轮各一发 poison —— 它不配再当默认。搜索几何（vd/minv/maxv/step/batch/
# delays/max）随便调；波形几何要改，先拿出比档案更强的证据。
TD=${TD:--40}; VR=${VR:-750}; TR=${TR:--20}; PW=${PW:-222}; GAP=${GAP:-150}
PULSES=${PULSES:-3}
# WAIT（发后静置，向主机要判决之前）不在这里写默认值：它是规程键 settle_wait_ms，见下面的 SP 块。
# 每一发的两个物理开关（0.5.4 起板子才认，默认 0 = 与档案里那 170 多发同一形状）：
#   SWIN=us   毛刺期间用 0x8b 采真实轨压，进 board.log 的 `vs` 行。本台架唯一的物理反馈：
#             在此之前"坑"是否存在只能推断——一笔 16 位写要 104~320 us 才落（从机在拉伸时钟），
#             而脉冲本身只有 222 us。
#   REVERT=1  时间表内恢复 VRM（post+500 ms 那笔 GA_MODE）。11-24 对照：同坐标 5/5 存活，
#             revert=0 那组死；战役一直写死 0，所以这条从未在开火时被试过。
# 0.5.3 及更早的板子不查这两个键，多发在 URL 里也只是被忽略——加了不会挡旧固件。
SWIN=${SWIN:-0}; REVERT=${REVERT:-0}
# ---- 0.5.5 的三颗新旋钮（都不动波形） ----
#   PREWARM=1  正式批之前先打一发"预热"，**由一个独立代理判决，写独立日志**。依据是我们自己的数据：
#              三次"批内首发"打死卡里两次是拔电后的第一发，11-21 的记录同样是 6 次掉链里 5 次发生在
#              拔电后第一发。卡刚从 12V/3V3 上电，它的第一发与后面每一发不是同一个实验；用一发买掉
#              这个特殊状态，比在第 8 发上意外死掉便宜。预热发打的坐标与正式批相同、波形逐参数相同，
#              所以它**必须**被判决：非 under（尤其 goldilocks/hit）就直接不开正式批 —— 拿 60 发盖掉
#              唯一一次命中，是本台架最贵的一种"用假 under 换真反应"。
#   PREWARM_WAIT=s  预热发之后最多等多久（判决 + 卡回总线；默认见 shot_policy）：这一发要走完
#              settle($WAIT) + 代理读 + 复核，比盲睡长是正常的。等不到卡就**停手**（绝不按 PEX_RST：
#              真命中之后再按一次就是把熔丝链重放一遍，那颗 arm 就没了）。
#   CONFIRM_READS / STABLE_S  判决复核（见 host/ga100vfi_agent.py 的 _needs_confirm）。脏读
#              （11-25 那批 ~1/3 的 CRC=0x57/0x75）与真反应在单发单读下同形，必须靠复读分开。
# ---- 每发的节奏：唯一出处是 host/ga100vfi/shot_policy.py，这里**不许写字面量** ----
# 为什么这么定：这些数以前散在四处 —— 这里、判决机上不在版本库里的入口层（它把 CARD_SETTLE_S=25
# 写死过，盖掉仓库里的改动）、代理 argparse 的默认值（first_read_delay 这里 4、那里 2）、以及每次
# 开火前临时 export 的人。散着的直接后果是"我改了但没生效"和"两个默认值互相咬"（把 first_read_delay
# 调大就会超过代理的判决预算，于是每一发都判成 lost）。所以现在：
#   * 值只写在 shot_policy.py；这里用 SP 取，**取不到就停**，绝不安静地退回一个旧默认值；
#   * tools/deploy_bench.py 部署前跑 `shot_policy --check-scripts`，别处再写字面量就拒绝部署；
#   * 要做实验就在操作脚本里显式 export 覆盖（那样它在台账里是看得见的自变量，而不是一个隐蔽分叉）。
SP() {                      # SP <键名> —— 取一个规程键
  local v
  v=$( cd "$HOST" && python3 -m ga100vfi.shot_policy --get "$1" 2>/dev/null )
  case "$v" in
    ''|*[!0-9.]*) echo "[!!] 取不到 shot_policy.$1（得到：${v:-空}）：不开火。每发的节奏只有一份出处，读不到就是环境坏了，不是可以蒙过去跑一批的事" >&2; exit 3 ;;
  esac
  printf '%s' "$v"
}
# 【钟】里程碑打印。为什么要有它：今天每一批"从起批到第一发"到底花在哪一步，我们只能靠文件 mtime
# 猜（07:49:42 起批 → 07:50:35 战役第一发，中间 50 s 全在预热段，而预热本该 ~10 s）。想改吞吐就得先
# 知道那 50 s 是 op=start 的往返、是代理起进程、还是等 campaign park 的轮询。$SECONDS 是 bash 自带的
# 批内累计秒数，不用自己记原点。
TS() { echo "     [钟 $(date +%H:%M:%S) | 批内 ${SECONDS}s] $*"; }
TS "crack.sh 起来（版本/baud 闸门之前，规程键已读齐）"
WAIT_CARD=${WAIT_CARD:-$(SP wait_card_s)}
PREWARM=${PREWARM:-$(SP prewarm)}
PREWARM_WAIT=${PREWARM_WAIT:-$(SP prewarm_wait_s)}
# 预热发的坐标：它【不是】$VD 的别名。预热负责吸收冷态第一发，所以永远打在规程给的"从没丢过卡的
# 格子"上；战役起点 VD 负责试深。混在一起的那天（08:54:22）起点抬到 363 ⇒ 预热首发打死卡 ⇒ 闸门
# 拒开正式批 ⇒ 一个电源周期 0 判决。
PREWARM_VD=${PREWARM_VD:-$(SP prewarm_vd_mv)}
CONFIRM_READS=${CONFIRM_READS:-$(SP confirm_reads)}
STABLE_S=${STABLE_S:-$(SP stable_s)}
# 一发允许的熔丝块 BAR0 会话数（0 = config-only 判决：一次 BAR0 都不开，判决天生未盖章）。
# 它是【阶段设定】：hunt=1、hammer=0 由 shot_policy 按 MODE 显式导出。做实验要改就改 MODE 或显式
# export，别把它当全局默认改 —— 默认必须是能盖章的那一档（否则人工直调就静默产出未盖章 under）。
BAR0_SESSIONS=${BAR0_SESSIONS:-$(SP bar0_sessions_per_shot)}
WAIT=${WAIT:-$(SP settle_wait_ms)}     # 板子打完一发之后多久向主机要判决（不是波形）
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
EXPECT_FW=${EXPECT_FW:-0.5.9}
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

# 目录名要能把"采样开没开、恢复在不在时间表内"区分开：这两个开关改的是物理形状，
# 不带后缀的话 0.5.4 的两批在台账里就长成同一个实验。envelope 的 RUN_DIR 是 search，
# 后缀不影响它解析 pulses/pw/vd。
TAGSFX=""
if [ "${SWIN:-0}" != 0 ]; then TAGSFX="${TAGSFX}_s${SWIN}"; fi
if [ "${REVERT:-0}" != 0 ]; then TAGSFX="${TAGSFX}_r${REVERT}"; fi
# td 也是自变量（正 td = 坑打在正在启动的卡上），默认值之外必须落到目录名里
if [ "${TD:--40}" != "-40" ]; then TAGSFX="${TAGSFX}t${TD}"; fi
DIR=$GA_BASE/logs/$TS-crack-${FWTAG:-nofw}-${PULSES}x${PW}_${VD}${TAGSFX}
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

# ===== 发前规程闸门：全量 FF capture 离第一发必须够远 =====
# 为什么这是一道闸门而不是一条注释：2026-11-26 我把 early 的 487 行基线塞进开火脚本，之后
# "每个电源周期都在第 2 发死"就固定出现了，而那次 capture 离第一发只有 ~30 s。全量 capture =
# 487 次熔丝块 CMD_READ，正是红线 1 说的"我们的读干扰卡自己的启动"。**规程要有牙，就得在这里挡。**
# 想挡掉这条只有一条路：显式 CAPTURE_OK_OLD=1（并在台账里写清理由）。
CAP_STAMP="$GA_BASE/state/last-capture"
if [ -f "$CAP_STAMP" ] && [ "${CAPTURE_OK_OLD:-0}" != "1" ]; then
  then_c=$(cat "$CAP_STAMP" 2>/dev/null || echo 0)
  gap=$(( $(date +%s) - then_c ))
  min=$(SP capture_min_gap_s)
  if [ "$gap" -lt "$min" ]; then
    echo "[!!] 距上次全量 FF capture 只有 ${gap}s（规程 >=${min}s）：不开火。" >&2
    echo "     那段时间里的几百次熔丝块 CMD_READ 会落进卡自己的启动里（红线 1）。要么等够，" >&2
    echo "     要么本轮就用已存的那份基线，要么显式 CAPTURE_OK_OLD=1 并在台账写明理由。" >&2
    exit 3
  fi
  echo "     capture 闸门：距上次全量 capture ${gap}s >= ${min}s ✓"
fi

# 卡回到总线之后、第一发之前静置一段（只轮询 config，不碰 BAR0）。值在 shot_policy（2 s）：
# 旧值 25 s 是为了盖"冷上电第一发剖面歪"那个观察，而那个观察现在可测（0.5.9 每发报
# `stretch_us`），能测的东西不该用固定睡眠盖；冷态第一发的真正吸收器是预热发。
CARD_SETTLE_S=${CARD_SETTLE_S:-$(SP card_settle_s)}
echo "     静置 ${CARD_SETTLE_S}s 再往下走（冷态第一发由预热发吸收；冷偏现在以 stretch_us 计量）"
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
# 串口往返的预算由规程给（console_tries / console_wait_s / console_repeat）。为什么要点名管它：
# 2026-11-26 08:05:33 那批，§2 这两次 console 往返实测 38 s（监听窗口无论有没有答复都跑满），而这台
# 卡多数周期只活 ~60 s。导出后两份 console.sh（bench/ 那份与下面内嵌的兜底副本）共同继承同一预算。
export CON_TRIES=$(SP console_tries) CON_WAIT=$(SP console_wait_s) CON_REPEAT=$(SP console_repeat)
CON="$BENCH/console.sh"
[ -f "$CON" ] || CON=/tmp/.console.sh
if [ ! -f "$CON" ]; then
  # 判决机每次重启都会清空 /tmp，缺这个助手就现场重建，别让依赖文件挡住开火。
  # 这份内嵌副本与 bench/console.sh 同语义（清残留 + 同一窗口内重发；test_regressions 钉住了
  # 两边必须同时含 CON_REPEAT/清残留，免得两份各自漂移）。
  echo "     （台架脚本未部署，现场重建 $CON）"
  mkdir -p "$BENCH" 2>/dev/null || BENCH=/tmp
  CON="$BENCH/console.sh"
  cat > "$CON" <<'CON_EOF'
#!/bin/bash
set -uo pipefail
PORT=${PORT:-/dev/ttyUSB0}; OUT=${OUT:-/tmp/con.gen.log}
CON_TRIES=${CON_TRIES:-2}; CON_REPEAT=${CON_REPEAT:-3}; CON_WAIT=${CON_WAIT:-3}   # 兜底=规程值（test_regressions 钉住一致）
CON_GREP=${CON_GREP:-cml=}
systemctl stop ModemManager 2>/dev/null || true
fuser -k "$PORT" 2>/dev/null || true
sleep 0.4
stty -F "$PORT" 115200 cs8 -cstopb -parenb raw -echo -hupcl 2>/dev/null || exit 1
timeout 0.5 cat "$PORT" > /dev/null 2>&1 || true      # 清残留（上一轮/开机噪声）
printf '\r\n' > "$PORT"                                # 预热：吞掉 ch341 第一笔被吞的写
timeout 0.5 cat "$PORT" > /dev/null 2>&1 || true
for try in $(seq 1 "$CON_TRIES"); do
  : > "$OUT"
  ( timeout "$CON_WAIT" cat "$PORT" > "$OUT" 2>/dev/null ) & L=$!
  sleep 0.3
  for i in $(seq 1 "$CON_REPEAT"); do
    for c in "$@"; do printf '%s\r\n' "$c" > "$PORT"; done
    sleep 1.0
    grep -q "$CON_GREP" "$OUT" 2>/dev/null && break
  done
  wait "$L" 2>/dev/null
  if grep -q "$CON_GREP" "$OUT" 2>/dev/null; then
    tr -d '\r' < "$OUT" | grep -vE '^ERR (invalid or overlong|unknown) command$' \
      | grep -a "$CON_GREP" | tail -6
    exit 0
  fi
  sleep 0.6
done
bytes=$(wc -c < "$OUT" 2>/dev/null || echo 0)
echo "console: ${CON_TRIES} 轮都没有「$CON_GREP」答复（收到 ${bytes} 字节）"
[ "$bytes" -gt 0 ] && tr -d '\r' < "$OUT" | grep -vE '^ERR (invalid or overlong|unknown) command$' | tail -6
exit 2
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
  *) echo "[!!] 锁状态判不出来（重试之后仍没有 cml=），拒绝在不知道锁状态时开火。原文："
     echo "$Z1" | tail -6 | sed 's/^/     | /'
     echo "     空 = 线缆/适配器；非空 = 板子答了别的（ERR status read 之类）。下一步不同，别混。"
     exit 3;;
esac
case "$Z1" in
  *"pwd=1"*) echo "     [ok] 无锁（pwd=1）→ 开打。不发 w：w 把 VRM 交回 PWM-VID，"
             echo "          GPU 的 PWMVID 接管后 PMBus 的 VOUT_COMMAND 被压制，"
             echo "          永远打不出 Goldilocks。";;
  *) echo "[!!] 仍然 pwd=0：锁解不开。此时开火每发都是空发（PEX_RST 不会下拉），"
     echo "     只会买到一堆假的 under。请断电 ≥30 s 后再来。"; exit 3;;
esac

echo "== 2.9 预热发（PREWARM，单独一份判决日志，不算本批坐标）=="
# 为什么花这一发：本台架的死亡分布不是均匀的 —— 三次"批内首发打死卡"里两次是拔电之后的第一发，
# 11-21 的记录同样是 6 次掉链里 5 次发生在拔电后第一发。刚上电的卡与打过一发的卡不是同一个实验
# 对象。买掉它只花一发，而它在正式批里第 1 发发生时，我们损失的是整批。
#
# 但它**必须有判决**（2026-11-26 修正自己的第一版设计）：预热发打的正是本批的坐标，如果它本身就是
# 那一发命中，"没人判 → 继续打 60 发"就是我们一直在骂的"假 under 换真反应"，而且这次换掉的是唯一
# 一次命中。所以这里起一个**独立的代理、写独立日志**（agent-prewarm.log）：正式批的 agent.log 与
# 坐标清点仍然干净，而预热发的判决作为物证保留，并且成为开正式批的闸门。
#
# 波形与正式批**逐参数相同**（含 wait=$WAIT）：预热要买的是"这张卡的第一发"这个状态，那就得打同
# 一发。任何一项不同（尤其把 settle 改小让它快点回来）都意味着我们在预热另一个实验。
if [ "$PREWARM" != "0" ]; then
  TS "预热发开始（坐标 $PREWARM_VD=规程的 prewarm_vd_mv，与战役起点 $VD 无关）：起独立代理 → op=start(max=1) → 等它 park → 取判决"
  pkill -9 -f 'ga100vfi_age[nt]' 2>/dev/null
  systemctl stop ga100vfi-agent 2>/dev/null
  python3 -m ga100vfi_agent "$BOARD" --bdf "$BDF" --first-read-delay "${FIRST_READ_DELAY:-$(SP first_read_delay_s)}" \
      --confirm-reads "$CONFIRM_READS" --stable-s "$STABLE_S" \
      --max-bar0-sessions "$BAR0_SESSIONS" > "$DIR/agent-prewarm.log" 2>&1 &
  PW_AGENT=$!
  sleep 2
  kill -0 "$PW_AGENT" 2>/dev/null || { echo "[!!] 预热代理起不来："; tail -5 "$DIR/agent-prewarm.log"; exit 1; }
  # 先替上一批把战役关掉：上一批若有【一发没人判】（代理被 presence 收走 / 被杀），板子会一直停在
  # awaiting_verdict，于是本批的 op=start 直接吃到 `glitch ERR hardware busy` —— 2026-11-26 07:06
  # 那批就是这么一发都没打（卡整批活着，我们白白看了它 60 s）。
  curl -s -m 8 "$BOARD/api/campaign?op=stop" >/dev/null 2>&1
  sleep 1
  PW_START="$BOARD/api/campaign?op=start&vd=$PREWARM_VD&td=$TD&vr=$VR&tr=$TR&batch=0"
  PW_START="$PW_START&pw=$PW&gap=$GAP&pulses=$PULSES&wait=$WAIT&step=$STEP&minv=$PREWARM_VD&maxv=$PREWARM_VD"
  PW_START="$PW_START&delays=$DELAYS&max=1&swin=$SWIN&revert=$REVERT"
  echo -n "     预热: "; curl -s -m 20 "$PW_START"; echo
  PW_TICKS=0
  # 等它把判决交出去并 park（代理读完后板子才继续；上限 PREWARM_WAIT）。不打完就 stop 会让这一发
  # 停在 awaiting_verdict，等于把"我们唯一一次可能命中的发"判给超时 —— 那正是禁止的做法。
  for _ in $(seq 1 "$PREWARM_WAIT"); do
    PW_TICKS=$((PW_TICKS + 1))
    RUN=$(curl -s -m 6 "$BOARD/api/status" | python3 -c \
          'import json,sys;print(int(json.load(sys.stdin).get("campaign",{}).get("running",False)))' \
          2>/dev/null || echo 1)
    [ "$RUN" = "0" ] && break
    sleep 1
  done
  kill -9 "$PW_AGENT" 2>/dev/null
  curl -s -m 8 "$BOARD/api/campaign?op=stop" >/dev/null 2>&1
  TS "预热战役已 park（轮询 ${PW_TICKS} 次后 running=0）"
  # 预热发的判决 = 开正式批的闸门。四种非 under 结果都要人来看：
  #   goldilocks/hit  它可能已经把窗口弄开了 → 立刻去 window.sh，别再打一发把它盖掉
  #   over            欠压横跨启动窗 = 档案定义的 poison 形状 → 这一发的账必须先进台账
  #   lost            卡掉链：正式批 60 发打在看不见的卡上
  #   没有任何判决行  板子根本没打出去（总线/锁/代理问题），也不能当"预热过了"继续
  PWV=$(grep -aE -- ' attempt [0-9]+ ' "$DIR/agent-prewarm.log" 2>/dev/null | tail -1 \
        | sed -n 's/.* -> \([a-z_]*\).*/\1/p')
  echo "     预热判决: ${PWV:-无}"
  TS "预热段结束（判决到手，闸门放行才继续开正式批）"
  grep -E -- '^(agent:|session|.*-> )' "$DIR/agent-prewarm.log" 2>/dev/null | tail -3 | sed 's/^/       /'
  case "$PWV" in
    under) echo "     [ok] under：与 11-21 命中批同形的'没反应'，可以开正式批";;
    *) echo "[!!] 预热发不是 under（判决='${PWV:-无判决}'）：正式批不发。"
       echo "     这一发打的就是本批坐标，任何'继续'都是在拿唯一一次命中换一批 under。"
       echo "     先看 $DIR/agent-prewarm.log 与 $DIR/board.log；goldilocks/hit 立刻走 bench/window.sh，"
       echo "     over 先按 poison 处理（bench/poison_check.sh），lost 先断电 ≥30 s 再重新探针。"
       curl -s -m 15 "$BOARD/api/log" > "$DIR/board.log" 2>/dev/null
       exit 3;;
  esac
  # 只读 config 的闸门。**这里绝不按 PEX_RST**：卡如果还在总线上却"看起来没了"，按一下是把熔丝链
  # 重放一遍——万一预热发就是那一发命中，按下去就把它毁掉（写窗口的 arm 只有一个，见 README §1.5）。
  if [ -e "$CFG" ]; then
    VDP=$(python3 -c '
import os, struct, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
v = struct.unpack("<I", os.pread(fd, 4, 0))[0]
os.close(fd)
print("0x%08x" % v)' "$CFG" 2>/dev/null)
  else
    VDP=0xffffffff
  fi
  echo "     预热后 config=0x${VDP#0x}（campaign running=$RUN）"
  if [ "$VDP" = "0xffffffff" ] || [ -z "$VDP" ]; then
    echo "[!!] 预热发把卡打离了总线：正式批不发 —— 60 发打在一张看不见的卡上是 60 发白烧。"
    echo "     这是预热发在替整批挡事故。请断电 ≥30 s（bash bench/host-down.sh → 等操作员上电），"
    echo "     上来先 temp/r_probe1.sh 的等价探针（MAX=1）确认可判，再重开这批。"
    exit 3
  fi
else
  echo "     跳过（PREWARM=0）：本批第一发就是拔电后的第一发，按上面的死亡分布它最危险"
fi

echo "== 3. 判决代理 =="
pkill -9 -f 'ga100vfi_age[nt]' 2>/dev/null
systemctl stop ga100vfi-agent 2>/dev/null
# 读时机/盖章次数/每发 BAR0 会话预算全部来自 shot_policy（做实验就在操作脚本里显式 export 覆盖，
# 这样它在台账里是看得见的自变量）。这里只留一条历史记录：2 s 时本卡同一族 55 发里有 31 发把 CRC
# 读成 0x57/0x75（基线 0x55），按包络的说法那叫"读得太早"，那些 under 要打折。
python3 -m ga100vfi_agent "$BOARD" --bdf "$BDF" --first-read-delay "${FIRST_READ_DELAY:-$(SP first_read_delay_s)}" \
    --confirm-reads "$CONFIRM_READS" --stable-s "$STABLE_S" \
    --max-bar0-sessions "$BAR0_SESSIONS" > "$DIR/agent.log" 2>&1 &
AGENT=$!
TS "正式批判决代理已起（pid $AGENT）；下面进发前闸门与 op=start"
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
# 顶替：BATCH=1 时每一发都换一档电压，最后一条判决属于上一发。2026-11-20 那一轮就这样
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
# 【实发 vs 判决】必须对得上：板子说打了 N 发而台账只有 M 行时，多出来的那些发每一发都真打出去了，
# 只是没人判（代理被 presence 看门狗收走、或批末被 crack.sh 杀掉）。不补这一行，"实发数"这一列本身
# 就是假的 —— 2026-11-26 一批 tries=7、判决 6，`顺序` 里只有 6 行，那少的 1 发在台账里不存在。
board_tries = 0
try:
    import json
    with open(os.path.join(os.path.dirname(log), "final.json")) as fj:
        c0 = json.load(fj).get("campaign", {})
    board_tries = int(c0.get("tries") or 0)
    fd, fv = c0.get("delay"), c0.get("v_dip")
except (OSError, ValueError):
    fd = fv = None
if rows and board_tries > len(rows) and fd is not None and fv is not None:
    for k in range(len(rows) + 1, board_tries + 1):
        rows.append((str(k), str(fd), str(fv), "nojudge", "", ""))
        try:
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"session cleanup attempt {k} delay={fd} vd={fv} "
                        f"读数=代理停战后无人判 -> nojudge\n")
        except OSError:
            pass
    print(f"   补记 {board_tries - len(rows)} 发为 nojudge：板子 tries={board_tries} 而判决只到 "
          f"{len(rows)} 发，少掉的每一发坐标都是 {fd}/{fv}（重打就从这里接着打）")
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
print("   delay    vd   合计  under  over    hit  lost  nojdg  poison  gold   备注")
for (d, v) in sorted(tab):
    c = tab[(d, v)]
    n = sum(c.values())
    note = ""
    if c.get("poison") or c.get("over"):
        note = "<- 这附近就是边界（over=跌太深=poison）"
    if c.get("hit") or c.get("goldilocks"):
        note = "<== 有命中迹象，下一轮把这里加密采样"
    # gold 那一列以前没有：命中迹象的备注是按 goldilocks 判的，但表里看不到它，于是
    # 「合计比各列之和多 1 + 一句有命中迹象」根本读不出是哪一发命中的 —— 2026-11-26 我就是这样
    # 把【归档里 delay=64/vd=372 那一发】错认成了 (84,380)，然后拿一整天下沿去钉那个坐标。
    print(f"   {d:>5}  {v:>4}  {n:>4}  {c.get('under',0):>5}  {c.get('over',0):>5}"
          f"  {c.get('hit',0):>4}  {c.get('lost',0):>4}  {c.get('nojudge',0):>5}  {c.get('poison',0):>5}"
          f"  {c.get('goldilocks',0):>4}  {note}")
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
# 不带 mode：0.4.1 起板子没有判决模式可言（板载 SMBPBI 判决已删除，本台架读不到那个
# 端点）。0.4.0 及更早的固件把这个参数当必填，所以只有 EXPECT_FW 闸门保证我们面对的
# 是哪一版固件之后，才允许改这条 URL —— 版本闸门在这里的作用就是让这类改动不靠记性。
# 这里走 op=start 直接开跑，而不是页面的 /api/req：本脚本自己就是这一批的发起方，
# 它拉起来的 agent 用 HTTP 交判决，两边同时在线，没有"没人领取"的可能。/api/req 是给
# 人的：人在页面上点，得由一个已经在线的判决方当场接手（0.5.0，见 PORT_NOTES）。
START="$BOARD/api/campaign?op=start&vd=$VD&td=$TD&vr=$VR&tr=$TR&batch=$BATCH"
START="$START&pw=$PW&gap=$GAP&pulses=$PULSES&wait=$WAIT&step=$STEP&minv=$MINV&maxv=$MAXV"
START="$START&delays=$DELAYS&max=$MAX&swin=$SWIN&revert=$REVERT"
echo "     vd 起 $VD，范围 ${MINV}..${MAXV}，步长 $STEP，batch $BATCH，delays=$DELAYS，max=$MAX"
echo "     物理开关：swin=$SWIN us（0x8b 采样）revert=$REVERT（时间表内恢复）"
TS "战役 op=start 之前（发前闸门已过）"
echo -n "     设备回答: "; curl -s -m 20 "$START"; echo
T_START=$(date +%s)      # survivor 停手用（见下面盯梢循环里那段）

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
  # ===== survivor 停手（零读模式下唯一能"看见命中"的形状）=====
  # 今天每个没命中的电源周期里，卡最长也只活到 ~78 s（6 发那批），而且中途一定有掉链；命中的定义是
  # 【它自己的 boot 成功了】，那它会一直答话、不再掉。所以"批已跑超过 survivor_halt_s 而卡仍在答话"
  # 本身就是该去读的异常：立刻停战役，把批末那【一次】熔丝块读用在一张活着的卡上 —— 否则我们会一路
  # 把它打到死，而命中判据（window: OPEN）永远读不到。
  if [ -n "${T_START:-}" ] && [ $(( $(date +%s) - T_START )) -gt "$(SP survivor_halt_s)" ] \
     && [ "$(head -c 2 "$CFG" 2>/dev/null | od -An -tx1 | tr -d ' \n')" = "de10" ]; then
    echo "     [survivor] 战役已跑 $(( $(date +%s) - T_START ))s 而卡一直在答话：这不是今天的死亡形状。"
    # 【就地读，不等批末】2026-11-26 07:27:35 那批第一次出现这个形状：11 发全 under、卡活到最后一发
    # 之后 +90 s。板子在我们停手后 07:30:03 变 stopped，而批末那次 burn status 落在 07:30:3x —— 中间
    # 隔着盯梢循环退出、台账、包络门禁、survival 表，实测 ~30 s，卡就在这 30 s 里死了（读到 ff ff）。
    # 零读模式下熔丝状态【只有这一处】能读到，读晚了等于这批白打。顺序必须是先停战役再读：否则读
    # 的同时板子还在打下一发。
    curl -s -m 8 "$BOARD/api/campaign?op=stop" >/dev/null 2>&1
    echo "                就地读那唯一一次（命中判据 = window: OPEN），批末不再重复花这次 BAR0。"
    # 【一次不够】闸门判"在场"与真正读之间那 1~2 s，卡正好能掉（2026-11-26 07:35:08 那批：11 发活着，
    # 停手后第一次读撞上 07:37:32 的掉链，整批唯一一次读数读成一行 traceback，`window:` 没拿到）。
    # 掉链之后它自己每 ~14 s 会重试 boot 一次回到总线上，所以按谓词重试几次，每次都先验 config=10de。
    # 过滤也必须收紧：burn status 出错时 traceback 会把源码行 `"efp": self.rd(DSOV_STATUS)` 打出来，
    # 宽松的 egrep 'dsov' 会把它当成一条读数（上一批就是这么"读到了东西"却没有 window 行的）。
    : > "$DIR/window-at-halt.raw"
    for t in $(seq 1 "$(SP halt_read_tries)"); do
      [ "$(head -c 2 "$CFG" 2>/dev/null | od -An -tx1 | tr -d ' \n')" = "de10" ] || sleep "$(SP halt_read_gap_s)"
      ( cd "$GA_BASE/host" && timeout 25 python3 -m ga100vfi.burn --bdf "$BDF" status 2>&1 ) \
        > "$DIR/window-at-halt.raw"
      egrep '^[[:space:]]*(fusectrl|crc|dsov|goldilocks window)' "$DIR/window-at-halt.raw" \
        > "$DIR/window-at-halt.txt"
      grep -q 'window' "$DIR/window-at-halt.txt" && break
      sleep "$(SP halt_read_gap_s)"
    done
    if grep -q 'window' "$DIR/window-at-halt.txt" 2>/dev/null; then
      sed 's/^/        /' "$DIR/window-at-halt.txt"
    else
      echo "        [!!] 就地读了 $t 次仍没拿到窗口（卡那一刻不在总线上）。这批的窗口判据只能等下一周期"
      echo "             批前那次冷启动读；原始输出留在 $DIR/window-at-halt.raw"
    fi
  fi
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
# 时刻归因 + 坐标清点（0.5.5）。这条不是"锦上添花的报表"，是两条纪律的落地点：
#   (1) 引用"A 类时刻不确定"之前必须先给出按 act 分组的 want/got/dur —— 固件从 0.3.x 就在免费记,
#           我们一次都没统计过，于是那句话被引用了十几次而没人知道抖的是哪一步；
#   (2) tries=40 不等于覆盖了 40 个坐标。VOUT_COMMAND 的 LSB 是 6.25 mV，300..380 只有 14 个坐标,
#           11-21 那条"2 mV 阶梯"三发是同一发。重复本身不是错（锤击是有意），但台账必须分得开。
echo "== 本批归因（host/ga100vfi/timeline.py）=="
(cd "$HOST" && python3 -m ga100vfi.timeline "$DIR") \
  || echo "   （归因没出来：board.log/agent.log 缺失，别把空表读成『没有抖动』）"
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
