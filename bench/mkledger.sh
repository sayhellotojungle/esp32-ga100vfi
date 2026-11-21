#!/bin/bash
# (Re)build the cross-run ledger from every campaign run's agent.log, so the scarce
# shots each power cycle produces accumulate into one (delay, vd) -> verdict table.
set -uo pipefail
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
LOGS=$GA_BASE/logs
LEDGER=$LOGS/ledger.txt
# 目录不存在就明说，别留一个 FileNotFoundError 让人以为是脚本坏了。
if [ ! -d "$LOGS" ]; then
  echo "没有日志目录 $LOGS：台账来自 crack.sh 的落盘，先跑过战役再汇总。"
  echo "（部署目录不是默认值时，用 GA_BASE=<部署目录> bash $0）"
  exit 1
fi
: > "$LEDGER.tmp"
# 汇总块的非零退出必须传出去：静默产出一份空台账，比报错退出危险得多（下一轮会照着
# 空表决定往哪个电压段补发）。
python3 - "$LOGS" "$LEDGER.tmp" <<'PY' || { echo "[!!] 台账生成失败，$LEDGER 保持原样"; rm -f "$LEDGER.tmp"; exit 1; }
import glob, json, os, re, sys
logs, out = sys.argv[1], sys.argv[2]
pat = re.compile(r"attempt (\d+) delay=(-?\d+) vd=(-?\d+).* -> (\w+)")
rows = []
# 目录名规则是 crack.sh 定的：<ts>-crack-<固件版本>-<pulses x pw>_<vd>。这里曾经是写死的
# "*-crack2f385b0"，固件版本一换就悄悄汇总不到任何东西 —— 台账空掉的危险性在于它看起来
# 像"还没有数据"。所以现在既放宽模式，也在零命中时明确报错。
# 目录前缀不能只认 crack：台架上还有 lostwatch.py（零干预观察 `lost` 到底是不是自愈）。
# 只 glob "*-crack*" 会把这类轮次静默丢掉，而"台账里没有"和"这一发没打"看起来一模一样。
runs = sorted(glob.glob(os.path.join(logs, "*-crack*", "agent.log"))
              + glob.glob(os.path.join(logs, "*-lostwatch*", "agent.log")))
if not runs:
    print(f"  [!!] {logs} 下没有任何 *-crack*/*-lostwatch*/agent.log。要么真的还没打过硬仗，"
          f"要么台架脚本的台账目录命名改了而这里没跟上 —— 后者必须修，不能让台账静默为空。")
    raise SystemExit(1)
for log in runs:
    run = os.path.basename(os.path.dirname(log))
    body = open(log, errors="replace").read()
    found = pat.findall(body)
    for a, d, v, r in found:
        rows.append((run, d, v, r))
    # A poisoned attempt never gets a verdict fed (red line 1), but the coordinates are
    # known, and "this parameter set can trip poison" is key data.
    #
    # 坐标取自板子（该轮 final.json 的 campaign.delay / v_dip），不是"最后一条判决"的坐标：
    # BATCH=1 时每发换一档电压，最后一条判决属于上一发。2026-11-20 第二张卡两条台账都记成
    # 339 poison，而板子停在 v_dip=334 —— 差的那一个 PMBus 码正是我们用来夹边界的那个数。
    # 与 bench/crack.sh 的补行逻辑同源，两处必须一起改，否则重建台账会把错坐标写回来。
    if "POISON" in body and found:
        d, v = found[-1][1], found[-1][2]
        fj = os.path.join(os.path.dirname(log), "final.json")
        try:
            c = json.load(open(fj, encoding="utf-8")).get("campaign", {})
            if c.get("delay") is not None and c.get("v_dip") is not None:
                d, v = str(c["delay"]), str(c["v_dip"])
            else:
                print(f"  [!!] {run}: final.json 无 delay/v_dip，poison 坐标退回最后一条判决")
        except (OSError, ValueError) as exc:
            print(f"  [!!] {run}: final.json 不可用（{type(exc).__name__}），"
                  f"poison 坐标退回最后一条判决 —— 该轮的 (delay, vd) 要人工核对")
        # 去重比较的是 4 元组 (run, d, v, "poison") 而左边只有 3 项 —— 永远不相等，于是
        # 每条 agent.log 里的 `-> poison` 都会被再补记一遍（docs/UNLOCK_AND_POISON §2.4.1
        # 记的那条台账翻倍就是这个）。补行的用途是"判决从没产生过"的发数，已经有就别补。
        if not (rows and rows[-1][:3] == (run, d, v) and rows[-1][3] == "poison"):
            rows.append((run, d, v, "poison"))
    if "lost_streak" in body or "停手" in body:
        print(f"  [note] {run}: 出现过连续 lost（卡不在总线上），这些发数不算数据")
