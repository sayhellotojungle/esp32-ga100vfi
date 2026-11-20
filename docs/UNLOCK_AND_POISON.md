# 解锁与 poison：判据、处置与已固化防护

适用范围：GA100 CMP 170HX 电压毛刺解锁与 FF 烧写。本文给出判据、处置顺序，以及每条判据在代码与脚本中的落地位置。命令值与寄存器定义见 [../README.md](../README.md) §2；FF 烧写规程以 ffcalc 仓库 `docs/BURNING.md` 为准。

---

## 一、VRM 锁状态：三种，处置不同

判据只有一个：`z` 打印的 `pwd`，即 `STATUS_CML(0x7E) & 0x08`。固件答复的文字不许用于判锁态。

| 状态 | 现象 | 成因 | 处置 |
|---|---|---|---|
| 无锁 | `pwd=1` | 正常 | 直接开火，不再发 `u` |
| 故障锁 | `pwd=0`，`z` 能读到 | 过压/欠压事件把故障闩上，控制器因此拒绝密码 | 先 `CLEAR_FAULTS(0x03)` 再交密码，一发即解，无需断电 |
| 总线无应答 | `z` 报 `ERR status read` | 接线/地址/供电/总线被占用 | 查硬件，与锁无关 |
| 真死 | 先清故障再交密码仍 `pwd=0` | 极少 | 拔 12V + 3V3 ≥30 s |

### 1.1 故障锁必须先清故障

`GA_CLR`（`0x03`）排在毛刺时间线内，时间线又被 `ensure_unlocked()` 挡在门外：

```
开火 → ensure_unlocked() → 密码被拒（故障闩着）→ 时间线不执行
     → GA_CLR 发不出去 → 故障永远闩着 → 下一发同样空转
```

固件解锁路径因此固定为先 `0x03` 再密码 `0xF2`（`main/main.c` 的 `unlock_state()`）。`0x03` 是普通命令，不受写保护门控；受保护写只有 `0x13/0x1F/0x21/0xE1` 一类。回归锁：`test/test_main.c` 的故障闩锁模型。

### 1.2 无锁时不发 `u`；开火前不发 `w`

`ensure_unlocked()` 除密码外还写 `0x13`（写保护）与 `0x46`（config-id）。预检规则：`pwd=1` 什么都不发，直接打。

裸打（`g`/campaign）之前不发 `w`：`w` 把 VRM 交回 PWM-VID，GPU 接管后 `VOUT_COMMAND` 被压制，窗口无法出现。

### 1.3 `0x46=0x1229` 属于解锁，不是可选项

完整解锁序列（与参考实现 `pico-ga100vfi/firmware/pwmvid.c:215-225` 一致）：

```
密码 0x7C25 → 0x13=0 → 0x46=0x1229（config-id）→ 回读必须等于 29 12
```

`0x1229` 的作用是阻止 GA100 devinit 重新锁上 VRM。缺它时第 1 发释放 `PEX_RST` 后 devinit 即把 VRM 重新锁死，其后每发都在解锁门口返回。`ensure_unlocked()` 现在写这三步并回读校验，写不进去即返回 `config-id-0x1229-refused`。

约束：**任何脚本不得手写 `0x46=0x1228`**（等于把 devinit 那把锁重新装上，实测立刻变成"密码真拒"）。`0x46` 与 fast/stock profile 表（BA/B9/30/31/32/45）无关，`profile_apply(false)` 不会把它写回。

### 1.4 空发的识别

`pwd=0` 时开火 = 每发都在门前 `return`，`GA_RST_LOW` 未执行，判决是对一张未被碰过的卡回 `under`。识别方法（任一）：

- 串口 `glitch done ... writes_failed=1`；
- 示波器/逻辑分析看 `PEX_RST` 是否真的下拉；
- `host/ga100vfi/cli.py` 只在 `writes_failed == 0` 时计入判决。

判决数为 0 的一轮不是参数问题，是没有打出去。两者的排查方向相反。

### 1.5 解锁错误报告分三类

`unlock_cmd()` 分报（`main/main.c`）：

| 输出 | 实际发生 | 处置 |
|---|---|---|
| `unlock ERR unreachable` | 总线不应答 | 查线/地址/占用 |
| `unlock ERR password-refused` | 密码被拒（含已先发 `CLEAR_FAULTS` 仍被拒） | 查故障位、`STATUS_CML` |
| `unlock PARTIAL pwd=1 0x46-failed` | 密码通过，config-id 写不进 | devinit 已锁，见 §1.3 |

规则：只能给一个字符串的错误报告不许用于硬件结论。

---

## 二、熔丝控制器：四态与恢复

