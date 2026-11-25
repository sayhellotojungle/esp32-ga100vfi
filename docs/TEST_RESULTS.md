# 测试记录

## 0.5.1 — 2026-11-23

0.5.0 第一次上板（OTA 到 `<设备IP>`）的结果就是本版的来源。上传那一步是成功的（`http 200 flashed, rebooting`），板子之后**再没回来**；本机 `<串口>`（CH340 → UART0，115200）抓到的横幅之后：

```text
esp32-ga100vfi 0.5.0: core1 executor, I2C 6/7 PEX_RST OD@10 bus=1000000 Hz reset=software
ESP_ERROR_CHECK failed: esp_err_t 0xb001 (ESP_ERR_HTTPD_HANDLERS_FULL) at 0x42006c2b
file: "…/arduino/ga100vfi/net.c" line 588  func: net_start_server
expression: httpd_register_uri_handler(s_httpd, &uris[i])
abort() was called at PC 0x4037c7f3 on core 1
```

即 0.5.0 的镜像每次启动都在开 HTTP 之前 abort，反复重启，HTTP 从未开台。镜像本身是对的（横幅已是 `0.5.0`、`I2C 6/7`、新增的 `judge host:` 帮助行都在），坏在 SDK 默认 8 条的 handler 表被第 9 条路由撑破。`bench/ota.sh` 按设计报了失败（`OTA 200 但板子 120s 没回来`）。

| 范围 | 命令 | 结果 |
|---|---|---|
| C 回归 | `python3 test/run_tests.py`（WSL Ubuntu 22.04，gcc 11.4.0） | **OFFLINE REGRESSIONS PASSED**；planner 335、protocol 316、peer 57、campaign 2227、runtime 55、console 66、pmbus 56×2、http **20** 场景（新增"表满时板子活得下来"），全部 0 failures |
| 固件构建 | `arduino-cli compile --fqbn "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi" --build-path build-arduino --warnings all` | 通过、无警告；app 899,914 B（比 0.5.0 少 8 B：`ESP_ERROR_CHECK` 换成一次 `console_printf`），全局变量 73,824 B |
| 台架脚本 | `bash tools/check_scripts.sh` | 13 个脚本 `bash -n` 通过，无 CRLF（含改了版本自证的 `bench/ota.sh`） |
| 版本一致性 | `tools/deploy_bench.py::check_versions()` | `main/board.h` / `bench/crack.sh` / `host/pyproject.toml` 均为 `0.5.1` |
| 发布门禁 | `python tools/package_firmware.py --prune` 后 `--check` | 见 `firmware/RELEASE.json` |

串口刷写的前置条件也在这次实测中显形：`esptool read_flash` 停在 `Wrong boot mode detected (0x8)`，即这块板（CH340）**不能靠 DTR/RTS 自动进下载模式**，必须人工按住 `BOOT` 再复位。也就是说板子在启动循环里时，唯一出路是人工进下载模式——`bench/ota.sh` 报的那句"准备 USB 兜底"背后是这一步。

待做（0.5.0 那份清单原样并到本版，一次做完）：

