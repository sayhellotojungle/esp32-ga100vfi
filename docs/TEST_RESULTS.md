# 测试记录

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