```
脉冲过深（over）
  → 控制器进 poison：FUSECTRL = 0xBADFxxxx
  → 此后任何 BAR0 读取都可能挂死 PCIe 上游 → 整机复位（事故 logs/02）
  → 也可能设备层不再应答配置读：id=ffff:ffff bar0=0xffffffff（判 lost）
```

### 2.1 四态判据

`host/ga100vfi/classify.py` 的 `fusectrl_state()` 是唯一判据函数，判决链、CLI 战役循环、台架脚本共用：

| 读数 | 状态 | 判决 | 处置 |
|---|---|---|---|
| `0xE0040000` | `idle` | 可判决 | 放行 |
| `0xFFFFFFFF` | `unreachable` | `lost` | 连号 `lost` 停战（代理 `--max-lost`，默认 3），不发补发 |
| `0xBADFxxxx` | `poisoned` | 不判决 | 抛 `Poisoned`，停战役，退出码 3/4 |
| 其他（实测 `0x00000000`） | `unknown` | 不判决 | 抛 `ControllerUnknown`，同上处置 |

两条硬性顺序与预算约束：

1. `unreachable` 必须先于 `poisoned` 判定：`0xFFFFFFFF` 的状态位同为 31；
2. 判定只允许一次 `FUSECTRL` 读。命中危险态后不许"再读一次确认"、不许 `classify`、不许 `capture`。

回归锁：`host/tests/test_regressions.py::PoisonGuard`，断言访问序列恰好等于 `[("r", FUSECTRL)]`。把 `STATE_UNKNOWN` 改为 `STATE_IDLE` 立刻 8 处 FAIL（已用变异验证）。

### 2.2 固化位置

| 层 | 位置 | 行为 |
|---|---|---|
| 判决链 | `host/ga100vfi/classify.py` `classify()` / `snapshot()` 第一行 | 抛 `FuseControllerStop` 子类，除这一读外不碰 BAR0 |
| HTTP 判决代理 | `host/ga100vfi_agent.py` `gpu_verdict()`（存在性探针之前）、`run_forever()` | 打印停火原因 → `stop_board()` → 退出码 3；每发判决带寄存器快照 |
| 串口战役 CLI | `host/ga100vfi/cli.py` `cmd_campaign` / `cmd_classify` / `cmd_baseline` | 停循环、输出 `poisoned_stop`、退出码 4 |
| 烧写 | `host/ga100vfi/burn.py` | `FuseError` |
| 台架 | `bench/poison_check.sh`、`bench/window.sh` 第 0 步、`bench/crack.sh` 第 1 道门 | 单寄存器一问 |

### 2.3 恢复顺序

1. 重启判决机（POST 拉一次 `PERST#`，卡与控制器一起走 POR）；
2. `bench/card_probe.sh`：卡重新枚举 → `bench/poison_check.sh` 验 `FUSECTRL=0xE004…` → 继续；
3. 卡未回来（全盘无 `10de`）但 `console.sh z` 显示 VRM 仍应答 → `bench/link_retrain.sh` 对桥发 Retrain + rescan（只写桥）；
4. 仍不回 → 拔 12V + 3V3 ≥30 s。判据为第 2、3 步的组合：有电、无链路、软件手段用尽，剩下的是 PHY 的 POR。

适用边界：poison 来自空发轮（`PEX_RST` 未动过）时，第 1 步实测足够；poison 来自真发（`wfail=0`、深度欠压横跨释放窗口）时，实测第 1–3 步不足以恢复，需第 4 步。两条路都走，顺序不许反。

Gen4 PHY 训练状态与控制器 poison 是两件事，处置不可互抄。

### 2.4 电压窗口实测（本卡，`3×222µs@334` / `delay=54` / `fast=1`）

| vd | PMBus 码 | FUSECTRL | DSOV | CRC | 结果 |
|---|---|---|---|---|---|
| 334 | 53（331 mV） | `e0040000` | 1 | `0x57` / `0x55` | under（太浅） |
| 330 | 53（同 334） | `e0040000` | `ffffffff` | — | lost（链路未起）→ 同轮第三发 poison |
| 309 | 49（250 mV 档） | `00000000` | 1 | `0x77` | `unknown` 态；同电压下一发 poison |
| **326** | **52（325 mV）** | `e0040000` | **0** | **`0x55`** | **goldilocks，第 1 发命中** |

两条规则：

1. `vd` 非线性。PMBus 码 = `(mv*16+50)/100`，LSB 6.25 mV：`vd=330` 与 `vd=334` 是同一个码 53，写入电压相同。换档必须跨过一个 LSB（334 → 326 → 319）。
2. 一发 `lost` 即停火。`lost` 之后控制器处于半死态，补一发实测得到 poison。`bench/crack.sh` 内置该停火条件。

### 2.5 窗口是易失的

命中之后禁止：发 `r`（`PEX_RST` = 熔丝重采，窗口立即关闭）、加载 `nvidia` 驱动、热重启。取证只走 `bench/window.sh`（全只读 + `CMD_READ`）。烧写是独立动作，逐条人工确认，脚本中不存在自动 sense/arm/burn。