with open(out, "w") as f:
    for run, d, v, r in rows:
        f.write(f"{run}\t{d}\t{v}\t{r}\n")
print(f"  写入 {len(rows)} 发")
PY
mv "$LEDGER.tmp" "$LEDGER"
echo "== $LEDGER =="
cat "$LEDGER"
echo
echo "== 合并后的窗口表 =="
python3 - "$LEDGER" <<'PY'
import collections, sys
tab = collections.defaultdict(collections.Counter)
for line in open(sys.argv[1]):
    p = line.rstrip("\n").split("\t")
    if len(p) < 4:
        continue
    tab[(int(p[1]), int(p[2]))][p[3]] += 1
if not tab:
    print("  （还没有真实判决数据）"); raise SystemExit
print("   delay    vd   合计  under  over  poison  hit  gold  lost  dead  void  nojudg  unk   判读")
for (d, v) in sorted(tab):
    c = tab[(d, v)]
    n = sum(c.values())
    bad = c.get("over", 0) + c.get("poison", 0)
    note = ""
    if c.get("goldilocks"):
        # 2026-11-21 本台账第一次出现这个判决（delay=64 vd=372）。它和 `hit` 不是一回事：
        # `DSOV` 离开基线只是"动到了"，`goldilocks` 是 `DSOV==0 且 CRC==0x55` —— 熔丝块在
        # 可写窗口里且链自校验通过，也就是**可以就地烧**。所以判读必须是"停手冻结这一族"，
        # 而不是"在这里加密"：继续往这一族补发只会把刚打开的窗打回去。
        note = "<== 命中窗口（DSOV=0 且 CRC=0x55）：停手、冻结这一族、进 bench/burn.sh"
    elif c.get("hit"):
        note = "<== 有命中迹象，下一轮在这里加密"
    elif bad and c.get("under"):
        note = "<- under 与 over 混现 = 边界就在这个 vd 附近"
    elif bad:
        note = "<- 全是 over/poison：太深，往浅调（vd 调高）"
    elif c.get("under"):
        note = "全是 under：还不够深"
    if c.get("dead") and "命中" not in note:
        # dead = 我们**没有**再按 PERST#，卡整窗（60 s / 240 次只读）都不在总线上。2026-11-21
        # 实测：同一坐标同一命令，三发里 1 发 dead、2 发从头到尾没掉过总线 —— 所以 dead 主要是
        # "毛刺时间表自己拉过一次 PERST# 之后链路重训练没成"的彩票（本卡上游桥只有 x8/Gen1），
        # 不能当"太深"的读数拿去推 vd。它确定的只有一件事：这一发读不到判决，而我们把它救回来
        # 的那一下会重放熔丝链，可能刚打出来的 arm 一起冲掉 —— 等于白烧一发。
        note = (note + " ｜" if note else "<- ") + \
               f"dead {c['dead']} 发：掉链是链路彩票不是深度证据，且这些发读不到判决"
    print(f"   {d:>5}  {v:>4}  {n:>4}  {c.get('under',0):>5}  {c.get('over',0):>5}"
          f"  {c.get('poison',0):>6}  {c.get('hit',0):>3}  {c.get('goldilocks',0):>5}"
          f"  {c.get('lost',0):>4}  {c.get('dead',0):>4}  {c.get('void',0):>4}"
          f"  {c.get('nojudge',0):>6}  {c.get('unknown',0):>3}   {note}")
# "台账里没有"与"这一发没打"长得一模一样，所以本表必须声明自己看的只是 agent.log 这一个来源。
# 真值在 crack.sh 追加的 verdicts.tsv 里（包络门禁读的也是它）；两者不符时以真值为准，
# 缺的行用 bench/backfill_verdicts.sh 按运行目录回填，不要人肉抄。
import os
tv = os.path.normpath(os.path.join(os.path.dirname(sys.argv[1]),
                                   "..", "..", "logs", "verdicts.tsv"))
if os.path.exists(tv):
    with open(tv, errors="replace") as f:
        n = sum(1 for line in f if "-> " in line)
    print(f"   真值台账 {tv}：{n} 发。本表来源仅 agent.log；不符时以真值为准，"
          f"缺的行用 bench/backfill_verdicts.sh 回填。")
else:
    print(f"   [!!] 找不到真值台账 {tv}：本表无法自证完整，包络门禁也会因为缺真值而保守。")
PY