| 范围 | 判据 | 结果 |
|---|---|---|
| 板上自证 | 串口写入两个 OTA 槽后正常上电，`/api/status` 的 `net.build` = 刚编的那一次 | **已过**：`0.5.1 (Nov 23 2026 16:25:01)`、`nvs` 未动（`sta:true`，仍是原来那个 `<设备IP>`） |
| 不再重启 | 连续读 `/api/status`，`board.uptime_s` 单调增长；`/api/log` 无 `abort()` | **已过**：`boot:1`，uptime 19→54 单调增，日志尾只有横幅 |
| 速率自证 | `board.baud == 1000000`，横幅 `I2C 6/7` | **已过**：`baud 1000000`（`requested_baud` 同值）、横幅 `I2C 6/7 bus=1000000 Hz` |
| 新端点 | `/api/req` 存在；递交后 `peer.req` 变 `pending`，10 s 后变 `expired`；未领取可撤回、重复撤回 409 | **已过**：`request stored …; valid 10 s unless claimed` → `pending req_seq=1` → 11 s 后 `expired`；过期后撤回 409；`seq=2` 撤回 200（`request withdrawn`）、再撤回 409 |
| 串口判决路径 | `@A` 收快照并回 `peer stored seq=N req=…`；形状不对整块拒收；无战役时 `@V` 回 `verdict stale` | **已过**：形状两类各拒一次（`must be a JSON object` / `must be printable ASCII`）；`@V 1 1 under` → `verdict stale: no shot is waiting for that session/attempt`；页面留有 `seq=3` 时心跳回包 `req=3 op=start&vd=340&…` |
| 采纳优先于过期 | 心跳带 `adopted=N` 后 `peer.req` 变 `adopted` | **已过**：`req":"adopted","req_seq":9`，`info` 内嵌快照 |
| 无人判决停战 | 起一轮 `max=5` 且无人判决：`phase` 应为 `no-judge`、`timeouts=1`、`tries=1` | **待做**：这一条要烧一发，且发前必须核对 FUSECTRL（`poison_check.sh` 在判决机上），等判决机上线随第一批开火一起做 |
| agent 侧串口通道 | 判决机上线后按 PORT_NOTES 实现 `@V`/`@A` 与 preflight | 待做（本轮未动 host 代码）。本轮的替身心跳证明固件这一侧已经把接口留对了 |

### 这次换回来的三条台架事实

- 串口刷写的完整可行流程（0.5.0 那种"刷死"情况下唯一出路）：人工 `BOOT`+复位进下载模式 → `esptool --before no-reset` 读写 → 写完**人工上电**。`esptool` 那句 `Hard resetting via RTS pin...` 在这块板上是空话：`RTS→EN` 传不过去，所以它既不能自动进下载模式，也不能把人从下载模式里放出来。
- 这块板的 otadata 两条 seq 记录都过不了校验（一半写过、一半擦除态），**"当前引导哪个槽"无法从 otadata 判定**。所以恢复时直接把同一个镜像写进两个槽（`0x10000` 与 `0x310000`，`nvs` 不碰）：不管引导器挑哪个都是新镜像。代价是另一槽里的 `0.3.2` 退路没了（镜像在 git 里，随时能取回）。
- **控制台串口能吞下的速率远低于 115200 线速，单块约 100 B**（判据：串口回包 `peer stored` 与 `/api/status` 的 `peer.seq` 双证）。单块 47 B / 71 B / 107 B 十发十中；单块 185 B 只剩 1/10；同一条 185 B 拆成 92+93 B 两块时，间隔 5 ms 为 3/10、**间隔 40 ms 为 9/10**；两条 90 B 连发（合计 180 B 一次突发）为 11/20。两个常见猜测被对照排除：板子繁忙（每发期间并行拉 16 KB 的 `/api/log`）与安静时同样糟；泵任务从"每次抽 64 B、空转睡 10 ms"改成"抽 512 B、睡 2 ms"也没有移动台阶（`CONFIG_FREERTOS_HZ 1000`，2 ms 是真 2 ms）。所以限制在 RX 路径本身——Arduino 核心的预编译 `sdkconfig.h` 未定义 `CONFIG_ESP_CONSOLE_UART_RX_BUFFER_SIZE`。落到实现上就是 `docs/PORT_NOTES.md` 规则 4：心跳一次 `write()` ≤100 B，更长的分块并留 ≥40 ms。

### 0.5.2 真机自证（本机，卡不在环）

本板本轮只接本机：`I2C`（6/7）与 `PEX_RST` 均未接线，卡也不在环里。所以下面只覆盖"固件自己能保证的部分"，凡是需要总线上有东西应答的判据都留到有卡那次。

