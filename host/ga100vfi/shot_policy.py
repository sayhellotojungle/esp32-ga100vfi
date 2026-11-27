"""每发前后的操作规程 —— 唯一出处（single source of truth）。

为什么要有这个文件：发数打到几百发之后，「每发前后该做什么、不该做什么」仍然散在五个地方 ——
`bench/crack.sh` 的 env 默认值、判决机上那层入口脚本里写死的 `CARD_SETTLE_S=25`、代理 argparse 的
默认值、板子固件里的默认值、以及每次开火前临时想起来 export 的几个数。散着的后果已经付过学费：

  * `CARD_SETTLE_S` 在仓库里改成 2、被判决机上不在版本库里的入口层写死成 25 盖掉，症状是「我明明
    改了」（2026-11-26）。
  * `first_read_delay` 代理默认 2.0、crack.sh 默认 4：同一件事两个数，谁生效取决于谁被调用。
  * 批前那次 487 行 capture 是随手加进开火脚本的，它离第一发只有 30 s —— 而「熔丝块的 BAR0 读会
    干扰启动」是我们自己写下的红线 1。**没有一份规程，这种改动就能混进来。**

规则：**这里列出的数，别处不许写字面量。** `bench/crack.sh` 与 `ga100vfi_agent.py` 从这里取默认值；
`tools/deploy_bench.py` 部署时跑 `--check-scripts`；操作脚本要改数就显式 export 覆盖（做实验就该
看得见自己在改什么）。这里同时放**规程本身**（BEFORE/AT/AFTER 的 must/never 清单），因为「不该做
什么」和数一样容易飘。
"""
from __future__ import annotations

import sys

