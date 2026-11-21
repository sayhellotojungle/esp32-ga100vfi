# 台架脚本（判决机上运行）

`bench/` 下的脚本承担开火、取证、恢复判据三类工作，运行在插着目标卡并跑 Linux 的判决机上。源文件随仓库走，不放在 `/tmp`：判决机每次重启清空 `/tmp`，脚本丢失曾两次把战役预检卡在"依赖文件不见了"。

## 部署

从 Windows 端（仓库根目录）：

```bat
set BENCH_HOST=<判决机IP>
set BENCH_USER=<判决机用户>
set BENCH_PW=<判决机口令>
python tools/deploy_bench.py
```

三个值都必须显式给出，仓库里不存判决机地址与口令；`BENCH_PREFIX` 可选，缺省为登录账户的 `$HOME/ga100vfi`。同理，所有台架脚本的板子地址都来自环境变量 `BOARD=http://<设备IP>`，缺 `BOARD` 直接退出 2——写死任何一台台架的地址，都会让下一个照做的人去打别人的卡。

部署步骤与断言（`tools/deploy_bench.py`）：

1. 打包 `host/` + `bench/` 上传解包到 `$HOME/ga100vfi/`；先删除旧 `bench/` 与 `host/__pycache__` 再解包，仓库里改名或删除的文件不会残留在机器上；
2. `tr -d '\r'` 规范化 CRLF，脚本加执行位，并为 `/tmp/.console.sh` 等旧路径留转发壳；
3. `bash -n` 检查全部脚本；
4. 断言红线判据在包里：`Poisoned` 与 `FUSECTRL` poison 判据、`STATE_UNKNOWN` 第三种态判据、判决行携带 `delay=`/`vd=` 坐标；
5. 在机器上 import 部署后的 `ga100vfi.classify`，逐一断言 `FUSECTRL` 四态归类正确，且 `classify()` 对 `0xBADF…` 与 `0x00000000` 都拒绝写 `DSOV_SET`。

第 4、5 步的意义：部署了没有防护的包比部署失败更危险。

## 脚本

| 脚本 | 用途 | 写硬件 |
|---|---|---|
| `crack.sh` | 开火入口：固件版本闸门 + **实际 I²C 速率闸门** + 等卡 + config 探活与 `FUSECTRL` 危险态 + 锁态 + 网格几何，共六道闸门，然后跑板载 campaign 并落跨轮台账（收尾追加 `logs/verdicts.tsv` 真值行） | 是 |
| `window.sh` | 命中后取证：`classify` / `baseline` / `burn status` / `capture`；第 0 步先问 `FUSECTRL` | 只读（含 `classify` 固有的一次 DSOV 写） |
| `burn.sh` | 烧写入口：`wake` → 窗口判据 → 快照 → 交叉验证 → **试烧计划第一条** → 独立重读逐行核对 → 续烧其余。每一步真写前等人输入 `YES` | 是（熔丝，不可逆） |
| `postboot.sh` | 冷启动后复核：`wake` → 控制器状态（`CRC`/`FUSEDEBUG`/poison）→ 与烧前快照逐行比 → 影子寄存器只读取影。`PLAN=`/`SNAP=`/`SHADOW_BEFORE=` 可省 | 只写 `Command` bit1 |
| `run_crack.sh` | 薄壳：确认没有第二个 BAR0 参与者（连发器 / agent / crack）后转交 `crack.sh`。**不自带几何默认值** | 是（转交） |
| `poison_check.sh` | 只读一次 `FUSECTRL`，给出恢复判据与处置顺序 | 只读一个寄存器 |
| `card_probe.sh` | 设备在不在、桥链路宽度、残留驱动 | 只读 |
| `link_retrain.sh` | VRM 是否应答 + 对**桥**发 Retrain + `pci rescan` | 写桥，不碰卡 |
| `readtrip.sh` | 零发实验：不打的条件下重放判决链访问（含连写三次 `DSOV_SET`），判是否我们自己的读写打进 poison | 是（判决链原样） |
| `mkledger.sh` | 汇总 `logs/*-crack*/agent.log` → `logs/ledger.txt`，输出 `(delay, vd) → 判决` 总表（含 `goldilocks`/`void`/`nojudge` 列，并声明本表来源仅 `agent.log`） | 否 |
| `backfill_verdicts.sh` | 从运行目录回填 `logs/verdicts.tsv`（真值台账）。409、手工判决、被吞掉的 `goldilocks` 都靠它补齐，不人肉抄 | 否 |
| `ota.sh` | `BOARD=http://<设备IP> IMG=… [EXPECT=<sha前缀>] bash ota.sh`；推送后回读 `/api/status` 的 `build` 并要求等于期望值，缺失即退出 4 | 刷固件，不碰卡 |
| `console.sh` | UART0 控制台助手（板端 `/api/log` 只有开机横幅，`@r`/`@w` 的答复只能从串口取） | 取决于所发命令 |