| 范围 | 判据 | 结果 |
|---|---|---|
| 板上自证 | OTA 后 `net.build` = 刚编的那一次 | **已过**：`0.5.2`，`bench/ota.sh` 按镜像文件名的版本比对 `[ok]`，3 s 回话 |
| 整行分派 | 同一套心跳重放 24 发：不得再出现任何硬件动作，一行错误不超过一条 | **已过**：`PEX_RST`、`unlock`、`no ack`、`released`、`glitch ERR` 全为 **0**（0.5.1 同一激励下各有若干次真执行）；错误条数与"未完整到达的行数"一致，一条被采纳（`peer stored`、`peer.present:true`、快照原样进 `info`） |
| 页面不可被心跳弄崩 | 畸形快照必须整块拒收；`/api/status` 必须始终可解析 | **已过**：6 种畸形/边界快照各拒各的（`must be one JSON object` 等）；含 `}{\"}` 的快照被正确转义回吐，连读三次 `/api/status` 均为合法 JSON（721 B） |
| 泵任务改动 | 不得让原有的发送方式恶化 | **已过（但也无效）**：≤107 B 仍 10/10，185 B 与 257 B 仍分别 1/10 与 0/10，台阶没移动——见上面那条台架事实。保留该改动是为了少几次系统调用与更低的首字节延迟，不是因为它改善了完整率 |

`board.boot` 从 2 变为 3，差的一次是本次 OTA 的正常重启；`campaign` 全零、`restore_ok:true`、日志无 `abort()`。另有一条自证手段的局限要记下：`net.build` 的时间戳来自未被重新编译的那个编译单元，增量构建时它可能停留原地（本次 app 已变大 16 B 而时间戳未变），所以"是不是刚编的这一次"要靠 app 哈希而不是时间戳。

## 0.5.0 — 2026-11-23

| 范围 | 命令 | 结果 |
|---|---|---|
| C 回归 | `python3 test/run_tests.py`（WSL Ubuntu 22.04，gcc 11.4.0） | **OFFLINE REGRESSIONS PASSED**；planner 335、protocol **316**、peer **57**（新套件）、campaign **2227**、runtime 55、console 66、pmbus 56×2、http **19** 场景，全部 0 failures |
| 页面脚本 | `node --check`（Windows 侧 node 22；WSL 无 node，回归脚本内该步跳过） | 通过（1 个 `<script>` 块） |
| Python 主机端 | `python -m unittest discover -s tests -p "test_*.py"` | **196 tests, 0 failed**（本轮未改 host 代码） |
| 台架脚本 | `bash tools/check_scripts.sh` | 13 个脚本 `bash -n` 通过，无 CRLF |
| 版本一致性 | `tools/deploy_bench.py::check_versions()` | `main/board.h` / `bench/crack.sh` 的 `EXPECT_FW` / `host/pyproject.toml` 均为 `0.5.0` |
| 固件构建 | `arduino-cli compile --fqbn "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi" --build-path build-arduino --warnings all` | 通过、无警告；app **899,922 B**（较 0.4.1 +7,410 B：peer 模块、两条串口命令的解析、更大的页面与状态缓冲），全局变量 73,824 B |
| 发布门禁 | `python tools/package_firmware.py --prune` 后 `--check` | 见下表与 `firmware/RELEASE.json` |

本轮没有真机验证：判决机 `<判决机IP>` 不在线，板子 `<设备IP>` 仍跑 `0.3.2`。因此 **0.5.0 的串口判决路径（`@V`/`@A`）只有离线模型跑过**，真机第一次开火前必须按下面两条自证。I²C 默认引脚自 0.4.0 起就是 GPIO6/7，本版不动接线：刷完先确认线在那对上，再核对速率即可。

| 范围 | 命令 | 状态 |
|---|---|---|
| 板上自证 | `BOARD=http://… IMG=firmware/esp32-ga100vfi-0.5.0-app.bin.zip EXPECT=<app sha 前缀> bash bench/ota.sh` | 待做 |
| 速率自证 | 冷启后读 `/api/status` 的 `board.baud`，并核对横幅 `I2C 6/7` | 待做，必须为 `1000000` |
| 新界面自证 | 打开页面：无心跳时「开始」灰掉并给出原因；`GET /api/req?op=start&…&seq=1` 后 `peer.req` 变 `pending`，10 s 后变 `expired`；`op=cancel` 返回 409（已无待领） | 待做 |
| 串口自证 | 在串口上发 `@A {"state":"idle"}` 应回 `peer stored seq=0 req=none`；`@V 1 1 under` 在无战役时应回 `verdict stale: …` | 待做（0.5.0 新路径，离线模型之外尚无证据） |
| 无人判决停战 | 起一轮 `max=5` 且无人判决：`phase` 应为 `no-judge`、`timeouts=1`、`tries=1` | 待做 |
| agent 侧串口通道 | 判决机上线后按 PORT_NOTES 实现 `@V`/`@A` 与 preflight | 待做（本轮未动 host 代码） |