# status 的取值：
#   settled    = 有证据支撑、当前在用；改动要在决策链里给理由
#   under-test = 正被单轴实验检验，值可能正处在实验组（用它的人要知道这回事）
#   floor      = 下限/保护量，调小需要证据
KNOBS: dict = {
    "card_settle_s": dict(
        value=2, unit="s", status="settled",
        why="卡回到总线之后、闸门之前的静置。原来 25 s 是个没测过的余量。冷态第一发由预热发吸收、"
            "冷偏由固件 stretch_us 计量 —— 能测的东西不该用固定睡眠盖；而一个电源周期只买得到 2 发，"
            "25 s/周期是纯税（操作员 2026-11-26 指令）。",
    ),
    "wait_card_s": dict(
        value=90, unit="s", status="settled",
        why="等卡出现在 PCIe 上的总预算（谓词=config 应答 10de，不是睡眠）。",
    ),
    "first_read_delay_s": dict(
        value=4, unit="s", status="under-test",
        why="一发之后到【第一次碰卡（开 BAR0，或 config-only 判决下的那一次在场探测）】之间的静默下限。"
            "红线 1：卡在自家 ROM/devinit 里时读熔丝块是本台上唯一挂死过判决机的通路。"
            "2026-11-26 三个同轴点：碰在 +4 s → 死于 +10~13 s；碰在 +20 s → 死于 +25~30 s；"
            "【一次都不碰（bar0_sessions_per_shot=0）】→ 活过 +57.8 s。存活时间跟着我们碰它的时刻走，"
            "不跟着那发坑走。所以现在这个数的身份是【每发间隔/吞吐】而不是安全余量：4 s 是既不许为 0"
            "（代理那条校验有测试）也不浪费周期的值。",
    ),
    "confirm_reads": dict(
        value=2, unit="次", status="under-test",
        why="同一发内对熔丝块的 BAR0 会话数（第 2 次用于脏读盖章：隔 stable_s 复读，两次一致才算 "
            "stable=yes）。读得多=判决可靠，但每次读都是一次干扰卡自己启动的机会 —— 它与 "
            "first_read_delay 属于同一个实验的两个旋钮。",
    ),
    "stable_s": dict(
        value=1.0, unit="s", status="settled",
        why="盖章复读之间的间隔。太短会把【我们读得太早】读成真反应。",
    ),
    "bar0_sessions_per_shot": dict(
        value=2, unit="次", status="under-test",
        why="一发允许的 BAR0 熔丝块会话上限，必须 >= confirm_reads：少一次就永远盖不了章。用光之后"
            "必须把这一发判成未盖章，而不是继续读 —— 2026-11-20 实测：lost 那一发的等待段里追加读会把"
            "卡打进 poison（那 28 s 里对一块正在离线的卡开了 15 次会话）。"
            "**默认必须是 2（1 次读 + 1 次盖章复读）**：零读（=0）与单发（=1）都是【阶段设定】，只能由 "
            "MODE 显式导出；把它当全局默认时，任何人工直调都静默产出未盖章的 under，而代理自己的参数"
            "校验还会把默认值本身拒掉（2026-11-26 让 9 条代理测试同时变红的就是这个）。"
            "零读的理由仍然成立：对照批实测死亡时刻跟着我们开 BAR0 的时刻走（读在 +20 s → 死于 "
            "+25~30 s；读在 +4 s → 死于 +10~13 s），所以死磕阶段一发都不开 BAR0，命中判据交给批末那"
            "【一个电源周期一次】的 burn status。",
    ),
    "prewarm_vd_mv": dict(
        value=384, unit="mV", status="under-test",
        why="【预热发的坐标，与战役起点分开】。预热发的职责是吸收【冷态第一发】这个危险状态（实测冷偏"
            "以 stretch_us 计量、且第一个周期最容易首发即死），它不是来替阶梯试深的。以前它打的就是 VD"
            "（战役起点），于是 08:54:22 那批把起点抬到 363 ⇒ 预热首发把卡打死 ⇒ 闸门按规矩拒开正式批 ⇒ "
            "整个电源周期 0 判决。分开之后：预热永远打在【这张卡从没丢过的格子】上，战役起点才允许压在"
            "边界格上 —— 混用的代价实测过两次（08:54:22 与 09:20:05：预热首发打死卡 ⇒ 闸门拒开批 ⇒ 0 判决）。"
            "现在的 384：delay=84 与 delay=54 下都是稳定 under（CRC=0x55），比实测致死格至少隔两格（下限）。"
            "战役格随阶段换（现 372/delay=64），预热格只跟 delay 走 —— 换 delay 时若没有该 delay 下的安全格，"
            "先打一发探它，别让预热替战役试深。",
    ),
    "presence_grace_s": dict(
        value=3.0, unit="s", status="settled",
        why="卡连续不在 PCIe 上多久就停战。每发板子自己拉 PERST#，那 ~1 s 里卡【合法地】不在总线上，"
            "采到就停会把每一批掐死；而【打完就不回来】是持续性的。",
    ),
    "max_lost": dict(
        value=1, unit="发", status="floor",
        why="连续几发没人判得出来就停火。判据的形状是（docs/UNLOCK_AND_POISON.md §四）【lost 之后补一发 → poison】，而 poison "
            "是这台上唯一不可逆的损失。调大它需要比【省发数】更强的理由。",
    ),
    "capture_min_gap_s": dict(
        value=120, unit="s", status="settled",
        why="【距开火这么近之内不许做全量 FF capture】（487 行 = 487 次熔丝块 CMD_READ）。这条是"
            "2026-11-26 我自己踩出来的：把 early 基线塞进开火脚本之后，每个电源周期都变成第 2 发死，"
            "而它离第一发只有 30 s。基线要在卡健康时抓，抓完隔一会儿再打，或干脆放在另一个周期。",
    ),
    "prewarm": dict(
        value=1, unit="bool", status="settled",
        why="拔电之后的第一发与后面每一发不是同一个实验对象，所以花一发把它买掉：独立代理判决、写 "
            "agent-prewarm.log、不进本批坐标清点，且【只有 under 才放行正式批】。",
    ),
    "prewarm_wait_s": dict(
        value=90, unit="s", status="settled",
        why="等预热发把判决交出去的上限（谓词=campaign 停）。不打完就 stop = 把唯一一次可能的命中判"
            "给超时。",
    ),
    "halt_read_tries": dict(
        value=3, unit="次", status="under-test",
        why="survivor 闸门停下来之后，那【一次】熔丝读数允许重试几次。实测：2026-11-26 07:35:08 那批 "
            "11 发全活着，闸门在卡还在场时判到停手条件，可第一次 burn status 正好撞上 07:37:32 开始的 "
            "7 s 掉链 ⇒ 整批唯一一次读数读成一行 traceback，`window:` 凭空丢掉。掉链之后卡自己每 ~14 s "
            "会重试 boot 一次回到总线上，所以按 config 谓词重试几次是买得起的（每次 ~1 s），而它换来的正是"
            "零读模式下唯一的命中判据。",
    ),
    "halt_read_gap_s": dict(
        value=4, unit="s", status="under-test",
        why="上面两次重试之间的等待。取 4 s 是因为它要【跨过】presence 的 2.5 s 采样粒度、又明显小于卡"
            "自己 ~14 s 的 boot 重试周期；真正的判据仍是每次都先验 config 应答 10de，这个数只是别把三次"
            "重试挤在同一瞬间打完。",
    ),
    "survivor_halt_s": dict(
        value=90, unit="s", status="under-test",
        why="【零读模式专用】一批开跑之后，卡【一直在答话】超过这么久就停战役并就地读那唯一一次窗口。"
            "依据：命中按定义是它自己的 boot 成功 —— 那它会持续在场；而 2026-11-26 白天每个没命中的周期"
            "里卡都在首发后 10~30 s 内死掉（那阵子每发 ~30 s，一批只买得到 2 发）。傍晚把每发压到 ~8 s"
            "之后出现新形状：07:27 与 07:35 两批各 11 发、卡活到最后一发后 +120 s —— 所以 90 s 已经不是"
            "'只有命中才做得到'的分界线，它现在的含义是【在卡还活着的时候把那次读做掉】，同时少打掉 3~4 发"
            "（继续打到死那次就读不到了）。若之后仍只见 11 发这种形状，就把这个数再往下压。",
    ),
    "verdict_budget_s": dict(
        value=100, unit="s", status="settled",
        why="代理对一发判决的总预算，到点判 lost。它必须大于 first_read_delay + 读 + stable_s，否则把 "
            "first_read_delay 调大就会把每一发都判成 lost（这就是【两个默认值互相咬】的形状）。",
    ),
    "settle_wait_hunt_ms": dict(
        value=1500, unit="ms", status="under-test",
        why="【hunt 阶段专用】板子打完一发之后、向主机要判决之前的静置（op=start 的 wait=）。判决代理的"
            "第一次读排在【settle + first_read_delay_hunt_s】之后，所以这个数直接决定我们离死亡有多近："
            "默认 4000 + 2 = 6 s，而 09:00:37 那批实测第 2 发（vd=363）之后卡【+6.7 s 起持续不可达】⇒ "
            "每一发深格子的判决都是【我们到点时卡刚走】：08:48:51 判到 363 under，09:00:37 同一格判成 "
            "lost，差的就是这 1 s 的赛跑。压到 1500 ⇒ 读排在发后 ~3.5 s。下限由红线决定：卡打完坑要自己"
            "拉 PERST# 重枚举（实测 ~2 s 才回总线），在它的 ROM/devinit 里读熔丝块是本台唯一挂死过判决机"
            "的通路，所以总静默不得小于 3 s（测试钉住上下限）。死磕阶段仍用 settle_wait_ms。",
    ),
    "settle_wait_ms": dict(
        value=4000, unit="ms", status="under-test",
        why="板子打完一发之后、向主机要判决之前的静置（URL 里的 wait=）。它【不是波形】：坑的形状由 "
            "pw/gap/td/pulses 决定，这个数只决定多久之后问主机。**它是现在唯一还压着吞吐的量**：零读模"
            "式下每发间隔 = settle + 探测点 + 判决往返 ≈ 8+4+1 = 13 s，而卡已经能活 ~76 s ⇒ 一个周期只买"
            "得到 6 发。压到 4000 ⇒ 每发 ~9 s ⇒ 同一个周期 8 发以上。档案那次命中的行里带的是 settle=8000"
            "，但那是在【每次都要读熔丝块】的前提下；现在发内一次都不读，短静置的代价只是把【卡还没回来】"
            "判成 nojudge，而那正是上面补记要显式记下来的东西。",
    ),
    "console_tries": dict(
        value=2, unit="轮", status="under-test",
        why="开火前第 2 道门（锁检查，以及每发的 ensure_unlocked）里 console.sh 的外层重轮数。预算公式写在 "
            "console.sh 的第一行输出里：tries × (wait+1) + 3。今天实测那两次锁检查往返（判前发现有锁 → "
            "CLEAR_FAULTS+密码 → 解后确认）吃掉 **38 s**，而这台卡多数周期只活 ~60 s —— 半个批次花在"
            "问一句『你锁了吗』上。3→2 保留一次重轮（ch341 会吃掉第一笔写，一轮不算数）。",
    ),
    "console_wait_s": dict(
        value=3, unit="s", status="under-test",
        why="上面每一轮的监听窗口。它是真正压着时间的那个数：监听窗口【无论有没有答复都会跑满】，所以一"
            "次成功调用的成本≈wait+1，而 wait=6 时两次调用就是 ~16~38 s。板子对 `cml=` 的答复是控制台"
            "状态行，3 s 足够；不够就重轮（上面 tries=2），而不是把每次成功都当 6 s 交出去。",
    ),
    "console_repeat": dict(
        value=3, unit="次", status="under-test",
        why="同一个监听窗口内把命令重发几遍（ch341 第一笔写会被吃掉，所以第一遍基本是白发的）。"
            "5→3：第 2、3 遍是真正的机会，再多就是在等一个不会来的答复。",
    ),
    "first_read_delay_hunt_s": dict(
        value=2, unit="s", status="under-test",
        why="【hunt 阶段专用】发后到第一次开 BAR0 的静默。为什么比 hammer 阶段的 4 s 更早：深的那几格"
            "卡【在判决之前就死了】—— 2026-11-26 08:18:34 那批 vd=362 首发即把卡打下线，我们的读排在 "
            "+4 s，落在死亡之后 ⇒ 整批 0 判决（08:05:33 的 vd=334 也是这个形状）；而 07:42 那批见过 "
            "+0.8 s 就死。找区间要的是【判决词】（under/over），章可以不要：历史上本卡在 2 s 读过 55 发，"
            "31 发 CRC 读脏（0x57/0x75，基线 0x55）但判决词照样出得来。再早就撞上红线（卡在自家 ROM/"
            "devinit 里读熔丝块是本台唯一挂死过判决机的通路），所以停在 2 s。",
    ),
    "post_shot_grace_s": dict(
        value=10, unit="s", status="under-test",
        why="【那一发还在飞时的在场宽限】看门狗的空闲档是 3 s，但它被实测咬出来说错了对象：08:23 与 "
            "08:27 两批的 agent.log 都是「卡连续 3.0s/3.3s 不在 PCIe 上 —— 不等判决，立刻停战」，而那"
            "正是 ladder 往深走的那第 3 发（vd=363）打出去之后 —— presence 记录器同一时刻看到卡"
            "+2~+12 s 仍在场（它在做正常的重枚举），代理却以 3 s 把这一发抛弃成 nojudge。区间下沿唯一"
            "可能给出 over 的那一发因此永远判不到。看门狗的职责是【别让板子往死卡上打下一发】，不是"
            "【抛弃已经打出去的那一发】：一发在飞时板子本来就不会打下一发（它在等判决），多等一会儿"
            "不花任何发数。上界取自卡的重启重试周期（~14 s 才回到总线 1 s）——10 s 仍能抓住真死；"
            "空闲档保持 3 s 不许放宽（05:44 那批的形状就是'卡没了板子还在发'）。",
    ),
    "presence_rate_s": dict(
        value=0.25, unit="s", status="settled",
        why="presence 记录器采一次 config 头的间隔（4 Hz）。它是唯一不依赖判决的存活证据 —— 今天"
            "所有【卡活到第几秒】的结论都来自这条 csv，而不是来自 agent.log。",
    ),
    "presence_span_s": dict(
        value=1500, unit="s", status="settled",
        why="记录器自己退出的上限：一个电源周期用不完，跑完就停，绝不留一个后台循环过夜（操作员红线）。",
    ),
    "batch_poll_s": dict(
        value=30, unit="s", status="settled",
        why="开火外壳看一次 /api/status 的间隔（只是进度打印，不参与判决）。",
    ),
    "batch_max_wait_s": dict(
        value=1350, unit="s", status="settled",
        why="外壳等一批结束的上限，到点就报【没等到】然后退出 —— 一次调用必须有终点，不许挂守护进程。",
    ),
    "fire_grace_s": dict(
        value=25, unit="s", status="settled",
        why="把战役起起来之后，第一次抓 manual-batch.log 关键行的延迟（闸门 + 预热代理起身的这段时间）。"
            "它只是【什么时候去看一眼】，不参与任何等待判定。",
    ),
}