---

## 三、网格几何：`max ≥ ndelays × batch`

`main/campaign.c` 的 `point_budget_locked()` 给每个 delay 点 `max/ndelays` 次机会；电压 ladder 要在同一电压点攒够 `batch` 个判决才挪一格。

```
max=20, ndelays=6, batch=4 → 每点 3 次 < batch → ladder 不动 → 20 发全打在同一个 (delay=0, vd=300)
```

该浪费从判决流里看不出来（每条判决都合法）。`bench/crack.sh` 在开火前计算几何，不满足即拒绝启动。本卡使用 `max=12, ndelays=1, batch=2`；多点搜索时按公式放大 `max`。

---

## 四、跨轮台账

一个电源周期能买到的发数上限是"到第一次 poison 为止"，因此每轮发数必须累进同一张表：

```bash
bash $HOME/ga100vfi/bench/mkledger.sh    # 汇总所有 *-crack*/agent.log → logs/ledger.txt
```

判读表（`(delay, vd) → under/over/poison/lost`）：

| 表里看到 | 结论 | 下一轮 |
|---|---|---|
| 全 `under` | 还不够深 | `vd` 调低 |
| 全 `over`/`poison` | 太深 | `vd` 调高 |
| `under` 与 `over` 混现 | 边界在此 | 该 `vd` 附近加密 delay 点 |
| `lost` | 卡不在总线 | 该批发数不算数据，先按 §2.3 恢复 |

poison 那一发不由代理回传判决（红线所致），台账按板子记录的坐标补一条 `poison` 事件。

台账坐标必须包含波形几何。`(delay, vd)` 两个数不足以复现一发脉冲：`pw/pulses/gap/td` 每一项都改变物理结果。档案命中几何为 `3 × 222 µs`、`gap 150 µs`、`td=-40 vr=750 tr=-20`、`delay=54`；单发 `pw=15000`（15 ms @ 300 mV）按档案定义属 over（欠压横跨整个启动窗），实测两轮各一发 poison，已弃用。

### 4.1 判决链自身的排除实验

`bench/readtrip.sh` 一个脉冲不打，重放判决链的全部访问（含连写三次 `DSOV_SET=1`），`FUSECTRL` 全程 `0xE004…`。结论：判决链的读写不会把控制器打进 poison。

---

## 五、台架脚本

源文件在仓库 `bench/` 下，由 `tools/deploy_bench.py` 一条命令装到 `$HOME/ga100vfi/bench/`。部署会先清理旧文件再解包、规范化 CRLF、跑 `bash -n`、并在机器上实跑 `FUSECTRL` 四态归类。脚本清单与用途见 [../bench/README.md](../bench/README.md)。

`crack.sh` 开火前的五道闸门：

1. 固件版本闸门：`/api/status` 的 `net.build` 首词必须等于 `EXPECT_FW`（与 `main/board.h` 的 `GA_FW_VERSION` 同步，部署时断言），否则拒绝开火；
2. 等卡回 PCIe（只看配置空间，不碰 BAR0），必要时 `pci rescan`；
3. config-only 探活取真实 `vendor:device`（sysfs 缓存会说谎）+ 只读一次 `FUSECTRL` 判危险态；
4. 判锁态：`pwd=1` 放行；`pwd=0` 走 `@w 25 03` + `u` + 回读，仍 `pwd=0` 拒绝开火；
5. 网格几何 `max ≥ ndelays × batch`。

每道门各自对应一类无效发数：①整轮日志无法回答"哪版固件打的"；②把"还没枚举"当卡死；③在 poison 态读 BAR0；④锁着开火整轮空转；⑤整轮塌在一个电压点。

---

## 六、FF 烧写闭环（本卡实烧结果）

### 6.1 结果

Goldilocks 命中（第 1 发、`vd=326` = PMBus 码 52、`delay=54`、`3×222µs`）后，窗口内烧入 16 条 FF 记录（行 297–312），逐条回读一致，全程 `CRC=0x55`，无 poison。`PEX_RST`（判决机热重启）后 devinit 重解析，14 个 option 影子寄存器全部等于计划目标：

| 字段 | 烧前 | 烧后 |
|---|---|---|
| `gpc_disable` | `0x15`（3 个 GPC 屏蔽） | `0x0` |
| `tpc_gpc0..7` | `ff/01/ff/01/ff/00/01/00` | `00/00/02/00/01/00/00/00`（保留 gpc2/gpc4 各 1 个缺陷 TPC） |
| `fbio_disable` / `fbpa_disable` | `0xc0330c` | `0xc03000` |
| `fbp_disable` | `0x852`（10 可用） | `0x840`（仍 10 可用，只清营销位） |
| `rop_l2_disable` | `0xc0330c` | `0xc03000` |
| `pes_disable` | `0x71c7` | `0x0` |
| `nvlink_disable` / `sys_pipe_disable` | `0x7` / `0xfe` | `0x0` / `0x0` |
| `fbp_defective` | `0x840` | `0x840`（物理缺陷位不变） |