## 0.4.1 — 2026-11-23

| 范围 | 命令 | 结果 |
|---|---|---|
| C 回归 | `python3 test/run_tests.py`（WSL Ubuntu 22.04，gcc 11.4.0） | **OFFLINE REGRESSIONS PASSED**；planner 335、protocol **302**、campaign **2212**、runtime 55、console 66、pmbus 56×2、http 17 场景，全部 0 failures |
| Python 主机端 | `python -m unittest discover -s tests -p "test_*.py"` | **196 tests, 0 failed** |
| 台架脚本 | `bash tools/check_scripts.sh` | 13 个脚本 `bash -n` 通过，无 CRLF |
| 版本一致性 | `tools/deploy_bench.py::check_versions()` | `main/board.h` / `bench/crack.sh` 的 `EXPECT_FW` / `host/pyproject.toml` 均为 `0.4.1` |
| 固件构建 | `arduino-cli compile --fqbn "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi" --build-path build-arduino --warnings all` | 通过、无警告；app **892,512 B**（较 0.4.0 −1,408 B，删掉的就是 SMBPBI 通路），全局变量 72,296 B；构建串 `0.4.1 (Nov 23 2026 13:41:16)` |
| 发布门禁 | `python tools/package_firmware.py --prune` 后 `--check` | `OK: 0.4.1 images … match 41 firmware sources`；app sha256 `2bd307eb42188680…`、merged `a9cd049e19ff400c…`、源码摘要 `803b8b101bf7f979…` |
| 判决机上的同一套 C 回归 | `python tools/pack_src_tests.py --run` | 本轮未跑：判决机 `<判决机IP>:22` 不可达（卡与机器都在等一次断电），改在 WSL 的同版本 gcc 上跑，见上表 |

**待办**（与 0.4.0 相同，均要求先动硬件）：

| 范围 | 命令 | 状态 |
|---|---|---|
| 板上自证 | `BOARD=http://… IMG=firmware/esp32-ga100vfi-0.4.1-app.bin.zip EXPECT=2bd307eb42188680 bash bench/ota.sh` | 待做。**前置条件**：SMBus 线在 6/7（0.4.0 起的默认引脚，见 [README §4](../README.md)），否则刷上去的镜像读不到 VRM |
| 速率自证 | 冷启后读 `/api/status` 的 `board.baud` | 待做，必须为 `1000000`（`crack.sh` 的 `EXPECT_BAUD` 闸门） |
| 新界面自证 | 网页起一轮 campaign，确认无 `mode` 字段、等待期出现手动判决按钮 | 待做；`curl "$BOARD/api/campaign?op=start&mode=0…"` 必须回 400 |

## 0.4.0 — 2026-11-21