# --------------------------------------------------------------------------- 冻结配方
# 11-21 唯一那次命中（e1=0xc2fa）的逐参数照抄。它必须只有一个出处：判决机上那层不在版本库里的
# run.sh 以前自带 `REV=${REV:-1} SWIN=${SWIN:-1200} MINV=${MINV:-366}`，只要调用方忘 export，
# 这批就**不是**那一发了（2026-11-26 检查时才发现；那几批恰好都显式传了值所以没被咬）。
# 现在开火脚本用 `shot_policy --recipe` 生成 export，配方从这里出，测试钉住它不许漂。
RECIPE: dict = {
    "PULSES": 2, "PW": 222, "GAP": 150,          # 波形：2 发 × 222 µs，间隔 150 µs（坑深靠 pw）
    "TD": -40, "VR": 750, "TR": -20,             # 时刻：出复位之前；vr/tr = 恢复写
    "VD": 372, "MINV": 372, "MAXV": 372, "STEP": 2,   # 物理上 = PMBus 码 60 = 375 mV（LSB 6.25 mV）
    "DELAYS": 64, "MAX": 60,                     # 单一 delay 点，自适应阶梯最多 60 发
    "SWIN": 0, "REVERT": 0,                      # 不采轨压；保住 0xE1 把 PWM-VID 交回去
    "BATCH": 1, "PREWARM": 1,
}
RECIPE_WHY = ("2026-11-21 与 2026-11-26 各命中一次的那一发：glitch vd=372 td=-40 vr=750 tr=-20 tv=0 "
              "fast=1 hold=0 pulses=2 pw=222 gap=150 delay=64 revert=0（docs/UNLOCK_AND_POISON.md §九）")

