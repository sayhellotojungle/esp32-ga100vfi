# 0.2.0 测试记录

验证日期：2026-11-17。环境：WSL2 Ubuntu 22.04、Python 3.10.12、GCC 11.4.0；C 外设和 SDK 接口使用项目内替身。宿主机未检测到 Node 时跳过网页 JavaScript 语法检查（本次已跳过）。

最终执行命令：

```sh
python test/run_tests.py --sanitizers
```

**结果：退出码 0，全部通过。** C 测试启用 AddressSanitizer 和 UndefinedBehaviorSanitizer；本次设置 `ASAN_OPTIONS=detect_leaks=0`，因此不包含泄漏检测。

## 回归结果

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

## 工程一致性

- Arduino 目录的 19 个共享文件已同步并通过一致性校验。
- `webui.h` 与 HTML 源文件重新生成的内容一致。

## 验证边界

以上是宿主机上的源码、替身和协议回归，**不是 ESP32 目标实板验证**。`firmware/` 中的预编译镜像由 arduino-cli（Arduino-ESP32 3.3.11，FlashSize=16M、PSRAM=opi）交叉编译通过并附带，但本版未对该镜像做实板刷写回归；未执行真实总线/复位时序测量或目标卡验证。

独立定时器增强未交付。复位释放保留软件调度，记录实际操作时间及偏差；这些记录不能保证外部电气边沿的确定性。

SMBPBI 保留原有实验帧，没有新增未经证实的协议定义。离线错误路径测试通过，不代表目标卡支持该帧格式。

构建入口见 [BUILD.md](BUILD.md)，时序限制见 [CALIBRATION.md](CALIBRATION.md)。
