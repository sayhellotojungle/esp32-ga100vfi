# 构建与运行

本文给出可复现的构建入口。`firmware/` 附带 arduino-cli（Arduino-ESP32 3.3.11，`FlashSize=16M,PSRAM=opi`）交叉编译的 0.3.0 镜像；构建与验证范围见 [TEST_RESULTS.md](TEST_RESULTS.md)。

所有命令默认在项目根目录执行。

## 1. 工具链与版本

| 用途 | 要求 |
|---|---|
| 离线 C 测试 | 支持 C11 的 GCC 或 Clang；测试通过外设替身运行，不连接设备 |
| Python 工具及测试 | Python 3.10+；实际串口访问另外需要 `pyserial>=3.5` |
| ESP-IDF 固件 | 目标 API 基线为 ESP-IDF 5.3，芯片目标 `esp32s3` |
| Arduino 固件 | Arduino-ESP32 3.x，且其内置 ESP-IDF 必须满足 5.3+ 的 API 基线 |

本项目使用的 `esp_driver_i2c` 和 `i2c_device_config_t.scl_wait_us` 已在官方 5.3 API 文档中核对。此核对不替代完整编译，也不意味着所有 Arduino 3.x 版本都满足条件。切换 SDK 大版本时应重新编译并核对配置。