# --------------------------------------------------------------------------- 阶段
# 两个阶段用的是同一套波形，差的只是【搜索轴】与【判决要不要读寄存器】：
#
#   hunt   = 找区间。操作员 2026-11-26 明确要求"把这张卡当成一张新卡、忘记历史数据，按一般流程
#            重新找区间"，所以 vd 不再钉死在 372，改走 README §开火 那条标准粗阶梯（284..384 步长 25），
#            并且每发必须真读一次熔丝块 —— config-only 判决分不出 under 与 over，而没有 over 就
#            画不出区间的下沿。代价按今天实测：读一次就把死亡时刻往前挪（碰在 +4 s → 死于 +10~13 s）。
#   hammer = 死磕一个坐标。零 BAR0（config-only），靠数量堆同一个点，窗口判据留给每周期批前那次冷读。
#
# 它放在这里而不是放在调用方，是因为它一次只能有一个值、且必须打进台账：批报告回显的那一行
# `阶段(hunt|hammer)=…` 就是这一批的证据，改它要改代码，不能靠人记住自己当时 export 了什么。
MODE = "hunt"

# 实测【致死坐标】：这是阶梯起点的地板，写在代码里而不是靠人记。
# 2026-11-26 08:10:18 那批：vd=384 两发都判得出来（under，CRC 干净），板子按 under 往深走一格到
# 356 ⇒ 那一发【没判到】，卡 +19.2 s 就没了；更早一批（08:05:33）从带心 334 起步，首发即死、整批
# 0 判决。两个形状合起来就是这条规矩：阶梯起点必须在实测致死格的【上面】，否则一个电源周期换回 0 条
# 判决 —— 而"死在第几格"本身要有对照才知道它是杀死人的那一格。
MEASURED_LETHAL_MV = 356

