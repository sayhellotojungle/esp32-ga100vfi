#!/usr/bin/env bash
# raw row 18（RIR sense）当相位刻度：把"这一批没反应"拆成「什么都没打到」与「打早了」。
#
#   bash bench/early.sh baseline            # 每个电源周期一次：趁卡健康抓 FF 行，存成本卡基线
#   bash bench/early.sh compare <运行目录>   # 批末再抓一次，与基线比，判 nojudge/nothing/early/partial
#
# 为什么值得为它开 BAR0（本台架唯一造成过整机死机的通路，红线 1）：熔丝链的读取顺序里 RIR sense
# 排在其它 sense **之前**，所以"控制行没翻（DSOV 仍 1）但 row 18 变了"不是噪声，而是**相位偏早**
# 的确证，方向还是给的（φ 往晚移一格）。只看 DSOV/CRC 时，这一发被读成 nothing，于是"这一带打了
# 240 发 0 反应"里可能混着一堆"其实碰到了，只是碰早了"。
#
# 两条纪律：
#   * **每电源周期一次，不是每发一次**。每发一次会把 60 发变成 120 次 BAR0 会话，那个量级我否了；
#     crack.sh 的发前路径因此一次 BAR0 都不开（11-21 两次"板子一发没打、判决机却挂了"就是这么来的）。
#   * 比的是**本卡自己的**基线：RIR 内容随 SKU 与个体不同，别人的 row 18 没有参考价值。所以基线
#     文件名带卡的指纹（vendor:device + FBP 身份），换卡不会拿别人的行当参照。
set -uo pipefail
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
HOST=$GA_BASE/host
BDF=${BDF:-0000:01:00.0}
STATE=${EARLY_STATE:-$GA_BASE/state}
CFG=/sys/bus/pci/devices/$BDF/config
MODE=${1:-}
ARG=${2:-}

card_key() {          # 卡身份：vendor:device + BAR0 的 OPT_ENABLE_FUSE_PROGRAM 区一位数，够用且只读
  local id fp
  id=$(head -c 4 "$CFG" 2>/dev/null | od -An -tx1 | tr -d ' \n')
  [ -n "$id" ] || return 1
  fp=$(cd "$HOST" && python3 -m ga100vfi.burn --bdf "$BDF" status 2>/dev/null \
       | awk '/^sbfe|^efp|^plm_fuse/ {printf "%s", $2}')
  echo "${id}-${fp:-nofp}"
}

baseline_file() { echo "$STATE/ff-baseline-$1.json"; }

# 红线 1：FUSECTRL 不在 IDLE 时一个 BAR0 都不开。这里复用 burn status 的那一读（它自己会拒绝并
# 报错），但先做一次 config 头 4 字节的在场判断：全 1 的卡连 status 都不该问。
present() { local h; h=$(head -c 2 "$CFG" 2>/dev/null | od -An -tx1 | tr -d ' \n'); [ "$h" = "de10" ]; }

case "$MODE" in
  baseline)
    present || { echo "early: 卡不在场（config 读不到 10de）：这次基线不抓，空手总比抓错强"; exit 3; }
    key=$(card_key) || { echo "early: 读不到卡身份，基线不抓"; exit 3; }
    mkdir -p "$STATE"
    out=$(baseline_file "$key")
    echo "$out" > "$STATE/current"        # 指针：批末卡可能已经死了，那时算不出身份（见 compare）
    # 临时名必须以 .json 结尾：burn.py capture 是按后缀决定写 JSON 还是 rows.txt 的，
    # 写成 .json.tmp 会静默落成 rows.txt，然后读的一方 json.load 报"Extra data"——
    # 症状看起来像"卡不配合"，实际是我们自己把文件名起错了。
    (cd "$HOST" && python3 -m ga100vfi.burn --bdf "$BDF" capture -o "$out.tmp.json") \
      || { rm -f "$out.tmp.json"; echo "early: capture 失败（BAR0 没开成？）"; exit 3; }
    mv -f "$out.tmp.json" "$out"
    date +%s > "$STATE/last-capture"       # 发前闸门的依据：crack.sh 拿它算"距上次全量 capture 多久"
    echo "early: 基线已存 $out（记入 $STATE/last-capture，${CAPTURE_MIN_GAP_NOTE:-发前闸门会用它挡开火前的近距离读}）"
    # 用 early.py 的 load() 报，不在这里再手写一遍解析：capture 的真实形状是
    # {"ff_rows": {...}}，手写那份读不到键就会安静地报 "row 18 = None"，
    # 而 "None" 听起来像"这张卡没有 RIR"，实际是我们键名读错。
    (cd "$HOST" && python3 - "$out" <<'PY'
import sys
from ga100vfi import early
rows = early.load(sys.argv[1])
watch = early.EARLY_ROW
if watch in rows:
    print("   row %d = 0x%08x  （%d 行；这是**本卡**的 RIR，别人的数字没有可比性）"
          % (watch, rows[watch], len(rows)))
else:
    print("   row %d 不在这份 capture 里（读到 0 或区外）：early 档对这张卡没有凭证，"
          "以后每批都会报 nojudge —— 别把 nojudge 读成 nothing" % watch)
PY
    )
    ;;
  compare)
    [ -n "$ARG" ] || { echo "用法：bench/early.sh compare <运行目录>"; exit 2; }
    # 基线路径**优先按指针**（baseline 那一步写下的 $STATE/current），其次才按当场算出的身份。
    # 反过来的话就有这个荒谬结果：卡死了 → 身份算不出来 → key=unknown → "没有基线"，而基线其实
    # 就躺在 state/ 里；症状读起来像"这个电源周期忘了抓基线"，实际是"卡已经不在 PCIe 上了"。
    # 两条都是 nojudge，但下一步完全不同：前者要补基线，后者是那一发的现场结论。
    ptr="$STATE/current"
    if [ -f "$ptr" ]; then
      pre=$(head -1 "$ptr")
      why="按 $ptr 指的基线"
    else
      key=$(card_key 2>/dev/null) || key="unknown"
      pre=$(baseline_file "$key")
      why="没有基线指针，按当场身份算出的路径"
    fi
    present || { echo "early 档判读：nojudge —— 批末卡已离链，post 一次都没读到（这本身是信息：那批最后一发之后卡死了）"
                 echo "   基线本来是有的：$pre（$why）；这一档缺的是批末那一次读取，不是基线"
                 exit 3; }
    if [ ! -f "$pre" ]; then
      echo "early 档判读：nojudge —— 本电源周期没有本卡基线（$pre 不存在，$why）"
      echo "   处置：卡健康时跑一次 \`bash bench/early.sh baseline\`。在那之前，这批的\"没反应\""
      echo "   只是\"没凭证\"，不是\"什么都没打到\"——别拿它当加深或换坐标的凭证"
      exit 3
    fi
    post="$ARG/ff-post.json"
    (cd "$HOST" && python3 -m ga100vfi.burn --bdf "$BDF" capture -o "$post.tmp.json") \
      && mv -f "$post.tmp.json" "$post" || { rm -f "$post.tmp.json"; echo "early 档判读：nojudge —— 批末 capture 失败"; exit 3; }
    (cd "$HOST" && python3 -m ga100vfi.early "$pre" "$post") | tee "$ARG/early.txt"
    ;;
  *)
    echo "用法：bash bench/early.sh baseline | bash bench/early.sh compare <运行目录>"; exit 2;;
esac