官方参考：[ESP-IDF 5.3 I²C](https://docs.espressif.com/projects/esp-idf/en/v5.3/esp32s3/api-reference/peripherals/i2c.html)、[Arduino-ESP32 发布说明](https://github.com/espressif/arduino-esp32/releases)。

## 2. 先运行离线测试

```sh
python test/run_tests.py            # 需要本机有 C 编译器
python test/run_tests.py --sanitizers
```

该入口编译并运行本机 C 测试，同时运行 Python 单元测试。C 测试用的是项目内的 RTOS/外设替身，不能证明真实驱动、Wi-Fi 或电气时序正确。测试环境与具体结果统一记录在 [TEST_RESULTS.md](TEST_RESULTS.md)。

没有 C 编译器的工作站（例如只装 Python 的 Windows）用同一条命令把测试输入打包送到有 gcc 的 Linux 机器（判决机）上跑，判据不变：

```bat
set BENCH_HOST=<判决机IP>
set BENCH_USER=<判决机用户>
set BENCH_PW=<判决机口令>
python tools/pack_src_tests.py --run
```

退出码即测试套件的退出码，可用于卡住发布。成功输出为 `OFFLINE REGRESSIONS PASSED`；`--sanitizers` 启用 ASan/UBSan（`ASAN_OPTIONS=detect_leaks=0`，替身故意保留伪 RTOS 句柄）。`python test/run_tests.py --python-only` 只跑主机端测试。

更新网页或固件源文件后执行：

```sh
python tools/gen_webui.py
python tools/sync_arduino.py
```

`main/` 是固件维护源。不要只修改 Arduino 目录中的副本，否则下一次同步会覆盖这些修改。

## 3. ESP-IDF 构建

先按 Espressif 的安装说明安装所选 SDK，并进入已加载 `export.sh` / `export.ps1` 的终端。

```sh
idf.py -B build-idf set-target esp32s3
idf.py -B build-idf build
```

默认配置文件只在生成配置时提供默认值。已有 `sdkconfig` 中的设置不会因为修改 defaults 就自动全部改变；请通过 `idf.py menuconfig` 核对配置，或使用独立的 SDKCONFIG 和构建目录。

本版默认使用 16 MB Flash 和项目根目录的 `partitions.csv`，含两个各 3 MiB 的 OTA 应用分区。沿用旧配置时，需核对 Flash 大小与自定义分区表，执行 `idf.py -B build-idf fullclean` 后重新配置、构建。首次切换到本版分区布局，需要通过串口完整刷写 bootloader、分区表和应用；仅 OTA 应用无法更改旧分区布局。

明确选择正确串口后，才运行刷写命令，例如：

```sh
idf.py -B build-idf -p <串口> flash monitor
```

串口参数按实际设备填写（Windows 形如 `COM7`，Linux 通常是 `/dev/ttyUSB0` 或 `/dev/ttyACM0`）。UART0 控制台为 115200、8N1。无自动复位线路的 TTL 适配器，需要按板卡自身的下载模式操作说明手动进入 bootloader。

## 4. Arduino 构建

先运行同步，再打开 `arduino/ga100vfi/ga100vfi.ino`。IDE 选项：

- Board：ESP32S3 Dev Module。
- Flash Size：16 MB；PSRAM：OPI PSRAM，对应 N16R8 模组。
- USB CDC On Boot：Disabled，使用本项目的 UART0 控制台。

同步脚本同时复制项目的 `partitions.csv` 到 sketch，供 Arduino 使用自定义分区布局。

Arduino CLI 的普通构建入口：

```sh
python tools/sync_arduino.py
arduino-cli compile --fqbn "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi" --build-path build-arduino --warnings all arduino/ga100vfi
```

上传必须使用与该次构建一致的板卡选项和正确串口：

```sh
arduino-cli upload -p <串口> --fqbn "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi" --input-dir build-arduino arduino/ga100vfi
```

上传命令不会自动编译，须确认指定目录来自本次成功构建。若当前 core 的选项名称不同，以 `arduino-cli board details --fqbn esp32:esp32:esp32s3` 输出为准；不得使用旧构建目录中的二进制代替重新构建。

CLI 参数参考：[compile](https://arduino.github.io/arduino-cli/1.4/commands/arduino-cli_compile/)、[upload](https://arduino.github.io/arduino-cli/1.4/commands/arduino-cli_upload/)。

### 4.1 发布打包与门禁

依次通过才算一版：

```sh
python tools/sync_arduino.py
arduino-cli compile --fqbn "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi" --build-path build-arduino arduino/ga100vfi
python tools/package_firmware.py --prune     # 按 main/board.h 的版本打包 firmware/，写 firmware/RELEASE.json
python tools/package_firmware.py --check     # 发布的镜像必须来自当前固件源码
python tools/pack_src_tests.py --run         # 判决机上 OFFLINE REGRESSIONS PASSED
python tools/deploy_bench.py                 # 三处版本号一致 + 脚本语法 + 红线防护在位
python tools/gen_manifest.py                 # 重新生成 MANIFEST.sha256
python tools/gen_manifest.py --check
```

`tools/package_firmware.py` 会拒绝两种情况：app 镜像里印出的构建串与 `main/board.h` 的版本号不符（说明编译的是旧目录），以及 `firmware/` 里的镜像与当前固件源码摘要不符（说明改了源码没重编）。`firmware/RELEASE.json` 保存版本、构建串、每个镜像的 sha256、源码摘要与构建时的 git 状态；`git.dirty` 为真表示打包时工作树相对记录的提交有改动，该镜像不应视为那次提交的可复现产物。

## 5. Python 主机包

建议在虚拟环境中安装：

```sh
python -m venv .venv
```

Windows PowerShell 激活：

```powershell
.\.venv\Scripts\Activate.ps1
```

Linux/macOS 激活：

```sh
. .venv/bin/activate
```

安装与帮助：

```sh
python -m pip install -e host
python -m ga100vfi --help
python -m ga100vfi attempt --help
python -m ga100vfi campaign --help
python host/ga100vfi_agent.py --help
```

`--port` 放在 `attempt`、`campaign` 或 `revert` 子命令之后；没有单独的 `status` 子命令。串口状态使用固件控制台的 `v`。`attempt`、`revert` 使用串口；`classify`、`baseline` 及主机 `campaign` 的判决部分依赖 Linux PCI sysfs/BAR0。

`ga100vfi_agent.py` 接受板卡 URL、`--bdf`、`--poll`、`--timeout`。它只处理明确处于 `awaiting_verdict` 状态的手动/主机模式自动流程，启动代理本身不会启动新流程。BDF 必须根据本机实际 PCI 设备确定；读取或修改 PCI 配置的权限由系统决定。代理与固件需一起升级到带 session/attempt 的协议版本。

## 6. 网页与 OTA

页面地址默认是 `http://192.168.4.1/`；也可使用串口日志打印的 STA 地址。网页内包含状态、日志、命令、流程参数、当前轮判决、Wi-Fi 设置和 OTA 入口。

OTA 需要设备上实际使用的分区表中存在可用的备用 OTA app 分区。本版 `partitions.csv` 提供 `ota_0` 和 `ota_1` 两个 3 MiB 应用分区；旧设备首次升级应先完成串口完整刷写。没有备用分区时接口会明确拒绝。

只上传该次构建产生的**应用固件**：Arduino 通常为 `ga100vfi.ino.bin`；ESP-IDF 为应用项目的 `.bin`。不要上传 bootloader、分区表或 merged image。应用文件还必须能放入备用分区，OTA 成功与实板运行正确是不同的验证项。

OTA 和 Wi-Fi 凭据写入必须等待硬件空闲、自动流程停止。接口忙时返回冲突，不会与正在执行的硬件命令并发写入。
