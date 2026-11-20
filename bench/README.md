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
| `crack.sh` | 开火入口：固件版本闸门 + 等卡 + config 探活与 `FUSECTRL` 危险态 + 锁态 + 网格几何，共五道闸门，然后跑板载 campaign 并落跨轮台账 | 是 |
| `window.sh` | 命中后取证：`classify` / `baseline` / `burn status` / `capture`；第 0 步先问 `FUSECTRL` | 只读（含 `classify` 固有的一次 DSOV 写） |
| `poison_check.sh` | 只读一次 `FUSECTRL`，给出恢复判据与处置顺序 | 只读一个寄存器 |
| `card_probe.sh` | 设备在不在、桥链路宽度、残留驱动 | 只读 |
| `link_retrain.sh` | VRM 是否应答 + 对**桥**发 Retrain + `pci rescan` | 写桥，不碰卡 |
| `readtrip.sh` | 零发实验：不打的条件下重放判决链访问（含连写三次 `DSOV_SET`），判是否我们自己的读写打进 poison | 是（判决链原样） |
| `mkledger.sh` | 汇总 `logs/*-crack*/agent.log` → `logs/ledger.txt`，输出 `(delay, vd) → 判决` 总表 | 否 |
| `ota.sh` | `BOARD=http://<设备IP> IMG=… [EXPECT=<sha前缀>] bash ota.sh`；推送后回读 `/api/status` 的 `build` 并要求等于期望值，缺失即退出 4 | 刷固件，不碰卡 |
| `console.sh` | UART0 控制台助手（板端 `/api/log` 只有开机横幅，`@r`/`@w` 的答复只能从串口取） | 取决于所发命令 |

## 开火

```bash
BOARD=http://<设备IP> MAX=12 BATCH=2 VD=334 MINV=284 MAXV=384 STEP=25 DELAYS=54 \
  bash $HOME/ga100vfi/bench/crack.sh
```

默认波形即档案命中几何：`3 × 222 µs`、`gap 150 µs`、`delay=54`、`td=-40 vr=750 tr=-20`。搜索几何（`VD/MINV/MAXV/STEP/BATCH/MAX/DELAYS`）可以调；波形几何只在 `under`/`over` 判据要求时改，并需留下比档案更强的证据。单发 `pw=15000` @ `vd=300` 按档案定义属 over（欠压横跨整个启动窗），已从默认值移除。

台账目录名自描述（固件版本 + 波形几何 + 电压），因为跨轮复盘依赖目录名回答"哪版固件、什么波形、几发"。

预检拒绝开火的四种情况：

1. `FUSECTRL` 为 `0xBADF…` 或叫不出名字的值 → 绝不进判决链（poison 态读 BAR0 = 整机复位，事故 `logs/02`）；
2. `pwd=0` 且清故障后仍 `pwd=0` → 此时每发都在 `ensure_unlocked()` 门口返回，`PEX_RST` 不动，买到的是假 `under`；
3. `max < ndelays × batch` → 整个网格塌在一个电压点；
4. 等不到卡（`WAIT_CARD` 秒内配置空间不可达）→ 先按下面的顺序分清是哪一种。

## 卡不见了（按顺序，不许倒）

```bash
bash bench/card_probe.sh       # 设备是否还在、桥链路宽度、残留驱动
bash bench/link_retrain.sh     # VRM 是否应答；对桥发 Retrain + rescan
```

判据（实测）：VRM 应答 + 板子 `phase=idle busy=false` + 桥发不出链路 = 卡 PHY 未走 POR，热重启不清它，拔 12V 与 3V3/VDD33 ≥30 s。

`FUSECTRL` 只是 poison 而设备仍在：先重启判决机（POST 拉一次 `PERST#`，实测足够），仍不行再断电。

## 固件版本自证

版本号定义在 `main/board.h` 的 `GA_FW_VERSION`，启动横幅与 `/api/status` 的 `net.build` 共用该宏；`GA_BUILD_STAMP` 记录编译时刻，用于区分同版本号的多次构建。

三处断言构成链条：

1. `tools/deploy_bench.py` 部署前断言 `main/board.h`、`bench/crack.sh` 的 `EXPECT_FW` 默认值、`host/pyproject.toml` 三处版本一致，并断言部署包含 `EXPECT_FW` 闸门；
2. `ota.sh` 要求镜像 sha 前缀等于 `EXPECT`，刷完回读 `net.build`，字段缺失即退出 4（用法 `BOARD=http://<设备IP> IMG=… EXPECT=<sha前缀> bash bench/ota.sh`）；
3. `crack.sh` 开火前要求 `net.build` 首词等于 `EXPECT_FW`（当前 `0.3.0`），不等即退出 3。

`bench/crack.sh` 的日志目录名取 `build` 首词，因此跨轮台账能回答"这一轮是哪版固件"。绕过闸门（`EXPECT_FW=any`）的代价是这轮日志不可追溯。

当前板端实测：横幅 `esp32-ga100vfi 0.3.0`，`net.build = 0.3.0 (Nov 20 2026 12:37:25)`，app 镜像 `892,624 B`、sha256 前缀 `6dca261e2e494173`。