烧后 `ffcalc gen`/`scan`：剩余计划 0 条，`80 GiB / 124 SM` 到顶。指纹由 `58bf9ac855da7053` 变为 `fc497d01b8255867`（SBFE 0→1）。烧后 `replay` 与寄存器一致性检查差异为 0（含烧前有系统性偏差的 5 个 chain-2 PCIe 位）；`tpc_gpc5/gpc7` 仍为 status 层 1 / fused 层 0，属固件运行时自屏蔽，不补烧。

### 6.2 四道闸门（缺一不可）

1. `ffcalc verify`：UUID/指纹认卡通过；
2. `ffcalc sim`：`SIMULATION OK`，0 error；
3. `ga100vfi.burn crosscheck`：独立实现，额外执行 `new & ~old == 0` 只清位规则，并现场逐行回读 16 行全空白，`0 problems`；
4. 两次独立读行逐字节一致（512 行 × 2 次，0 差异）。

烧写节奏：先单烧行 307（`0x420004c1`，`fbp`+`rop_l2`），复核「512 行中只有 307 变化」；再烧其余 15 条，复核「16 行全中、其余 496 行零改动」。

### 6.3 四条事实

1. **`CMD_SENSE` 不刷新 option 影子寄存器。** `0x00820364`(`fbp`) 等只在 `PEX_RST`/devinit 重算；`SENSE` 只重解析熔丝宏自身影子（EFP/SBFE/CRC/DSOV）。验证记录是否被采纳必须 `PEX_RST`。映射本身不需要靠冷启证明：烧前影子等于链重放值（例 `(0x4290>>3)&0xFFF = 0x852`）。
2. **`EFP_STATUS(0x00820078)` 冷启后读 0，发一次 `CMD_SENSE` 才为 1。** ffcalc `burntool` 的 `in_write_window()` 要求 `EFP==1`，刚进窗口时会拒 burn；先 `sense` 即可。`ga100vfi.burn` 的 `window_ok()` 不含 EFP，`arm()` 之前先 `sense()`。
3. **`pcie_magic_gen4_dis` 即 `magic_d` 的 bit25**（`magic_d` 为 bit730 起 32 位，跨 block 45/46/47）。清该位必然使 `magic_d` 从 `0x16680000` 变 `0x14680000`。
4. **FF 链是追加/后写覆盖的。** 写错的记录可用新行纠正；本卡烧后剩余 174 行空闲。

### 6.4 现场数据

`burnplan/`：`plan.json`、`card-dump.json`、`crosscheck_report.md`、`ffcalc-verify.txt`、`ffcalc-sim.txt`、`after_row307.json`、`after_all16.json`、`snapshot_post.json`、`card-dump-post.json`、`plan-post-empty.json`。

---

## 七、已推翻的结论（保留作判据）

| 不成立的结论 | 事实 | 判据 |
|---|---|---|
| 「判决全 `under` ⇒ 电压配方不对」 | 该轮 20 发全为空发（`writes_failed=1`） | 看 `PEX_RST` 是否下拉；读 `writes_failed` |
| 「VRM 闩死必须拔 VDD33 ≥30 s」 | 故障锁一发即解（`cml 0x00→0x08`） | 先 `CLEAR_FAULTS`，再看 `pwd` |
| 「毛刺把卡打死」 | poison 后设备不应答配置读；`PEX_RST` 走 POR 即恢复 | `config-only` 读到 `ffff:ffff`，重启后 `10de:20c2` 回来 |
| 「I2C 被占用 / 板子拉住时钟」 | campaign 用 `hold_ms=0`、`scl_wait_us=10000`；空闲时寄存器不变 | 排除性实验（`logs/*-observe*`） |
| 「网格不收敛 ⇒ ladder 有 bug」 | `max < ndelays × batch` 的配置错误 | 读 `point_budget_locked()` |
| 「链路 `2.5GT/s ×2` 是故障」 | 该卡出厂为 Gen1 x4，x16 需 PCIe 电容改造 | §6.1；FF 与链路速率无关 |
| 「第 2 发必 poison」 | 旧固件的"第 2 发"是空发，毒由真发留下；与发数无关 | `readtrip.sh` + 波形几何对表 |
| 「`fast=1` 导致连打失败」 | 缺 config-id `0x46=0x1229`；参考实现中奖行同为 `fast=1` | §1.3 |

共同点：每一条都源于跳过判据直接下结论。每条现在对应一个脚本或一段断言。