## 开火

```bash
BOARD=http://<设备IP> MAX=12 BATCH=2 VD=334 MINV=284 MAXV=384 STEP=25 DELAYS=54 \
  bash $HOME/ga100vfi/bench/crack.sh
```

默认波形即档案命中几何：`3 × 222 µs`、`gap 150 µs`、`delay=54`、`td=-40 vr=750 tr=-20`。搜索几何（`VD/MINV/MAXV/STEP/BATCH/MAX/DELAYS`）可以调；波形几何只在 `under`/`over` 判据要求时改，并需留下比档案更强的证据。单发 `pw=15000` @ `vd=300` 按档案定义属 over（欠压横跨整个启动窗），已从默认值移除。

台账目录名自描述（固件版本 + 波形几何 + 电压），因为跨轮复盘依赖目录名回答"哪版固件、什么波形、几发"。

### 健康区随机扫（`SWEEP=random`）

命中是概率事件，而"阶梯往深走一格"等于把每一发都押在悬崖上。所以开火几何可以不由人拍，改由包络
在**健康区**里随机生成：

```bash
BOARD=http://<设备IP> SWEEP=random NDELAYS=4 MAX=16 BATCH=1 PW=222 TD=-40 PULSES=2 \
  bash $HOME/ga100vfi/bench/crack.sh
```

（`DELAYS`/`VD`/`MINV`/`MAXV`/`STEP` 在这模式下会被包络给出的值覆盖，不用手填。）

`healthy_sweep()`（`host/ga100vfi/envelope.py`）随机的是三样：相位候选（从域里洗牌取，**冻掉的档
直接淘汰**）、电压窗口在 `HEALTHY_BOX` 里的落点与宽度、ladder 步长。三条约束：

1. 随机只发生在 `HEALTHY_BOX`（默认 `362..380`）内 —— 本台账里 poison 全在 354..358，而卡反复
   活着回来的地方在 366 以上；
2. **每个候选坐标都过 `check()`**：族冻结、按坐标禁火、rung 功课一条都不许绕。所以"打死过卡的
   那个点"不会因为换了随机窗口就被绕过去（`delay=64` 配不上任何盖住 362 的窗口）；
3. `pw` 与 `td` 不参与随机 —— 动那两根轴是换配方，不是抽签。

门禁因此也改了：`--delays` 会**逐个 delay 点都审**。以前只审 `delays=` 的第一个点，多点批次里后面
那些点等于没有门禁，而随机扫一批就是四个点。

预检拒绝开火的四种情况：

1. `FUSECTRL` 为 `0xBADF…` 或叫不出名字的值 → 绝不进判决链（poison 态读 BAR0 = 整机复位，事故 `logs/02`）；
2. `pwd=0` 且清故障后仍 `pwd=0` → 此时每发都在 `ensure_unlocked()` 门口返回，`PEX_RST` 不动，买到的是假 `under`；
3. `max < ndelays × batch` → 整个网格塌在一个电压点；
4. 等不到卡（`WAIT_CARD` 秒内配置空间不可达）→ 先按下面的顺序分清是哪一种。