| 范围 | 命令 | 结果 |
|---|---|---|
| Python 主机端 | `python -m unittest discover -s tests -p "test_*.py"`（Windows 开发机，Python 3.13） | **196 tests, 0 failed**（0.3.1 基线 172；新增续烧、快照差分、memory-enable、台架脚本形状四组） |
| 台架脚本 | `bash tools/check_scripts.sh` | 13 个脚本 `bash -n` 通过，无 CRLF |
| 版本一致性 | `tools/deploy_bench.py::check_versions()` | `main/board.h` / `bench/crack.sh` 的 `EXPECT_FW` / `host/pyproject.toml` 均为 `0.4.0` |
| Arduino 镜像源 | `python tools/sync_arduino.py` | 19 个源同步，`GA_FW_VERSION "0.4.0"`、`PIN_I2C_SDA 6` / `PIN_I2C_SCL 7`；编译期引脚检查按新默认值通过 |
| 固件构建 | `arduino-cli compile --fqbn "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi" --build-path build-arduino --warnings all arduino/ga100vfi`（arduino-cli 1.5.1 / Arduino-ESP32 3.3.11） | 通过，无警告新增；app **893,920 B**（程序空间 68%），全局变量 **72,304 B**（22%）；构建串 `0.4.0 (Nov 21 2026 16:03:02)` |
| 发布门禁 | `python tools/package_firmware.py --prune` 后 `--check` | `OK: 0.4.0 images … match 41 firmware sources`；app sha256 `40272836e797c3ae…`、merged `4fd4f012f8db0a5f…`、源码摘要 `80ad27236a91685a…`；`git = {commit: 99c0df3, dirty: false}`（工作树干净，镜像可等同于该提交） |

| 发布工件 | `python tools/gen_manifest.py` | 清单覆盖 firmware/ 镜像与全部台架脚本；`gen_manifest.py --check` 通过 |

**待办**：

| 范围 | 命令 | 状态 |
|---|---|---|
| C 回归 | `python test/run_tests.py`（需 gcc） | 未跑；本轮未改 C 侧，仅改 `main/board.h` 的引脚与版本宏。判决机上用 `python tools/pack_src_tests.py --run` 补 |
| 板上自证 | `BOARD=http://… IMG=… EXPECT=40272836e797c3ae bash bench/ota.sh` | 待做。**前置条件**"先把 SMBus 线移到 6/7，否则刷上去的 0.4.0 读不到 VRM"至今有效：6/7 就是 0.4.0 起的默认引脚，对 0.4.0 之后每一个镜像都成立 |
| 速率自证 | 冷启后读 `/api/status` 的 `board.baud` | 待做，必须为 `1000000`；`crack.sh` 会把它当闸门（`EXPECT_BAUD`） |

0.4.0 之前：`firmware/` 只保留当前版本的镜像，`--prune` 已移除 0.3.1 的 zip；0.3.1 与 0.3.2 的镜像与板上自证记录在本文件下方。

## 0.3.1 — 2026-11-21

| 范围 | 命令 | 结果 |
|---|---|---|
| Python 主机端 | `python3 test/run_tests.py`（WSL Ubuntu 22.04 内的同一套） | 82 tests, 0 failed |
| C 回归（WSL Ubuntu，gcc 11.4.0） | `python3 test/run_tests.py` | **OFFLINE REGRESSIONS PASSED**；planner 335、protocol 300、campaign/oracle 2385、runtime 47、console 66、pmbus 52、pmbus_high_bank 52、http 17 场景，全部 0 failures |
| 固件构建 | `arduino-cli compile --fqbn "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi" --build-path build-arduino arduino/ga100vfi` | 通过；app 893,008 B（程序空间 68%），全局变量 72,008 B（21%）；镜像内含 `0.3.1 (Nov 21 2026 11:11:09)` 构建串 |
| 板上自证 | `BOARD=http://… IMG=… EXPECT=72d40a33f51444d9 bash bench/ota.sh` | `net.build = 0.3.1 (Nov 21 2026 11:11:09)`；控制器仍 IDLE、`FUSECTRL = 0xe0040000`（未受本轮刷写影响） |
| 默认总线速率 | 冷启后不发 `@F`，读 `/api/status` + `@r 25 7e 1` | `baud = 1000000`（默认即 1 MHz），`STATUS_CML(0x7E) = 0x00`，即时 ACK |
| 速率切换 | `@F 400000` → `@r 25 8b 2` → `@F 1000000` → `@r 25 8b 2` | 两次读回同为 `c9 02`；降级/回升都不需要重启，最终 `baud = 1000000` |
| 发布门禁 | `python tools/package_firmware.py --prune/--check`、`gen_manifest.py`、`deploy_bench.py` | 镜像与 41 个固件源一致（sha256 `e599a0bff5ba9b0c…`）；清单与台架自检通过 |