SWEEP: dict = {
    # 【钉死归档里唯一那颗 goldilocks：delay=64 / vd=372】
    # 为什么不再钉 (84,380)：那一格今天打了 27 发，【DSOV 一次都没动过】（每批末"反应（DSOV 动过）：0
    # 次"）。同一坐标 + 同一波形 + 同一 delay 是【确定性事件】不是抽奖 —— 27 发零反应说明它不是命中格，
    # 再钉下去每周期只是把 24 发换成"更确定的不命中"。命中的唯一实时信号就是 DSOV 离开健康值 1。
    # 台账里真正带命中迹象的那一行是这个（verdicts.tsv 第 32 行，判决机 09:47 那次读出来的原文）：
    #     vd=372 pw=222 d=64 p=2 -> goldilocks FUSECTRL=0xe0040000 DSOV=0x00000000 CRC=0x00000055
    # DSOV 从健康值 1 变成 0 = 熔丝影子真的加载过，而它和档案里 2026-11-21 那颗 goldilocks 是同坐标
    # （host/tests/test_regressions.py 的 test_the_goldilocks_row_survives_the_stopped_campaign 写死了
    # "delay=64 vd=372 读到 DSOV=0 CRC=0x55"）。
    # 我先前认错过一格：跨轮总表的"有命中迹象"是按 goldilocks 判的，而表里【没有 goldilocks 那一列】，
    # 于是"合计比各列之和多 1 + 一句命中迹象"读不出是哪一发 —— 已给表补上 gold 列。它指向的是另一格，
    # 身份（哪天哪张卡）下一周期用 logs/ledger.txt 的 MM-DD 与 run 名认，认不认得出来都不改变上面那条。
    # 【11-26 下午，操作员证词 + 原文核对，推翻"确定性零反应"读法】本卡之前的 goldilocks 就是
    # vd=372/delay=64 打到的；原文（blog.kate.land，GA100VFI）写明命中是【每发概率事件】：作者用
    # 1 小时~48 小时的【同一配方 × 每发一个电源周期】拉到，且 die-specific。⇒ "同格 N 发零反应"
    # 不构成"该格空"的证据；正确打法就是钉住命中率最高的格反复买彩票。死在第 2 发 = 一个电周期
    # 买 1~2 张，与原文节奏一致（他 S3 循环 3 s，我们人肉断电）。操作员指令：就在 (372,64) 周围猛打。
    # 已知风险（写在这里而不是藏在心里）：致死边跟着 delay 走 —— delay=54 时下沿在 363..368，
    # delay=84 时抬到 375..380 ⇒ delay=64 的边大概 368..372，372 可能正好压在边上。首发即死也要打：
    # 那一发的判决词（DSOV 动不动）就是我们要的东西，而 lost 与 under 在这一点上是同一个答案。
    # 【11-26 10:09:47 实测，操作员定下一批】372@64 第 1 发判出 under（CRC 干净、DSOV 没动）、
    # 同坐标第 2 发 lost：+8 s 起持续 off-bus ⇒ d=64 的边已抬到压住 code 60（372/374/376 同码，
    # LSB=6.25mV，抬到 374 物理上就是再打同一格）。真抬一格 = 换码：退回 delay=54 下实测稳定的
    # code 59（366..370，12+ 发 under）即 VD=368。预热仍在 384（d=54 下 2 发 under，从没打死过卡）。
    # 【11-26 11:30:21 实测】(368,54) 21 发 20 under 干净读、卡活过整批 ⇒ code 59@d=54 确定性零反应，
    # 重复它不买命中。操作员定续探 delay 轴：DELAYS=64（归档命中的 delay）。64/368 历史 22 发全 under
    # 没死过，且 10:09:47 那批预热 384@64 判出放行 ⇒ d=64 下 384 仍是从没丢过的预热格。
    # 【11:54:47 验磨损批】code 59 第 3 批 21 发又零死亡零反应（本电周期 ~70 发）⇒ 磨损说证伪，
    # 死亡跟码走（code 60 次发必死，54/64 两个 delay 同形状）。11:59:01 又量 (368,44)：21 发零反应
    # 零死亡 ⇒ code 59 在 44/54/64 三个 delay 全死心。操作员定打 (380,54)=code 61：致死边已抬到
    # 60/61 之间，归档两次命中（11-21 的 372=code60；ledger 11-24 的 84/380=code61）都是贴边码，
    "VD": 372, "MINV": 372, "MAXV": 372, "STEP": 6,
    "DELAYS": 64, "MAX": 24, "BATCH": 1,               # 猛打模式：卡不死就往上打；死了走断电周期再来一张
    "BAR0_SESSIONS": 1, "CONFIRM_READS": 1,            # 每发一次会话、一次读：够分 under/over，不多花
}
SWEEP_WHY = ("找区间的标准粗阶梯来自 bench/README.md §开火（MINV=284 MAXV=384 STEP=25 DELAYS=54 "
             "BATCH=2 MAX=12），它故意跨过被历史标成 poison 的 354..358 与被称为健康区的 362..380 —— "
             "当成新卡就要重新量边界，而不是继承旧边界。已经量到的：384 两发 under（CRC 干净）、372 几十"
             "发全 under；362（08:18:34 首发）、356（08:10:18 第 3 发）、334（08:05:33 首发）全是【首发即"
             "死、而我们的读排在 +4 s 落在死亡之后】⇒ 0 判决。于是边界卡在 362..372 之间：这一轮打 368，"
             "并把带收窄到 344..384、步长收到一个码（6 mV）。起点规矩（MEASURED_LETHAL_MV + STEP）保证"
             "不再把整个周期押在已知判不出来的格子上；读时刻由 first_read_delay_hunt_s 管（4 s 太晚）。"
              "【BATCH=1 而不是 2】一个电源周期实际只买到 ~3 发：卡在第 3 发后 ~11 s 掉线，而 max_lost=1 "
              "一发 lost 即停火（红线：lost 之后补一发就是判据 §四 的 poison 形状 —— 卡还没烧，那是不可逆的）。"
              "BATCH=2 时那 3 发里有 2 发在重复 368，而 368/372/384 的 under 已有 10+ 发盖章在台账里 —— 重复"
              "它不产生边界信息。判到就走，才让每周期那几发都落在没量过的格子上。"
              "【BATCH=1 立刻见效】08:48:51 那批 3 发打了 3 个新坐标：368 under、【363 under】、356 lost。"
              "363 在此前四个周期里从未判到（全是 nojudge/lost，因为 BATCH=2 时它总排在第 3 发、卡那时已"
              "经被两发打过）⇒ 边界不在 363，往深还有一段。"
              "【363 到底判不判得出来：三种排队方式都试过了】① BATCH=2 时它排在第 3 发 ⇒ nojudge×3；"
              "② BATCH=1 时它排在第 2 发 ⇒ lost×3（08:48:51 那批判到 under 是唯一例外）；③ 09:20:05 那批"
              "把预热剥到 368、让它当【战役首发】⇒ 打出后 +0.4 s 就持续不在场，还是 lost。三种排队共 7 发"
              "只有 1 发判得出来 ⇒ 结论不是【我们读得太晚】，而是 ≤363 这一族就是杀死卡的，而杀死卡的那一发"
              "我们永远拿不到判决词。区间的下沿因此量在 **363..368**：368 及以上稳定判得出（全 "
              "under，CRC 干净），363 及以下判不出。"
              "【为什么换 delay 这个轴】整天打在 delay=54，而 delay 决定欠压累计能量 ⇒ 它同时决定致死边"
              "（54 时下沿 363..368；84 时抬到 375..380，375 一发即死）。"
              "【为什么最后钉在 64/372 而不是 84/380】(84,380) 打了 27 发，DSOV 一次都没动 —— 同坐标同波形"
              "是确定性事件，零反应就是零反应，重复它不买到命中。台账里唯一的命中迹象是 "
              "`d=64 vd=372 p=2 pw=222 -> goldilocks DSOV=0x00000000 CRC=0x55`，与档案 11-21 那颗同坐标。"
              "判决机下次起来先读 logs/ledger.txt 里带 goldilocks 的行（它带 MM-DD 与 run 名，verdicts.tsv "
              "只有 HH:MM:SS 认不出跨没跨天），认完是哪天哪张卡再决定是否加码。")


