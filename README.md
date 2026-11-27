# esp32-ga100vfi 0.5.9

ESP32-S3 电压毛刺器，用于 NVIDIA GA100 CMP 170HX（`10de:20c2`）的 FF 熔丝解锁。毛刺打出 Goldilocks 写窗口 → 窗口内取证 → 生成 FF 烧写计划并交叉验证 → 追加 FF 记录 → 冷启动验收。

本文的顺序就是接手该项目需要建立的认知：**原理（§1）→ 流程（§2）→ 判据（§3）→ 随机预期（§4）→ 红线（§5）**。§0 是给已经读过一遍的人的执行卡。

分工是固定的：能读 BAR0 的只有插着卡的那台 Linux，因此**每一发的判决只能由主机给出**（0.4.1 删除板载判决：本台架对该 SMBPBI 端点四次读全部无回执）。判决方（主机代理）经串口或 HTTP 交判决并以心跳换回页面的开跑请求；网页只负责观测、调参与递交一次「开始」。**一发无人判决即结束本轮战役**，板子既不猜电压方向也不打下一个坐标。

| 依赖 | 用途 | 是否可替代 |
|---|---|---|
| **ffcalc**（同作者独立仓库） | FF 链分析、烧写计划生成、`verify`/`sim` 交叉验证 | 不可。计划与验证以它为准 |
| `host/ga100vfi`（本仓库） | 判决链、512 行采集、第二套独立对账、烧写执行 | 不可（与 ffcalc 互为独立实现，必须互相对账） |
| [cmpunlocker](https://github.com/amoghmunikote/cmpunlocker) | 熔丝之外的驱动层几何显现与 Gen4 重训（§2.8） | 属另一层，与本仓库无关 |

上游为 [dalzyu/pico-ga100vfi](https://github.com/dalzyu/pico-ga100vfi)（RP2350 实现）：毛刺原理与时序打法承袭上游；固件移植到 ESP32-S3；主机端工具、HTTP 协议与熔丝规程在本仓库重写。原理的原始出处是 [kate 的毛刺解锁长文](https://blog.kate.land/posts/using-voltage-glitching-to-unlock-the-a100/)，本仓库的复现记录与它的一致与分歧写在 §1.6 与 §4。另参考 [cmpunlocker](https://github.com/amoghmunikote/cmpunlocker)、[hexkyz/neutralized](https://github.com/hexkyz/neutralized)。

---

## 0. 先读后动

执行任何开火或烧写动作前，先读完下表四份文件并按其判据行动。本项目推荐把仓库整体交给 AI 读取后再由其执行操作——**流程的正确性靠脚本与判据，不靠执行者的记性**，AI 与人在这里适用同一节。

| 顺序 | 文件 | 必须从中得到的结论 |
|---|---|---|
| 1 | 本文 §1（原理）、§2（流程）、§3（判据与寄存器）、§5（安全红线） | 为什么这么做、动作顺序、停止条件 |
| 2 | ffcalc 仓库 `docs/BURNING.md` | 寄存器与命令实机值、烧写授权断言、踩坑速查 |
| 3 | [docs/UNLOCK_AND_POISON.md](docs/UNLOCK_AND_POISON.md) | 判决五档、随机性模型、`FUSECTRL` 四态、恢复顺序、已推翻的结论 |
| 4 | [bench/README.md](bench/README.md) | 台架脚本、开火前六道闸门、节奏参数出处 |

动手前必须能复述以下五项，任一项说不出即不动手：

1. 目标卡的 `vendor:device`、PCIe BDF、UUID 与 fingerprint；
2. 当前 `FUSECTRL` 读数及其四态归类；
3. 计划记录数、行号范围、每条记录的 `chain/block/old/new`；
4. 分批烧写方案（第一条是哪一行，复核判据是什么）；
5. 停止条件（§5）与恢复顺序。

三类参数不许自行改动，也不许"顺手优化"：**波形参数**（`td vr tr pw gap pulses fast revert`，只用 §2.3 的档案几何；搜索轴 `vd`/`delay` 是每卡待寻的坐标，见 §4，换它不算改波形，但只能按 `shot_policy --recipe` 给的格子换）；**寄存器地址与命令值**（§3 全为实机确认值）；**判据**（`FUSECTRL` 四态、poison 后禁读 BAR0、`new & ~old == 0`、目标行必须空白）。节奏与搜索参数（读时刻、看门狗、预热电压、模式）的唯一出处是 `host/ga100vfi/shot_policy.py`，脚本里出现字面量参数即违例。

文档中的地址、口令、串口、BDF 一律是占位符：`<设备IP>` 是毛刺板 LAN 地址，`<判决机IP>` 是插卡并跑 Linux 的主机，`<串口>` 是 TTL 适配器设备名，`<BDF>` 用 `lspci -D -d 10de:` 现取。台架脚本对这些值不给默认——没填就退出，免得别人的台架地址变成你的操作目标。以下命令都在判决机的仓库目录里执行：

```bash
export BOARD=http://<设备IP>                            # 板子串口横幅或 /api/status 的 net.ip
export BDF=$(lspci -D -d 10de: | awk 'NR==1{print $1}') # 目标卡 BDF，多卡机自己确认是哪一个
```

### 0.1 执行卡

| # | 动作 | 命令 | 进入下一步的判据 | 停在这一步的条件 |
|---|---|---|---|---|
| 1 | 部署与体检 | `python tools/deploy_bench.py`；`bash bench/poison_check.sh` | 红线断言全过；`FUSECTRL=0xE0040000`；`build` 与源码版本一致 | `0xBADF…`（poison，卡只能按已烧状态继续）或读不到 |
| 2 | 建立解锁状态 | `bash bench/crack.sh`（内置锁态判定） | `pwd=1` | 清故障后仍 `pwd=0` → 拔电 ≥30 s |
| 3 | 打毛刺 | `python3 -m ga100vfi.shot_policy --recipe` 然后 `bash bench/crack.sh` | 出现"反应"（`DSOV` 1→0）并判为 `goldilocks` | 一发 `lost` 立即停火；`FUSECTRL` 非 IDLE 停火；`over`/`hit` 当次叫停 |
| 4 | 窗口内取证 | `bash bench/window.sh` | 窗口判据（§2.4）全绿；两次独立读逐字节一致 | 两读不一致 = 快照不可信，别拿它算计划 |
| 5 | 计划 + 四道闸门 | `DIR=<窗口目录> bash bench/planmig.sh` | `SIMULATION OK`、`verify … belongs to card`、`crosscheck 0 problems` | 任一闸门红：现场与计划不一致，停，回 §2.4 重取 |
| 6 | 烧写 | `PLAN=… bash bench/burn.sh` | 试烧 1 条 → 独立重读 → 只有该行按记录改变 → 续烧 → 再重读 → 计划外零改动 | `0xBADF…`、超时、计划外改动 → 立即停手 |
| 7 | 冷启动验收 | `bash bench/postboot.sh` | A 控制器健康；B 逐行差异恰为计划内行；C 影子等于目标、`gen` 剩余 0 条 | CRC 非 `0x55` 或影子不匹配 → 别再烧，按恢复顺序处理 |

每一步都可以整步重跑（幂等），唯一的例外是熔丝真写。判到 `over`/`hit`/`goldilocks` 之后不"再打一发确认"：那一发没有中间态可停靠。

---

## 1. 原理

### 1.1 限制来自哪里

GA100 的熔丝控制器包含 16,384 颗 eFuse（512 行 × 32 位）与读它、写它、保护它的那套逻辑。它的输出不是"位"而是 **option words**：芯片其余部分只消费 option words。其中一组 `_DISABLE` option words 直接驱动 **floorsweep** 线——被 sweep 掉的块不可达（且不供电），于是同一颗 die 出厂时可以是 A100，也可以是只有 70 SM、8 GB、Gen1 x4 的矿卡。

关键性质：**`_DISABLE` 掩码只能降到 `_DEFECTIVE` 掩码**。原厂因为良率保守而多 sweep 掉的块可以解锁；真缺陷的块清不掉那些位，硬清 = 上电即坏。所以解锁收益的上限由 die 本身决定，不由脚本决定。

熔丝分四个区：

| 区 | 行 | 性质 |
|---|---|---|
| Regular | 0–221 | 直接编码 option words，带 PRI/RED 冗余；行 0/1 是"控制行"（安全敏感 option words）。烧了就基本没有恢复手段 |
| FPF | 96–221（Regular 的子集） | 唯一在生产卡上允许烧的区，内容是微码版本水印一类 |
| **FF（fuseless fuses）** | 222–486 | 不是"一条熔丝对应一个选项"，而是一段**重放的脚本**：每条记录给某条链的某个 16 位块赋新值。我们要解锁的东西几乎全在这里 |
| IFF | 487–511 | 另一种格式的在线改写指令，生产卡通常不可达；未枚举态下读回是锁存噪声 |
| RIR | 512 行之外的 8 行 | 熔丝 IP 供应商提供的"修复"指令，8 GB SKU 上 16 条里 15 条是空操作 |

### 1.2 FF 记录：追加即覆盖，所以只能清位

每条 FF 记录是一个 32 位字：`[4:0]` 选链，bit5 区分 FF/IFF（FF 必须为 0），`[15:6]` 选 16 位块，`[31:16]` 是该块的新值。控制器把 FF 区当成脚本**按行序重放**，同一个 `chain.block` 后写的记录覆盖先写的。物理熔丝一颗也不会被"复原"，变的是重放出来的 option word。

由此得到两条纪律（`crosscheck` 的输入）：

- 某块的有效值是重放的结果，追加一条记录只能让位**变少**，不能凭空多出一位。这就是 `new & ~old == 0`：计划的 `old`/`new` 是 option word 的块值，不是行内容；越写越严，所以"解锁"必须一次算准，把 `_DISABLE` 掩码里那几位一次清到 `_DEFECTIVE`。
- FF 区可以反复追加，但**行是不可复用的一次性资源**（空白行读回 `0x00000000`，写过就不是空白）。行号由 ffcalc 按这张卡的现场算，不许照抄别人的行号。

### 1.3 平时为什么烧不了 FF

生产卡的 option word `SBFE`（`OPT_SECURED_BURN_FEATURE_ENABLE`）= 1，含义是"只有 FPF 区允许 PROGRAM"。FSM 状态、`CMD_PROGRAM` 的行为在 FF 区与 FPF 区完全一样，写完也确实回 IDLE——**唯一不同的是 FF 区的写入不落地**。这就是很多人对着 FF 区反复烧、每次都"成功"、重启后什么都没变的原因。

要烧 FF，必须让 `SBFE` 读作 0。

### 1.4 毛刺改的是 latch，不是熔丝

上游给出的（也是本台架实测一致的）模型：加电或 `PEX_RST` 释放后，熔丝控制器按阶段启动——sense RIR → sense 控制行 → **把控制行 latch 成 option words** → sense 其余行 → latch 常规行 → 解析 FF 链 → latch FF 值。熔丝是按行整排 sense 的，攻击者很难操纵单颗熔丝的读值，但可以在这几个 latch 时刻用欠压让整批读值出错。

于是毛刺打的是**采样/锁存**，不是熔丝本身：Goldilocks 状态下行 0/1 的原始内容逐字节不变，只是它们派生出的 option words 全成了 0。三个实测层次：

| 故障态 | 现象 | 可用性 |
|---|---|---|
| 欠压不够（`under`） | option words 全是融合值，`SBFE=1` | 什么都没发生（空发） |
| **Goldilocks** | 行 0/1 派生的 option words 全 0：`DSOV=0`、`ENABLE_FUSE_PROGRAM=0`、priv mask `0xffffffff`；而 `CRC_STATUS=0x55`（镜像本身完好）、PCI ID 仍是 `20c2`、BAR0 与 PRI 环可用 | 可烧 FF（见 §1.5） |
| 过深（`over`） | 几乎所有 option words 读 0，`CRC_STATUS=0x00`（尚未解析），PCI ID 退成基础值 `2080`，PRI 环被复位；有趣的是这种卡往往原生起 Gen4 | 死路：控制器不可达或已废 |

`FUSECTRL=0xBADFxxxx` 是第四种形状：控制器锁死，之后**再读一次 BAR0 都可能把 PCIe 上游挂死**（§5 红线 1）。

Goldilocks 里 `ENABLE_FUSE_PROGRAM=0` 看起来是自相矛盾的：熔丝编程被自己的 canary 关掉了。上游把它解释成一种毛刺检测 canary——整行 sense 出错时，最安全的做法是把最敏感的功能关掉。

### 1.5 关键的一步：`CMD_SENSE`

写不进去，是因为 `ENABLE_FUSE_PROGRAM` 是不可 shadow 的 option word；而 shadow 别的 option word 需要 `DSOV=0`（`OPT_DISABLE_SW_OVERRIDE`，正常为 1，它挡住 `EN_SW_OVERRIDE`，而后者才能开启 option word shadow 通路）。看起来卡在两难里。

出路是发一次 `CMD_SENSE`（熔丝控制器重新 sense + 重新 latch）：**控制行派生的 option words 会回到融合值，唯独 `DSOV` 保持 0**。于是 `ENABLE_FUSE_PROGRAM` 恢复为 1（融合值），`DSOV` 仍是 0，此时把 `SBFE_SHADOW` 写 0 即可解锁 FF 区。最后一个障碍是 `FUSEWDATA` 的 PLM 掩码：同样在 `DSOV=0` 下把 `WDATA_WR_SECURE` 写 0，`PLM_FUSE` 自动全开（不要直写 PLM 寄存器）。

本仓库的烧写断言序列（§2.6）就是这段因果的直译：`CMD_SENSE` → 断言 `DSOV` 仍 0 → 写 `SBFE_SHADOW=0` 与 `WDATA_WR_SECURE=0` → 七项授权断言 → `FUSEADDR`/`FUSEWDATA`/`CMD_PROGRAM` → 独立重读。`EFP_STATUS` 冷启后读 0、发过一次 `CMD_SENSE` 才为 1，也是同一段因果的证据。

### 1.6 本台架与上游的注入方式差异

| | 上游（kate / pico-ga100vfi） | 本仓库 |
|---|---|---|
| 注入点 | 摘掉 PWMVID 上的 0 Ω 电阻，用 MCU 波形顶掉 GPU 自己发的 PWM，直接命令 MP2888B | 不动 PWM 走线，经 SMBus/PMBus 临时改 VRM 保护参数（`fast=1`），让 VRM 自己在启动瞬间把轨拉下去 |
| 时刻基准 | 以 NVVDD 越过 225 mV 为 t0，脉冲是 ms 级（实测命中波形：3 × 2 ms，落在 t0 后 ~980 ms） | 以板子自己拉的 `PEX_RST` 释放为锚，脉冲是 µs 级（3 × 222 µs，首凹在释放前 94 µs） |
| 一次重入启动的方式 | 整机 S3/S0（几秒），配一个 SBC 兜住起不来的主机 | 板子发一次 `PEX_RST` 就重新走一遍启动窗，不需要重启主机 |
| 代价 | 每发都要重启，发数便宜、节奏慢 | 毛刺会把卡瞬断甚至直接离链，一个电源周期只能买到几发，卡死需人工断电 ≥30 s |

两者打的是同一个 latch，但**基准点、脉宽、深度全都不同**——这正是"坐标不迁移"的物理原因（§4）：可迁移的是"在控制行 latch 时刻打一串欠压脉冲"这件事，不可迁移的是你那一台机器的 latch 到底发生在何时、要多深。

### 1.7 窗口为什么易失、解锁之后还差什么

写窗口是 latch 出错的结果，不是状态改变：任何一次重新 latch 都会恢复融合值——`PEX_RST`、加载 `nvidia` 驱动、热重启都算，未观察到自然到期。所以计划、脚本、交叉验证必须在开窗之前全部就绪，而新熔丝要等 devinit 在 boot 时重算才生效：烧完必须冷启动验收。

FF 只解除 floorsweep。`nvidia-smi` 看到的几何、Gen4 链路属驱动层（FBPA_CFG 等配置字），用 cmpunlocker 一类补丁驱动完成；ECC/MIG/NVDEC/NVJPG 在上游文章里是"可解锁但未验证"，本仓库只验到 NVDEC/NVJPG/OFA/MIG 管道计数与 `SYS_PIPE`（§2.7）。上游另外记录的硬件条件——Gen4 x16 需要换耦合电容、NVLink 需要桥接 PCB 上缺件的 U11——属同一层，不在本仓库链路内。

---

## 2. 流程

### 2.1 准备

1. 按 §6 接线；刷入固件（§9 预编译镜像，或按 [docs/BUILD.md](docs/BUILD.md) 自建）。
2. 板子配网：SoftAP `GA100VFI` / `ga100glitch`，接入后用串口 `@W` 或 `http://192.168.4.1/` 的设置页改成自己网络；之后统一用板子的 LAN 地址。
3. 判决机部署（Windows 端一条命令，含落地自校验；三个变量必须显式给出）：

   ```bat
   set BENCH_HOST=<判决机IP> && set BENCH_USER=<用户> && set BENCH_PW=<口令>
   python tools/deploy_bench.py
   ```

   部署重装 `~/ga100vfi/{host,bench}`，断言红线判据在包里、`bash -n` 全过、`FUSECTRL` 四态在机器上归类正确。台架脚本不放 `/tmp`：判决机每次重启清空 `/tmp`，脚本丢失曾两次把战役预检卡在"依赖文件不见了"。
4. 连通性与版本检查（`crack.sh` 会拒绝版本或总线速率不符的板子）：

   ```sh
   curl -s http://<设备IP>/api/status | python3 -c 'import json,sys; print(json.load(sys.stdin)["net"]["build"])'
   bash bench/poison_check.sh                 # 只读一次 FUSECTRL
   BOARD=http://<设备IP> bash bench/ota.sh    # 刷板：另给 IMG= 与 EXPECT=，回读 net.build 自证
   ```

### 2.2 建立解锁状态

VRM 未解锁时每一发毛刺都在 `ensure_unlocked()` 门口返回，`PEX_RST` 不动，判决是对一张未被碰过的卡回 `under`（空发）。判据只看 `pwd`（`STATUS_CML(0x7E) & 0x08`）：

| 状态 | 现象 | 处置 |
|---|---|---|
| 无锁 | `pwd=1` | 直接开火，不再发 `u` |
| 故障锁 | `pwd=0`，`z` 能读到 | 先 `@w 25 03`（`CLEAR_FAULTS`）再 `u`，无需断电 |
| 总线无应答 | `z` 报 `ERR status read` | 查接线/地址/供电，与锁无关 |
| 真死 | 清故障后仍 `pwd=0` | 拔 12V + 3V3 ≥30 s |

`bench/crack.sh` 已内置该判定；手工核对用 `bash bench/console.sh z`。裸打之前不发 `w`：`w` 把 VRM 交回 PWM-VID，`VOUT_COMMAND` 被 GPU 接管后压制，窗口出不来。

### 2.3 打毛刺，命中 Goldilocks

```bash
# BOARD 未设则 crack.sh 直接退出 2，不会拿任何默认地址开火
python3 -m ga100vfi.shot_policy --recipe      # 先向规程要本次战役的坐标与节奏
bash $HOME/ga100vfi/bench/crack.sh            # 再开火：六道闸门 + 板载 campaign + 代理判决循环
```

**波形是配方，坐标是待寻量。** 固定不变的是波形：`3 × 222 µs`、`gap 150 µs`、锚点 `td=-40 vr=750 tr=-20`（首凹在 `PEX_RST` 释放前 94 µs，脉冲串在 0.9 ms 内结束，覆盖启动期熔丝采样窗）；`fast=1` 才真正改变 VRM 保护参数、轨道才真被拉下去，`fast=0` 是空 dip。随卡而定的只有坐标 `vd`/`delay`，所以上面这组档案坐标对新卡只是一个搜索起点，不是答案（§4）。

`vd` 以 PMBus 码写入，LSB 6.25 mV：`vd=334` 与 `vd=330` 是同一个码 53，写进 VRM 的电压相同；300..380 mV 合法区间实际只有 14 个可分辨档，搜索轴必须按码选格。

注意事项：

- 出现 `lost` 立即停火（控制器半死态，补一发实测得到 poison；停火命令必须在交回判决**之前**发出）；`FUSECTRL` 非 IDLE 立即停火并不再读 BAR0；判到 `over`/`hit`/`goldilocks` 在当次 poll 内叫停战役。
- 每个电源周期的第一发由预热发买掉（冷态第一发大概率只是把卡打离链），所以"第一发没反应"不含信息量。
- 网格必须 `max ≥ ndelays × batch`，否则整个网格塌在一个电压点上（`crack.sh` 开火前算并拒绝）。

### 2.4 窗口内取证（全部只读）

窗口是易失的：发 `r`（`PEX_RST`）、加载 `nvidia` 驱动、热重启都会关闭它。取证期间只允许 `CMD_READ`。判到命中的那一批**不再补发**，直接取证。

```bash
bash bench/window.sh                                  # classify / baseline / burn status / capture
python3 -m ga100vfi.burn --bdf "$BDF" capture -o rows.txt    # 独立重读；一次约 50 s ~ 2.5 min
python3 -m ga100vfi.burn --bdf "$BDF" capture -o rows2.txt
diff <(sort rows.txt) <(sort rows2.txt)                # 必须 0 差异
```

注意事项：两遍独立读不一致说明快照不可信（读早了与卡真的动了同形），不要拿它去算计划；IFF 区（487–511）在未枚举态读回的是锁存噪声，一切对账限制在 0..486。

### 2.5 生成计划，并用 ffcalc 交叉验证

四道闸门全部通过才允许进入 §2.6。ffcalc 与本仓库工具是两套独立实现，必须互相对账，不允许只信一套。整个 §2.5 已固化成一个只读入口：

```bash
DIR=$HOME/ga100vfi/logs/<本次>-window bash $HOME/ga100vfi/bench/planmig.sh
# 全只读，一发不烧。不给 DIR 就取最新窗口目录；SKIP_READ=1 复用本窗口已抓的产物重跑闸门；
# TWICE=1 在同一窗口里读两遍并逐字节自检（"两遍一致才认快照"）。
```

它依次执行：两条独立采集路径逐行对账（0..486 必须 0 差异）→ `ffcalc mergeff` 覆盖率 → `ffcalc gen`（`--mig` 出媒体引擎与 MIG 计划）→ `ffcalc sim`（必须 `SIMULATION OK`）→ `ffcalc verify`（必须 `OK: plan ... belongs to card`）→ `ga100vfi.burn crosscheck`（必须 `0 problems`）。手工等价命令：

```bash
cd /path/to/ffcalc
python3 -m ffcalc.capture --bdf "$BDF" -o card-dump.json
python3 -m ffcalc mergeff card-dump.json $HOME/ga100vfi/host/rows.txt   # 必须报覆盖完整
python3 -m ffcalc gen card-dump.json -o plan.json --mig
python3 -m ffcalc sim card-dump.json plan.json
python3 -m ffcalc verify plan.json --dump card-dump.json
cd $HOME/ga100vfi/host
python3 -m ga100vfi.burn --bdf "$BDF" crosscheck card-dump.json plan.json
```

`crosscheck` 独立复核：记录重编码 `record == (new<<16)|(block<<6)|chain` 且 IFF 位为 0；清位规则 `new & ~old == 0`（§1.2）；`old` 等于 0..486 按行序回放的值且新行号大于被覆盖行号；目标行为空白（空白读 `0x00000000`，不是 `FF`）且同一计划内 `chain.block` 不重复；烧后回放等于计划 `new`；现场再逐行 `CMD_READ` 仍为空白。

认卡优先用 UUID；fingerprint 是 `*_defective` 全字 + SBFE 位的哈希，**每次烧写都会改变它**，跨烧写认卡用去掉 SBFE 的 die 指纹。判决机上没有 nvidia 驱动，`ffcalc capture` 不写 fingerprint，需要 `tools/attach_identity.py` 补字段后 `verify` 才认卡——这不是形式，是"这张计划属于这张卡"的断言。

### 2.6 烧写（先 1 条，再其余）

入口 `bash bench/burn.sh`（`PLAN=`/`SNAP=` 指定，每步真写前等人确认，`YES=<行表>` 预授权）。它做的顺序与判据：

1. `wake` → 窗口判据 → 现场快照 → `crosscheck`；
2. **试烧 1 条**（用计划中的 `fbp_disable`/`rop_l2_disable` 记录）→ 独立重读全表 → 与烧前快照逐行 diff：判据是"**只有该行按记录改变**"，且 `CRC` 仍 `0x55`、`DSOV` 仍 `0`、`FUSECTRL` 仍 IDLE、无 `0xBADF…`；
3. **续烧其余** → 再独立重读 → diff：判据是"计划内的行全部按记录改变、计划外零改动"。核对判据不是"我烧的那几行变了"。

两条容易踩错的语义：

- **续烧的 crosscheck 基线必须是已落地之后的现场**（它要求"声称已续烧的行"在快照里就读作本条记录），而 **diff 的起点仍是烧前那份**。两者不能共用一份快照。已落地行由 `burn` 现场认出并跳过——FF 是追加 + 后写覆盖，该行已是计划终态。
- `burn` 在写之前现场回读计划涉及的每一行：读回既不是空白也不是本条记录的行直接拒绝（往这种行里 PROGRAM 是唯一救不回来的操作）。

单条记录的断言序列（`host/ga100vfi/burn.py`，ffcalc `burntool` 为等价实现；因果见 §1.5）：

```
0) 断言 FUSECTRL=IDLE, FUSEDEBUG=0, CRC=0x55, DSOV=0
1) CMD_SENSE(3)，轮询回 IDLE，断言 DSOV 仍 0
2) arm：SBFE_SHADOW(0x00821108)=0 → WDATA_WR_SECURE(0x0082048C)=0（PLM 自动全开）
3) 七项授权断言全齐
4) FUSEADDR=row（回读核对）→ FUSEWDATA(0x0082000C)=record → FUSECTRL=2
5) 轮询回 IDLE；0xBADF… 或超时即停
6) CMD_READ 该行，回读 == record；断言 CRC/DSOV 未变
```

`EFP_STATUS` 在刚进窗口时读 0，发过一次 `CMD_SENSE` 才为 1；`FUSEWDATA` 的"读回 0"只在第一条记录之前成立。中途出现 `0xBADF…` 或超时立即停手，未烧的行下次窗口继续。

### 2.7 冷启动验收

新熔丝只有 devinit 在 boot 时才会重算，因此烧完之后必须冷启动。`PEX_RST` 等价于完整冷启动（重启判决机即可，POST 拉一次 `PERST#`）；正式验收另做一次整机断电 ≥30 s。

冷启动后读 BAR0 之前先看配置空间 `Command`：没有驱动 claim 这张卡时 `Command=0x0000`（Memory Space Enable 未置），**BAR0 整片读回 `0xffffffff`**，形状与"卡掉了"不可区分。分岔点是配置空间答不答 `10de:20c2`：答 = decode 没开（`burn wake` 只写该卡 `Command` bit1），不答 = 掉卡。

```bash
PLAN=plan.json SNAP=card-dump.json FFCALC=/path/to/ffcalc bash bench/postboot.sh
```

三条判据：A 控制器状态健康（`CRC=0x55`、`FUSEDEBUG=0`、IDLE、无 poison、`DSOV=1`、`SBFE=1`、窗口按预期关闭）；B 重读 FF 区与烧前快照逐行比，差异恰好是计划内的行；C devinit 重算的 option 影子等于计划目标、计划未涉及的字段逐位不变，且用 ffcalc 独立重算的剩余计划为 **0 条**、引擎计数读成目标值（NVDEC/NVJPG/OFA/SYS_PIPE）。

注意事项：`SBFE 0→1` 是 boot 后的应有形状（也正是认卡时整串指纹变化的来源）；`tpc_gpc5/tpc_gpc7` 一类位会出现"status 层为 1、fused 层为 0"（原厂固件运行时自屏蔽），不补烧；引用 SM 数必须写清口径（option 影子口径与 `ffcalc analysis` 的缺陷位口径可以差 4）。

### 2.8 熔丝之外

FF 只解除 floorsweep。`nvidia-smi` 的几何显现与 Gen4 链路属驱动层，用 [cmpunlocker](https://github.com/amoghmunikote/cmpunlocker)（补丁驱动 + `CMPUNLOCKER_CARD_PROFILE=mixed`，Gen4 走其 `RMPcieLinkSpeed`/E1 重训）。上游参考卡从出厂 8 GB/Gen1 x4 到 80 GiB/126 SM/Gen4 x16 的完整过程与坑，见 [docs/CMP170HX解锁经验与踩坑总结.md](docs/CMP170HX解锁经验与踩坑总结.md)。

---

## 3. 判据与寄存器

BAR0 偏移，全部实机确认：

| 寄存器 | 偏移 | 说明 |
|---|---|---|
| `FUSECTRL` | `0x00820000` | 低 5 位写命令；FSM 状态在 `bits[20:16]`，IDLE = `0xE0040000`，PROGRAM 中可见 `0xE00C0000`，`0xBADFxxxx` = 控制器锁死 |
| `FUSEADDR` | `0x00820004` | 行号 0–511，读写行前写它，读完恢复 |
| `FUSEDATA` | `0x00820008` | 只读数据口（写这里静默丢失） |
| `FUSEWDATA` | `0x0082000C` | 记录写入口 |
| `FUSEDEBUG` | `0x00820060` | 必须为 0 |
| `CRC_STATUS` | `0x00820070` | `0x55` = 镜像有效（H0/H1 均通过）；烧写后由控制器自动重算 |
| `EFP_STATUS` | `0x00820078` | 冷启后读 0，`CMD_SENSE` 后为 1 |
| `DSOV_STATUS` | `0x00820084` | 即 `OPT_DISABLE_SW_OVERRIDE` 熔丝位的影子；Goldilocks 窗口内 = 0 |
| `NV_FUSE_OPT_PRIV_LEVEL_MASK` | `0x008200FC` | Goldilocks 下 `0xffffffff` |
| `SBFE_SHADOW` | `0x00821108` | `DSOV=0` 时可写；写 0 = 授权烧写；冷启恢复 1 |
| `WDATA_WR_SECURE` | `0x0082048C` | 写 0 → `PLM_FUSE` 自动全开（不得直写 `0x8200F4/F8/FC`） |

| 命令（写 `FUSECTRL` 低 5 位） | 值 | 实测行为 |
|---|---:|---|
| `CMD_READ` | 1 | 读 `FUSEADDR` 行到 `FUSEDATA`，数十 µs 回 IDLE；空白行读回 `0x00000000` |
| `CMD_PROGRAM` | 2 | 约 1 ms 回 IDLE |
| `CMD_SENSE` | 3 | 重解析 OTP→影子：EFP/SBFE 回融合值 1，`DSOV` 保持 0（§1.5）；**不刷新 option 影子寄存器** |

`FUSECTRL` 四态判据（`host/ga100vfi/classify.py: fusectrl_state()`，判决链、战役循环、台架脚本共用同一函数）：

| 读数 | 状态 | 处置 |
|---|---|---|
| `0xE0040000` | `idle` | 放行 |
| `0xFFFFFFFF` | `unreachable` | 判 `lost`，卡不在总线上；一发即停（停火与判决同一次交出），不补发 |
| `0xBADFxxxx` | `poisoned` | 停手，禁止再读 BAR0 |
| 其他（实测 `0x00000000`） | `unknown` | 停手；重启判决机即回 IDLE，无需断电 |

`unreachable` 必须先于 `poisoned` 判定：`0xFFFFFFFF` 的状态位同为 31。判决五档（`lost`/`goldilocks`/`hit`/`under`/`over`）的完整条件与含义见 [docs/UNLOCK_AND_POISON.md](docs/UNLOCK_AND_POISON.md)。

---

## 4. 随机预期

熔丝烧写是确定的，**打中窗口不是**。这一节决定所有搜索策略与预算预期；完整版见 [docs/UNLOCK_AND_POISON.md](docs/UNLOCK_AND_POISON.md)。

- **随机性分三类**：A 我方时刻不确定（计划步到达偏差实测 16..268 µs，宽于命中所需窗）——做成确定性；B 卡侧时刻不确定（OTP 采样在 `PEX_RST` 释放后多久，每卡不同、不可测）——当作每卡一个待扫常量；C 轨上真实效果不确定（同一命令因起始轨电压与环路状态产生不同结果）——只能逐发验证。
- **命中坐标不跨卡迁移**。本仓库三次命中落在三组互不相同的坐标（`326/54/3×222`、`372/64/2×222`、`380/84/1×222`），上游也明写"波形与响应都是 die-specific"。把别卡坐标当先验去做二分，是在错误的轴上搜索。
- **它是每发独立的小概率事件**。上游的经验值是数小时到 48 小时量级、按 die 不同；本仓库的一次直接样本是同一坐标当日 200 余发零反应之后，最后一批的第 2 发命中。预期单位是"一个下午"，不是"一次实验"。
- **进度只有一根指标**：`DSOV` 是否从 1 变成 0（"反应"）。`under`/`over` 的分布只说明边界位置。`over` 与 poison 之间没有可停靠的中间态，向深侧探索每一发都承担接近对半的毒化概率。
- **正解是钉住边界浅侧一步反复打**（A 类抖动使每发有效落点在分布里飘），而不是在每个新坐标上各打一发；但坐标本身零反应时，加发数不买命中，要换相位档。已知混现的坐标不再打。
- **致死是另一个独立概率**：维度是深度（PMBus 码）、累计欠压能量（`pulses × pw`）与相位，且按卡而异；杀死卡的那一发本身拿不到判决。一个电源周期能买到几发由卡的存活决定（板子到 `tries` 上限自动 park），卡死需人工断电 ≥30 s。
- **不要相信固定秒数的等待**：毛刺会让卡瞬断约 0.3 s 再自己回来；收尾那套写（一次故意的掉电再上电）只能打在"已启动完成"的卡上。台架等待的条件是"卡连续在场足够久"这个事件。

---

## 5. 安全红线

1. `FUSECTRL` 为 `0xBADFxxxx` 后不再读 BAR0：继续读会挂死 PCIe 上游、整机复位。判据链已固化：`classify()` 第一件事只读一次 `FUSECTRL`，命中即抛异常、停战役并退出，不存在"再读一次确认"。
2. `FUSECTRL` 非 IDLE 不发下一条命令。
3. 记录编码、清位规则、空白行三查缺一不烧。写错 `chain.block` 即永久改坏未知字段。
4. 永不直写 PLM（`0x8200F4/F8/FC`）；用 `WDATA_WR_SECURE=0` 让 PLM 自动全开。
5. 驱动加载或 `PEX_RST` 都会终结窗口：计划、脚本、交叉验证在开窗前全部就绪。
6. `new & ~old == 0`：FF 重放只能让 option word 的位变少。
7. 未解锁不开火（`pwd=0` 时 `PEX_RST` 不动，买到的是假 `under`）。
8. `writes_failed=1` 表示"根本没打出去"，不是"参数不对"。
9. 网格必须 `max ≥ ndelays × batch`，否则整个网格塌在一个电压点上（`crack.sh` 开火前算并拒绝）。
10. 一发 `lost` 即停火，停火先于交判决；`lost` 之后不许补发。
11. 判到 `over`/`hit`/`goldilocks` 立即停手只读，不"再打一发确认"。
12. 首次命中后先单烧 1 条并独立重读复核，再烧其余；已烧行不重写。
13. 恢复顺序：板子拉 `PEX_RST` → 重启判决机 → 拔 12V + 3V3 ≥30 s。

---

## 6. 硬件与接线

| 功能 | GPIO |
|---|---:|
| I²C SDA（→ VRM 控制器 MP2888B SMBUS） | 6 |
| I²C SCL | 7 |
| `PEX_RST`，开漏输出 | 10 |
| UART0 TX / RX（控制台，115200 8N1） | 43 / 44（S3 默认，未重映射） |

默认值就是本台架的实际接线，全部战绩、标定与时序数字都取自在用的一对引脚。**换接线改变的是总线而不只是引脚号**：1 MHz 基线在每次 bring-up 重新探测，因此接线后必须核对 `/api/status` 的 `board.baud` 仍为 `1000000`——`bench/crack.sh` 已把这条做成闸门（`EXPECT_BAUD`），因为波形配方是 1 MHz 的算术，速率降级会让每一发的沿宽与档案不可比。横幅也打印这两个引脚号：真正伤人的是"固件以为是一对、线在另一对"。SDA/SCL 接插件必须压实，总线"全死"最常见的原因是接触不良。

引脚定义集中在 [main/board.h](main/board.h)。内含编译期检查：0/3/45/46（strapping）、19/20（原生 USB）、26–32（SPI flash）、33–37（octal flash/PSRAM，需 `GA_ALLOW_OCTAL_PINS` 放行）一律拒绝；三个角色不得共用一个引脚。任意 ESP32-S3 开发板按上表接线、刷入固件即可。

两条硬件不变量：

- `PEX_RST` 是 1.8 V 网络，ESP32-S3 pad 是 3.3 V。该线只允许拉低或释放（高阻），任何情况下不得输出高电平、不得改成推挽驱动；固件中它终身固定为 `GPIO_MODE_OUTPUT_OD`。
- strapping 脚在上电或下载模式下被内部弱上拉拉高，不接 `PEX_RST`，也不接 SMBus。

GND：显卡与毛刺板之间直接接一根 GND 线；经主板/供电共地只作退路。

实拍（黄 = SDA，蓝 = SCL，红 = `PEX_RST`，U809/U810 区域）：

![焊接点位标注](docs/images/wiring-points.png)
![焊接点位原图](docs/images/wiring-points-closeup.png)
![接线后全图](docs/images/wiring-overview.png)
![桌面接线](docs/images/bench-setup.png)

---

## 7. 固件、控制台与 HTTP

构建见 [docs/BUILD.md](docs/BUILD.md)。`/api/status` 的 `build` 字段与启动横幅都取 `main/board.h` 的 `GA_FW_VERSION`；证明"板上跑的是哪份镜像"用该字段 + `uptime`（重启后应为个位数秒）。架构与协议细节（时间预算、串口词表、心跳载荷、错误处理）见 [docs/PORT_NOTES.md](docs/PORT_NOTES.md)。

BAR0 判决永远在插着目标卡的 Linux 主机本地完成（mmap `/sys/bus/pci/<bdf>/resource0`），ESP32 不搬运 BAR0 数据。由此得出分工：**能判的那一侧就是唯一有权开跑的那一侧**。

| 方式 | 命令通路 | 判决通路 | 适用 |
|---|---|---|---|
| A. 串口驱动 `python -m ga100vfi attempt/campaign --port …` | 主机经串口发 `u`/`g`/`w` | 同机打完一发后本地读 BAR0 分类 | 调试、首次打通 |
| B. 板载 campaign + `host/ga100vfi_agent.py` | 代理发 `@C start`；页面经 `GET /api/req` 递交开跑意图 | 代理读 BAR0 分类，经串口 `@V` 或 `GET /api/verdict` 交判决 | 长时间爬山、按批次自动跑 |
| C. 网页 | 只递交请求与「停止」，不直接开火 | 逃生舱按钮走 `/api/verdict` | 人在旁边看进度、临时手工一发 |

三条路径的判决都绑定 `session+attempt`，过期轮次返回 HTTP 409（串口回 `verdict stale`）；代理对未确认结果幂等重试。板载 campaign 在 settle 之后必然进入等待判决状态；等不到判决时结束本轮战役（`phase":"no-judge"`）而不是打下一个坐标——没人应答只有一个含义：读 BAR0 的那一侧不在了。

```sh
# A：--port 是 TTL 适配器设备名（Linux 常见 /dev/ttyACM0 或 /dev/ttyUSB0）
python -m ga100vfi campaign --port <串口> --bdf "$BDF" --adaptive
# B：未配网时 <设备IP> 是 SoftAP 出厂地址 192.168.4.1
curl "http://<设备IP>/api/campaign?op=start&vd=326&td=-40&vr=750&tr=-20&pw=222&gap=150&pulses=3&delays=54&batch=2&max=12&step=25"
sudo python3 host/ga100vfi_agent.py http://<设备IP> --bdf "$BDF"
```

| 端点 / 命令 | 用途 |
|---|---|
| `POST /api/cmd`（body=命令行文本） | `u` 解锁 VRM、`w` 回 PWM-VID、`r` PEX_RST 脉冲、`g <参数>` 单发、`@C …` campaign |
| `GET /api/status` | 板卡/网络/阶段/看门狗面包屑、`build`，以及 `peer` 块（判决方在线、快照年龄、开跑请求状态） |
| `GET /api/campaign`（`op=start|stop`/查询） | 直接开跑或停止：`vd/td/vr/tr/batch/max/minv/maxv/step/pw/gap/pulses/wait/delays` |
| `GET /api/req`（`op=start&…&seq=N`／`op=cancel`） | 页面递交开跑意图：参数原样交给判决方，板子不解析；10 s 无人领取即作废 |
| `GET /api/verdict` | 提交判决（携带 `session+attempt`，拒绝旧轮次） |
| `@V <session> <attempt> <判决词>`（串口） | 同上走串口；五个判决词与 HTTP 完全一致 |
| `@A [seq=N] [adopted=N] {json}`（串口） | 判决方心跳：存快照，回包带回页面留下的开跑请求。载荷必须是整包可打印 ASCII JSON 对象，超限或截断即整块拒收 |
| `GET /api/log?pos=` | 滚动日志（逐发物理证据：`writes_failed`、`slip_us`、`reset_slip_us`、timing 表） |
| `POST /api/ota` | 固件升级 |

---

## 8. 台架脚本

`bench/` 下，`python tools/deploy_bench.py` 装到判决机 `$HOME/ga100vfi/bench/`。用法与闸门见 [bench/README.md](bench/README.md)。`crack.sh` 开火前有六道门：固件版本、总线速率、等卡回 PCIe、config-only 探活 + 只读一次 `FUSECTRL`、锁态判定、网格几何；每道门各自对应一类无效发数。

| 脚本 | 作用 | 写硬件 |
|---|---|---|
| `crack.sh` | 开火入口：六道闸门 + campaign + 跨轮台账 | 是 |
| `run_crack.sh` | 薄壳：确认没有第二个进程碰卡，再交给 `crack.sh` | 是（转交） |
| `window.sh` | 命中后取证（classify / baseline / burn status / capture） | 只读（含 `classify` 固有的一次 DSOV 写） |
| `planmig.sh` | 窗口内计划生成 + 四道闸门交叉验证（只读；`SKIP_READ=1` 复用已有产物） | 只读 |
| `burn.sh` | 烧写入口：试烧一条 → 独立重读逐行核对 → 续烧其余，每步真写前确认 | 是（熔丝，不可逆） |
| `postboot.sh` | 冷启动后复核：`wake` + 控制器状态 + 与烧前快照逐行比 + 影子寄存器 + ffcalc 独立重算 | 只写 `Command` bit1 |
| `poison_check.sh` | 只读一次 `FUSECTRL` | 只读一个寄存器 |
| `card_probe.sh` | 卡在不在（含 `Command` 原始字与 memory decode 判读）、桥链路、残留驱动 | 只读 |
| `onscene.sh` | 现场快照：卡/板/战役/台账一次打全，用于报数与事后对质 | 只读 |
| `timeline.sh` | 板端逐发 timing 表（到达偏差与每笔事务时长的归因数据） | 只读 |
| `link_retrain.sh` | 对**桥**发 Retrain + rescan | 写桥，不碰卡 |
| `readtrip.sh` | 零发实验：重放判决链访问，判是否我们自己的读写打进 poison | 是（判决链原样） |
| `revive.sh` | 卡离链时先分"可叫回"与"只能断电"，先发一次 `PEX_RST` 再 rescan | 只碰复位与配置空间 |
| `mkledger.sh` / `backfill_verdicts.sh` | 跨轮台账 `(delay,vd,几何) → 判决` 与真值回填 | 否 |
| `host-down.sh` | 停火 + 留痕 + 关机（卡电来自判决机时即断卡电） | 关机 |
| `ota.sh` | 刷板并回读 `build` 自证版本 | 刷固件，不碰卡 |
| `console.sh` | UART0 控制台助手 | 取决于所发命令 |

一次写窗口的全部产物（快照、计划、crosscheck、逐行 diff、冷启动读数）落在判决机 `$HOME/ga100vfi/logs/<时间戳>-*/` 下，按运行目录组织；要留的证据从这些目录里取，仓库不存某一张卡的档案。

---

## 9. 仓库、构建与发布

| 路径 | 用途 |
|---|---|
| `main/` | 固件唯一维护源：命令、状态机、总线、网络、网页源文件 |
| `arduino/ga100vfi/` | Arduino sketch 与同步后的固件副本（`python tools/sync_arduino.py` 生成） |
| `host/` | 串口客户端、Linux 判决代理、`ga100vfi.burn` 集成烧写/核验工具、离线测试 |
| `host/samples/` | 参考卡快照 + ffcalc 计划（回归样例：17 条记录，行 304–320，`host/tests/test_burn.py` 直接核对） |
| `test/` | 本机 C 回归测试、外设替身、统一测试入口 |
| `bench/` | 判决机台架脚本（见 bench/README.md） |
| `tools/` | `sync_arduino.py`、`gen_webui.py`、`deploy_bench.py`、`gen_manifest.py`、`package_firmware.py`、`pack_src_tests.py`、`attach_identity.py`、`sniff_console.ps1` |
| `docs/` | 原理与判据、构建、架构与协议、数据局限、上游完整解锁参考 |
| `firmware/` | 本版预编译镜像与 `RELEASE.json` |
| `partitions.csv` | N16R8 的 16 MB Flash 布局，两个 3 MiB OTA 应用分区 |

```sh
python test/run_tests.py                 # 离线 C + Python 回归（无编译器时用 tools/pack_src_tests.py --run）
python tools/gen_webui.py                # 改网页后
python tools/sync_arduino.py             # 改 main/*.c|h 后必须执行
python -m pip install -e host
python tools/deploy_bench.py             # 部署判决机（含红线与版本一致性断言）
```

发布门禁（依次通过才算一版）：

```sh
python tools/sync_arduino.py
arduino-cli compile --fqbn "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi" --build-path build-arduino arduino/ga100vfi
python tools/package_firmware.py --prune     # 按 board.h 版本打包，写 firmware/RELEASE.json
python tools/package_firmware.py --check     # 发布的镜像必须来自当前固件源码
python tools/pack_src_tests.py --run         # OFFLINE REGRESSIONS PASSED
python tools/deploy_bench.py                 # 台架与红线断言
python tools/gen_manifest.py                 # 重新生成 MANIFEST.sha256
python tools/gen_manifest.py --check         # 清单必须覆盖当前跟踪文件
```

`firmware/RELEASE.json` 记录版本、镜像内构建串、每个镜像的 sha256、固件源码摘要与打包时的 git 状态；`--check` 用源码摘要拒绝"源码改了但镜像还是旧的"。`MANIFEST.sha256` 记录 git 规范化内容的哈希（不含自身），Windows 与 Linux 检出结果一致；新增/改名/删除文件后必须重生成，`--check` 会点名具体文件。

预编译镜像（Arduino-ESP32 3.3.11 / ESP32-S3，Flash 16 MB、OPI PSRAM）以 `firmware/RELEASE.json` 为准：app 镜像用于网页或 `POST /api/ota`；merged 镜像（bootloader + 分区表 + OTAboot + app）用于首次上板整片刷写到偏移 0。

```sh
esptool.py --chip esp32s3 -p <串口> -b 921600 write_flash 0x0 esp32-ga100vfi-<版本>-merged.bin
```

串口刷写有一条前置条件是实测出来的：本台架这块板的 USB 串口是 CH340，**没有接 DTR/RTS 自动复位电路**，`esptool` 连不上时报 `Wrong boot mode detected (0x8)`——必须人工把 `BOOT`（GPIO0）拉低再复位一次。板子陷入启动循环时（HTTP 起不来，OTA 无从下手），这是唯一入口。`tools/sniff_console.ps1 <秒数>` 抓 UART0 控制台。刷完必须核对 `/api/status` 的 `board.baud` 为 `1000000`，并核对横幅打印的引脚与线在同一对上。

---

## 10. 边界与已知问题

- `--port auto` 仅按已知 Espressif/Raspberry Pi USB VID 查找；外接 TTL 需显式指定串口。
- Linux BAR0 判决需在插有目标卡的主机运行并正确指定 BDF。
- 复位时间轴由软件调度；`slip`/`reset_slip` 报告执行偏差，不承诺确定性硬件时序（[docs/CALIBRATION.md](docs/CALIBRATION.md)）。
- campaign 运行中执行 `op=stop` 若恰逢毛刺执行中，存在停止竞态 → TWDT 复位（卡与板子自行恢复）。规避：等当前 attempt 判决出来再 stop；且 stop 只能打在"已启动完成"的卡上。
- 熔丝控制器的启动阶段模型是黑盒推断（无 RTL）：阶段划分由控制器状态、原始行可读性、option words 与 PRI 环可达性反推；本文按"可复现假设"使用它。命令值、空白行 = 0、寄存器表等实测结论同样来自有限样本。
- `/api/status` 的 `board.vout_uv` 取自 `STATUS_VOUT(0x0b)` 的 bit4，位位置无厂商文档佐证，两种解码相差 8 倍。**未证实**：只用于"VRM 是否应答"，不许当作 12V 轨电压的测量值引用。
- dip 内的 ADC 遥测按固定 500 µs 步长采样，不是逐点记录：`td`/`tr` 小于该步长时只能证明"这段窗口里采到过什么"，不能证明 dip 底部形状。
- 无驱动条件下，"熔丝真生效"的证据面只有 devinit 重算的 option 影子寄存器（§2.7 判据 C）；`tpc`/`pes`/`fbio`/`nvlink`/`sys_pipe` 那些位只能以 FF 行内容本身作证据。
- 上游记录的收益预期（8 GB SKU 约九成可到 80 GB、SM 常见 >120、约 1.2× 零售 A100）来自其样本，属 die 彩票；本仓库不担保，只担保"影子等于目标、剩余计划 0 条"这类可验证判据。

---

## 许可

本仓库代码与文档采用 **Unlicense**（见 [LICENSE](LICENSE)）：公有领域，不保留版权。引用的上游实现与外部研究（dalzyu/pico-ga100vfi、kate-land、amoghmunikote、hexkyz 等）版权归各自作者。文中提及的商标（NVIDIA、GA100、CMP 等，以及乐鑫/Espressif 相关标识）均为相应权利人财产，提及仅为说明用途，不构成背书或隶属关系。