降级路径（1 MHz NACK → 400 kHz → 100 kHz）只有 `test/test_pmbus.c` 的替身用例覆盖：实卡上总线在 1 MHz 一直 ACK，没有真实降级事件可测。

## 0.3.0 — 2026-11-20

| 范围 | 命令 | 结果 |
|---|---|---|
| Python 主机端 | `python -m pytest host/tests -q`（Windows）／`python3 test/run_tests.py --sanitizers` 内的同一套（判决机） | 67 passed, 5 subtests passed, 0 failed（两侧一致） |
| C 回归（判决机 Ubuntu，gcc 15.2.0，ASan+UBSan） | `python tools/pack_src_tests.py --run` | **OFFLINE REGRESSIONS PASSED**；planner 335、protocol 300、campaign/oracle 2385、runtime 47、console 66、pmbus 42、pmbus_high_bank 42、http 17 场景，全部 0 failures |
| 固件构建 | `arduino-cli compile --fqbn "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi" --build-path build-arduino arduino/ga100vfi` | 通过；app 892,624 B（程序空间 68%），全局变量 72,000 B（21%）；镜像内含 `esp32-ga100vfi 0.3.0` 横幅与 `0.3.0 (Nov 20 2026 12:37:25)` 构建串 |
| 板上自证 | `bash bench/ota.sh`（`IMG=… EXPECT=6dca261e2e494173`）→ `/api/status`、`/api/log` | `net.build = 0.3.0 (Nov 20 2026 12:37:25)`，横幅 `esp32-ga100vfi 0.3.0`，`uptime` 个位数秒 |
| Arduino 同步 | `python tools/sync_arduino.py --check` | 19 个共享文件一致 |
| 台架部署自检 | `python tools/deploy_bench.py`（判决机） | 三处版本号一致；9 个脚本 `bash -n` 全过；`FUSECTRL` 五值四态归类正确；两种危险态下 `classify()` 拒绝写 `DSOV_SET` |
| 开火闸门 | `BOARD=http://127.0.0.1:9/ bash bench/crack.sh` | 在等卡之前退出 3（固件版本闸门拦住）；板子在线时 `net.build` 首词 = `0.3.0`，放行 |
| 实卡 | 见 [../README.md](../README.md) §7 与 [UNLOCK_AND_POISON.md](UNLOCK_AND_POISON.md) §六 | 命中 Goldilocks → 16 条 FF 记录烧入 → 冷启 14/14 影子寄存器等于计划目标 → 重扫 0 条 |
| 发布门禁自检 | `python tools/package_firmware.py --check`、`python tools/gen_manifest.py --check` | 见下方注入表 |
| 台架脚本参数化 | WSL bash：`bash -n` + 缺 `BOARD` 干跑 + 伪造部署目录跑 `mkledger.sh` | 全部脚本语法通过；`crack.sh`/`ota.sh` 缺 `BOARD` 退出 2（在碰硬件之前）；`GA_BASE` 缺省 `$HOME/ga100vfi` 生效；`mkledger.sh` 对新命名与历史命名两类台账目录各命中 1 发，零命中时退出非零 |

Windows 工作站无 C 编译器，C 回归由 `python tools/pack_src_tests.py --run` 一条命令完成：打包 `main/ test/ host/ tools/ arduino/ partitions.csv`，上传判决机，干净目录解包后执行 `python3 test/run_tests.py --sanitizers`，退出码即套件退出码。

上表「台架部署自检」与「开火闸门」两行是判决机上的实跑结论。台架脚本改为占位符与 `GA_BASE`/必填报参之后，判决机与本仓开发板暂时离线，这两项的机器上复跑留到回线后补做；静态侧由上一行的 WSL 检查覆盖。

门禁自身的验证（两个脚本都是"拒绝发布"的那一道，所以它们误报和漏报都算缺陷）：