# 死磕阶段：零 BAR0 是它【自己的】设定，不是全局默认。写成显式导出是因为默认值一旦是 0，任何人工
# 直调代理（不经批脚本）都会静默产出未盖章的 under —— 那正是 2026-11-26 让 9 条代理测试变红的形状。
HAMMER: dict = {"BAR0_SESSIONS": 0, "CONFIRM_READS": 0}


def recipe_exports() -> str:
    """把这一批的自变量打成 shell 的 export 行（开火脚本 eval 它，别再抄第二份）。

    波形永远来自 RECIPE；搜索轴与判决方式由 MODE 决定，且 MODE 自己也 export 出去，
    这样批报告里能直接看到"这批是找区间还是死磕"。
    """
    out = dict(RECIPE)
    if MODE == "hunt":
        out.update(SWEEP)
        # 找区间的生死线是【能不能在第一发死掉之前读到判决词】，所以读时刻也归这个阶段管：
        # 见 first_read_delay_hunt_s 的 why（4 s 的读排在死亡之后 = 整批 0 判决）。
        out["FIRST_READ_DELAY"] = get("first_read_delay_hunt_s")
        # 读时刻= settle + FIRST_READ_DELAY，只调前者等于半个旋钮（见 settle_wait_hunt_ms 的 why）
        out["WAIT"] = get("settle_wait_hunt_ms")
    elif MODE == "hammer":
        out.update(HAMMER)
    else:
        raise SystemExit(f"shot_policy.MODE 只认 hunt / hammer，当前是 {MODE!r}")
    out["MODE"] = MODE
    # 预热坐标永远由规程给（它不是"战役起点"的别名）：两者混在一起时，抬起点就等于让预热去试深，
    # 而预热首发即死 = 闸门拒开批 = 一个电源周期 0 判决（08:54:22 实测）。
    out["PREWARM_VD"] = get("prewarm_vd_mv")
    return "\n".join(f"export {k}={v}" for k, v in sorted(out.items()))