## 卡不见了（按顺序，不许倒）

```bash
bash bench/card_probe.sh       # 设备是否还在、桥链路宽度、残留驱动
bash bench/link_retrain.sh     # 对桥发 Retrain + rescan（只写桥；本卡实测无效，见下）
```

```text
板子控制台（bench/console.sh 或 POST /api/cmd）：  @r 25 7e 1   # STATUS_CML，卡有没有电
```

判据（2026-11-21 重测，本卡 `10de:20c2`）：毛刺之后卡不在总线上时，**卡从不自愈** —— 同一发
命令零干预观察 60 s、240 次只读 `config` 全是 `0xffffffff`。

"不在总线上"有**两种**，处置与代价不同，别混成一条（混了就会对硬死反复摁 `PEX_RST`，白花时间）：

| 现象 | 救法 | 那一发的判决 | 算不算包络里的事故 |
|---|---|---|---|
| `config` 全 `1`，板子 `j`→`J` 后 ~0.3 s 重新枚举 | 板子自己就会做（每发的 `g` 自带 `PERST#`） | **作废**：`PEX_RST` 重放熔丝链，刚打出的 arm 一起冲掉 | 否，记 `void` |
| `config` 全 `1` 且 `PEX_RST` 叫不回；主机 `remove`+`rescan` 后 `01:00.0` 节点根本不出现、桥 `LnkSta` 停在 `0x1081`（`dllLA`=0） | 只能断电（`12V` + `3V3` ≥30 s） | 无从判决 | 是，记 `dead` |

今天（2026-11-21）第二种实测 6 次，其中 5 次是"拔电之后的**第一发**"。所以一次电源周期真正买
到的发数远比 `max` 少，发数稀缺这件事只能靠"每发都留下坐标与判决"来省。

对上游桥下发 `Retrain Link` + `rescan` **实测 4/4 无效**：桥的 `LnkSta` 停在 `0x1081`、
`dllLA` 始终 0，EP 侧根本没重试训练。所以这一条只能当作"顺手排掉桥侧故障"的便宜判据，不要
指望它把卡救回来；`link_retrain.sh` 保留是因为它顺带回答"卡有没有电"。

代价要说清楚：`PEX_RST` 会**重放熔丝链**，所以靠它救回来的那一发读不到有效判决（刚打出来的
arm 一起被冲掉）。掉链的发＝白烧一发，降白烧率只能靠坐标与节奏，不能靠恢复手段。

`FUSECTRL` 是 poison（`0xBADFxxxx`）而设备仍在：先让板子把 `PEX_RST` 按住 2 s（实测足够，
`0xbadf510e → 0xe0040000`，`device id` 也从 `10de:2080` 回到 `10de:20c2`）；仍不清才重启判决机
（POST 拉同一根线），最后才断电。毒态在 `config` 的 `device id` 里就看得见，一次 BAR0 都不用开。

## 台账：哪些发算打过、哪些算事故

包络门禁（`host/ga100vfi/envelope.py`）按族（`pw` × 相位档）统计事故，冻结反复打死卡的形状。
所以**每一行台账的含义都是判据的一部分**，四类必须分清：

| 判决 | 含义 | 算打过 | 算事故 |
|---|---|---|---|
| `under` / `over` / `hit` / `goldilocks` | 判决读回来了 | 是 | 否 |
| `poison` / `dead` | 卡被这个形状打进毒态，或端点自己关掉、只能断电 | 是 | **是**（`FATAL_CLASS`） |
| `lost` | 判决没读回来，卡还在 | 是 | 否 |
| `void` | 判决作废：桥 retrain 失败，板子拉 `PEX_RST` 就把卡叫回来了 | 是 | 否 |
| `nojudge` | 板子打了、判决 0 行，但卡仍应答 `config` | 是 | 否 |

三条踩过的坑：