| 注入 | 期望 | 实测 |
|---|---|---|
| 改 `main/board_runtime.h` 一行注释后 `package_firmware.py --check` | 退出 1，提示源码已变、需重编 | 退出 1，`published … now …` 摘要不同 |
| 同一文件在 LF/CRLF 间来回翻转 | 不应报错（换行符不是内容） | 两次均退出 0，摘要不变 |
| 跟踪文件加一行 / 改名 / 新增未入清单的文件 | `gen_manifest.py --check` 退出 1 并点名 | 分别报 `[changed]`、`[removed]`、`[new]`，各 1 项 |
| 恢复原状后 `--check` | 退出 0 | 131 个文件 current |

本轮修掉的测试缺陷：`test/test_main.c` 的控制器替身不保存寄存器内容，`pmbus_r()` 对任何地址回 0，于是固件新增的 config-id 回读校验（`0x46` 必须读回 `29 12`）在测试里必然失败，runtime 套出现 8 处 FAIL。替身现在带寄存器文件（只记录被接受的写入）并新增 `cfg_devinit_locked` 模式（写 ACK 但保持 `0x1228`）。runtime 套由 39 项增至 47 项，新增断言：一次成功发射后 `regs[0x46]==0x29 && regs[0x47]==0x12`；`0x46` 写被拒或回读不符时不发 `PEX_RST`。变异验证：删去 `ensure_unlocked()` 中的回读值比较（`main/main.c:211`）→ runtime FAIL 142；把 `STATE_UNKNOWN` 改成 `STATE_IDLE` → Python 侧 8 处 FAIL。

`host/tests` 在 Linux 与 Windows 均可运行；`gpu_verdict()` 写 `/sys/.../power/control` 属 best-effort，异常按 `OSError` 吞掉。

## 0.2.0 — 2026-11-17

验证日期：2026-11-17。环境：WSL2 Ubuntu 22.04、Python 3.10.12、GCC 11.4.0；C 外设和 SDK 接口使用项目内替身。宿主机未检测到 Node 时跳过网页 JavaScript 语法检查（本次已跳过）。

最终执行命令：

```sh
python test/run_tests.py --sanitizers
```

**结果：退出码 0，全部通过。** C 测试启用 AddressSanitizer 和 UndefinedBehaviorSanitizer；本次设置 `ASAN_OPTIONS=detect_leaks=0`，因此不包含泄漏检测。

## 0.2.0 回归结果

| 测试程序/范围 | 通过数量 |
|---|---:|
| planner | 335 项检查 |
| protocol | 300 项检查 |
| campaign / oracle | 2,385 项检查 |
| runtime | 30 项检查 |
| console | 66 项检查 |
| pmbus | 42 项检查 |
| pmbus_high_bank | 42 项检查 |
| http | 17 个场景，含 OTA 五个失败阶段 |
| Python 主机端 | 54 项测试 |

共运行 8 个 C 测试程序：前 7 个合计 3,200 项检查，HTTP 另有 17 个场景。回归覆盖命令边界、状态机、判决关联、失败恢复、串口分包、网络重试及外设错误路径。

## 0.2.0 工程一致性

- Arduino 目录的 19 个共享文件已同步并通过一致性校验。
- `webui.h` 与 HTML 源文件重新生成的内容一致。

## 验证边界

宿主机上的源码、替身和协议回归不等于 ESP32 目标实板验证。0.2.0 的预编译镜像当时未做实板刷写回归。

0.3.0 镜像（`firmware/esp32-ga100vfi-0.3.0-*.bin.zip`）由 arduino-cli（Arduino-ESP32 3.3.11，FlashSize=16M、PSRAM=opi）交叉编译，并经 `bench/ota.sh` 刷入本仓库开发板（N16R8，16 MB Flash、OPI PSRAM）后完成实卡解锁闭环；未执行示波器/逻辑分析仪级的时序与波形测量。

独立定时器增强未交付。复位释放保留软件调度，记录实际操作时间及偏差；这些记录不能保证外部电气边沿的确定性。

SMBPBI 保留原有实验帧，没有新增未经证实的协议定义。离线错误路径测试通过，不代表目标卡支持该帧格式。

构建入口见 [BUILD.md](BUILD.md)，时序限制见 [CALIBRATION.md](CALIBRATION.md)。