# 每一步都要能在代码里找到落点（第三列），否则它只是一句愿望。
BEFORE_SHOT = [
    ("must", "config 头 4 字节应答 10de:20c2（只读配置空间）", "crack.sh 等卡循环 / poison_check.sh"),
    ("must", "FUSECTRL 危险态（poison / 第三种态）判定通过", "poison_check.sh：config-only，FUSECTRL 那一读留给判决会话"),
    ("must", "没有第二个 BAR0 参与者（连发器 / 别的 agent）", "run_crack.sh 的锁检查"),
    ("must", "固件版本与 I2C 速率闸门（baud=1MHz 是几何的算术前提）", "crack.sh 的 EXPECT_FW / baud 闸门"),
    ("never", "开 BAR0：发前一次都不开（它是唯一挂死过判决机的通路）", "crack.sh 发前路径 + test_regressions 钉住"),
    ("never", "全量 FF capture 出现在距开火 capture_min_gap_s 之内", "early.sh 只在卡健康时被显式调用；见 KNOBS"),
    ("never", "写 DSOV / 熔丝：窗口判据没过就不许写", "burn.sh 的 gate 链"),
    ("must", "本电源周期第一发由预热发买掉，且它必须被判决", "crack.sh 预热闸门 + agent-prewarm.log"),
]

AT_SHOT = [
    ("must", "板子按冻结几何执行：PERST 拉低 -> 坑（reanchor：后续步骤跟着锚定写平移）-> 恢复写",
     "main/glitch_plan.c, main/main.c"),
    ("must", "revert=0（保住 0xE1 交回 PWM-VID 模式）；swin 只在要轨压证据时开",
     "crack.sh 传参 + board.log 的板端原文行"),
    ("never", "战役 running 时拉 PERST / 跑 console 的复位类命令", "bench/revive.sh 的战役检查"),
]

AFTER_SHOT = [
    ("must", "批一结束【先】判要不要断电：卡已 off-bus 就立刻发关机命令，然后才动别的脚本或代码"
             "（操作员 2026-11-26 定的顺序：判决机开着而卡是死的，那段时间什么都换不到）",
     "bench/host-down.sh（temp/r_down.sh 是它的一次性入口）"),
    ("must", "等 first_read_delay_s 才第一次开 BAR0，且只在卡 config 应答时读", "ga100vfi_agent.evaluate()"),
    ("must", "一发最多 bar0_sessions_per_shot 次熔丝块会话；用光就判未盖章，而不是继续读", "ga100vfi_agent.evaluate()"),
    ("must", "每一发打出去都必须有判决或 lost：不许有打了没人判的发", "agent 交判决 / max_lost 停火"),
    ("never", "lost / dead 之后追加读（宁可这一发没有判决）", "ga100vfi_agent.evaluate() 的不追加读规矩"),
    ("never", "卡持续不在场时继续打（presence 看门狗停战，BAR0 一次不花）", "agent.presence_sample()"),
    ("must", "反应（DSOV 离开健康值 / over / hit / goldilocks）= 立刻停战，不加深", "agent.reaction()"),
    ("must", "每发留下：坐标(码)、slip/reset_slip/stretch_us、寄存器、判决、presence 时间线",
     "agent.log / campaign.csv / board.log / presence*.csv"),
]


def get(name):
    """取一个键的值。写错名字要当场炸，不要安静地给个默认值。"""
    try:
        return KNOBS[name]["value"]
    except KeyError:
        raise KeyError("shot_policy 里没有 %r；已有：%s" % (name, ", ".join(sorted(KNOBS)))) from None


def as_table():
    out = ["%-26s %6s %-4s %-11s %s" % ("键", "值", "单位", "状态", "为什么")]
    for k in sorted(KNOBS):
        v = KNOBS[k]
        out.append("%-26s %6s %-4s %-11s %s" % (k, v["value"], v["unit"], v["status"], v["why"]))
    for title, steps in (("开火前", BEFORE_SHOT), ("发内", AT_SHOT), ("发后", AFTER_SHOT)):
        out.append("-- %s" % title)
        for kind, text, where in steps:
            out.append("   [%5s] %s    <- %s" % (kind, text, where))
    return "\n".join(out)


# 这些脚本里不许再出现这些键的字面量赋值（只许从本模块取；实验性覆盖只在 temp/ 下显式 export）。
GUARDED = ["card_settle_s", "CARD_SETTLE_S", "first_read_delay", "FIRST_READ_DELAY",
           "confirm_reads", "CONFIRM_READS", "stable_s", "STABLE_S",
           "max_lost", "presence_grace_s", "prewarm_wait_s", "PREWARM_WAIT",
           "presence_rate_s", "presence_span_s", "batch_poll_s", "batch_max_wait_s",
           "fire_grace_s", "capture_min_gap_s", "settle_wait_ms",
            "survivor_halt_s", "halt_read_tries", "halt_read_gap_s", "prewarm_vd_mv"]