1. `lost` **既不是事故也不算反应**。本台账 44 发 `lost` 的深度从 `vd=339` 铺到 `vd=380`，与深度
   无关，是链路/台架现象。上一版把它算进"over 类"，结果把唯一零硬史的族（`pw=222`）冻掉了 ——
   方向正好反了。它只进"链路健康"那一行。
2. **判决作废不能记成 `dead`**。台架自己恢复得动的事件如果被记成事故，门禁就会拒绝一个其实零
   硬史的族：`pw=222` 曾被这种行凭空扣上 5 发事故，而它是本卡唯一零硬史的宽度。
3. **板子打了但判决 0 行的那一发必须进台账**（`crack.sh` 收尾时按现场证据补 `dead`/`nojudge`）。
   以前这种发根本不留痕，于是"刚把卡打死的那一族"在包络里依然显示零事故，下一轮门禁照样放行
   同一个形状去打死第二次。判决词也**不列白名单**：写的一方加新词，读的一方若列清单就静默丢掉。

同一 `attempt` 的重复行（判决 409 后代理会重试，`agent.log` 与 `ladder.log` 各一份）只算一发，
否则"浅档已攒够 12 发"会变成假话。

## 节奏：命中是概率事件

不存在"确定的临界值"，也不存在"打了很多发零反应所以这个形状不行"这种判据 —— 零反应只是还没
命中。到了包络边缘应当**在同一族上连续补发**，而不是换形状重开一局。配方每张卡都可能不一样：
台账里的数字是本台架本张卡的战绩，不是定律，不要抄成别人的起点。

## 固件版本自证

版本号定义在 `main/board.h` 的 `GA_FW_VERSION`，启动横幅与 `/api/status` 的 `net.build` 共用该宏；`GA_BUILD_STAMP` 记录编译时刻，用于区分同版本号的多次构建。

三处断言构成链条：

1. `tools/deploy_bench.py` 部署前断言 `main/board.h`、`bench/crack.sh` 的 `EXPECT_FW` 默认值、`host/pyproject.toml` 三处版本一致，并断言部署包含 `EXPECT_FW` 闸门；
2. `ota.sh` 要求镜像 sha 前缀等于 `EXPECT`，刷完回读 `net.build`，字段缺失即退出 4（用法 `BOARD=http://<设备IP> IMG=… EXPECT=<sha前缀> bash bench/ota.sh`）；
3. `crack.sh` 开火前要求 `net.build` 首词等于 `EXPECT_FW`（当前 `0.4.0`），不等即退出 3。

`bench/crack.sh` 的日志目录名取 `build` 首词，因此跨轮台账能回答"这一轮是哪版固件"。绕过闸门（`EXPECT_FW=any`）的代价是这轮日志不可追溯。

`crack.sh` 还有一道**速率闸门**：要求 `/api/status` 的 `board.baud`（实际生效值，不是请求值）等于 `EXPECT_BAUD`（默认 `1000000`），不等即退出 3。波形配方是 1 MHz 的算术（一笔 16 位 PMBus 写 36 µs，档案的 `delay` 就是对着这个宽度标的），总线自己降级到 400/100 kHz 之后每一发的 dip 边沿都被拉长，打出来的判决与台账里前面那些发不是同一个实验。0.4.0 换了 I²C 引脚，这条从"有用"升级成"必查"：接错线、忘接上拉、线太长，第一个症状都是这里降级。降级速率下强行开火用 `EXPECT_BAUD=any`，并在台账里另记实际速率。

当前板端实测（2026-11-21 开火前 `/api/status` 自证）：`net.build = 0.3.2 (Nov 21 2026 13:00:10)`，`baud = 1000000`。0.4.0 换了 I²C 引脚，须先按 [README §4](../README.md) 把 SMBus 线从 47/48 移到 6/7，再 `ota.sh` 刷到 0.4.0；0.3.1/0.3.2 的镜像记录见 [docs/TEST_RESULTS.md](../docs/TEST_RESULTS.md)。