# 判决机外壳（bench/judge-entry/）连**波形**都不许自带默认值：它以前写着 REV 与 SWIN 的默认，
# 调用方一忘 export 就不是 11-21 那一发了。外壳只许透传。
ENTRY_GEOMETRY = ["PULSES", "PW", "GAP", "TD", "VR", "TR", "VD", "MINV", "MAXV", "STEP",
                  "DELAYS", "MAX", "SWIN", "REVERT", "REV", "PREWARM", "BATCH",
                  "CARD_SETTLE_S", "CONFIRM_READS", "STABLE_S", "FIRST_READ_DELAY",
                  "WAIT_CARD", "BAR0_SESSIONS", "PREWARM_VD"]
# 外壳唯一允许自己设的开火变量：包络门禁按操作员规则固定关掉。
ENTRY_ALLOW = {"ENVELOPE_GATE"}
# 允许写字面量的地方：本模块、操作/实验脚本（temp/）、测试与工具。
ALLOWED_PREFIX = ("host/ga100vfi/shot_policy.py", "temp/", "test/", "tools/")


def check_entry(glob_dir="bench/judge-entry"):
    """判决机外壳必须是纯透传：自带波形/节奏默认值 = 第 N 个出处 = 违例。

    这条检查有具体起因：/root/mig-card2/run.sh（不在版本库里）写着 REV 与 SWIN 的默认值，而且与
    冻结配方相反（REV=1、SWIN=1200）。那几批没被咬，只因为调用方每次都显式 export 了 0 —— 靠人记
    的规矩不算规矩，所以它现在是一段代码，并且部署前必过。
    """
    import os
    import re
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    d = os.path.join(root, glob_dir.replace("/", os.sep))
    if not os.path.isdir(d):
        return ["%s 不存在：判决机外壳跑到仓库外面去了（那意味着改它要靠跨机器 sed —— 那次 sed 把"
                "七个文件的行尾字母删掉了）" % glob_dir]
    bad = []
    # 判定"赋值里带了字面量"：`VD=$VD`、`VD=${VD}`、`VD=${VD:-}`、`VD=${VD:?}` 都是透传（放行），
    # `VD=372`、`PW=${PW:-222}`、`REV=${REV:-1}` 都是第二个出处（违例）。
    one = re.compile(r"\b([A-Z_][A-Z0-9_]*)\s*=\s*([^\s;|&)]*)")
    passthru = re.compile(r"^\$\{?[A-Z_][A-Z0-9_]*(?::?[-?+][^}]*)?\}?$")
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".sh"):
            continue
        path = os.path.join(d, fn)
        for ln, line in enumerate(open(path, encoding="utf-8", errors="replace"), 1):
            s = line.strip()
            # 注释与 echo 里的叙述允许提这些键（报告行要能自报名字），赋值行不许。
            if s.startswith("#") or "echo" in s:
                continue
            for m in one.finditer(s):
                key, val = m.group(1), m.group(2)
                if key not in ENTRY_GEOMETRY or key in ENTRY_ALLOW:
                    continue
                if val and passthru.match(val):
                    continue
                bad.append("%s/%s:%d: %s（%s 自带了值，配方只有一个出处：%s）"
                           % (glob_dir, fn, ln, s[:90], key, val))
    return bad


def check_scripts(root="."):
    """返回把规程里的数写死在别处的违例清单。部署与测试都调它，别靠我记得。"""
    import os
    import re
    import sys as _s
    root = root.replace("\\", "/").rstrip("/")
    bad = []
    pat_assign = re.compile(r"\b(%s)\s*[:=]\s*-?[0-9]" % "|".join(GUARDED))
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", ".git", "logs", "state", "build"}]
        for fn in files:
            if not fn.endswith((".sh", ".py")):
                continue
            p = os.path.join(base, fn).replace(os.sep, "/")
            # 相对化要在**两种 root 写法下都对**（相对 "." 与绝对 "/mnt/d/..."）：以前绝对路径时
            # rel 留成全路径，ALLOWED_PREFIX 的 "temp/" 就永远匹配不上，豁免形同虚设。
            rel = p[len(root) + 1:] if p.startswith(root + "/") else p
            if rel.startswith(ALLOWED_PREFIX) or "/tests/" in rel:
                continue
            try:
                text = open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for ln, line in enumerate(text.splitlines(), 1):
                s = line.strip()
                if s.startswith("#") or s.startswith('"""'):
                    continue
                if pat_assign.search(s):
                    bad.append("%s:%d: %s" % (rel, ln, s[:100]))
    _s.stdout.write("")
    return bad


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv[:1] == ["--get"]:
        print(get(argv[1]))
    elif argv[:1] == ["--recipe"]:
        print(recipe_exports())                     # 开火脚本 eval 它：配方只有一个出处
    elif argv[:1] == ["--check-entry"]:
        viol = check_entry()
        for x in viol:
            print("  [!!] 判决机外壳自带了波形/节奏：%s" % x)
        print("shot_policy --check-entry：%s" % ("通过" if not viol else "%d 处违例" % len(viol)))
        sys.exit(1 if viol else 0)
    elif argv[:1] == ["--check-scripts"]:
        viol = check_scripts(argv[1] if len(argv) > 1 else ".") + check_entry()
        for x in viol:
            print("  [!!] 规程外的写死值：%s" % x)
        print("shot_policy --check-scripts：%s" % ("通过" if not viol else "%d 处违例" % len(viol)))
        sys.exit(1 if viol else 0)
    else:
        print(as_table())
