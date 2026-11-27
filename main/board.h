// Pin map for the ESP32-S3 glitcher — single source of truth.
//
// Every connected pin boots as INPUT with pulls DISABLED (see pins_safe_boot):
// some ESP32 strapping pins keep a hardware weak pull-up that would feed
// 3.3 V into the card's 1.8 V nets, and a powered card back-drives an
// unpowered MCU through the pads' ESD diodes if we present anything else.
//
// PEX_RST is configured GPIO_MODE_OUTPUT_OD for its whole life: driving a 1
// on an open-drain pad is hi-Z, so no code path can ever put 3.3 V on the
// 1.8 V reset net. Never route it to a strapping pin.
#ifndef BOARD_H
#define BOARD_H

#include "sdkconfig.h"

/* Build knobs work on both targets: standalone IDF builds get CONFIG_GA_*
 * from Kconfig; the Arduino sketch (core 3.x bundles IDF 5.5 headers but no
 * GA100VFI Kconfig) falls back to these defaults. */
#if defined(CONFIG_GA_I2C_BAUD_HZ)
#define GA_BAUD_HZ CONFIG_GA_I2C_BAUD_HZ
#else
#define GA_BAUD_HZ 1000000
#endif

/* Hard ceiling on how long PEX_RST may sit low, enforced in firmware.
 *
 * 'j'/'J' is an unbounded manual hold, and the reset that each shot's timeline
 * performs is only ~1 ms, so anything approaching this number is a forgotten 'j'
 * -- not a plan. Measured consequence on 2026-11-21: a 10 s manual hold left the
 * card unreachable and the box needing a power cycle. The guard task in main.c
 * releases the pad itself once this much time passes and prints why, so "never
 * hold reset for long" is a property of the build, not of who typed the command. */
#ifndef GA_PEX_MAX_HOLD_MS
#define GA_PEX_MAX_HOLD_MS 3000
#endif

// SoftAP fallback: always up, so the board is reachable even before any
// station network is configured (http://192.168.4.1/).
#define GA_AP_SSID "GA100VFI"
#define GA_AP_PASS "ga100glitch"
/* 版本号只在这里定义一次；横幅与 /api/status 都取这两个宏。
 * 编译时刻单独成串，是为了能区分同版本号的不同构建。
 * 0.3.2: 每步实际执行时刻 + 时长打进日志（dip 的尾沿和被 1 MHz 阻塞写拖走的释放沿，
 * 是今天 0/60 发不命中的真正原因），并且 PEX_RST 手动保持超过
 * GA_PEX_MAX_HOLD_MS 会被固件自己放开（忘记的 'j' 和卡死了长得一模一样）。
 * 0.4.0: I²C 引脚改为 GPIO6/7（见下面的接线说明）。波形、时序、遥测字段与 0.3.2 完全
 * 一致，因此 0.3.2 攒下的战绩与标定对本版仍然有效；本版号升 minor 只因为**接线变了**——
 * 拿旧版说明书接线的人会在总线上拿到 NACK，这件事必须从版本号上就能看出来。
 * 0.4.1: 删除板载 SMBPBI 判决（旧 mode 0）。本台架对该端点的四次读全部无回执
 * （docs/UNLOCK_AND_POISON.md §一），板子看不见这颗 GPU；留着这个模式只会让一轮战役
 * 在"每一发都由一条本板不存在的通路判成 lost"的状态下跑完。波形、时序、遥测与 0.4.0
 * 一致；对外可见的改动是 `/api/campaign` 不再接受 `mode=0`（`mode=1` 仍接受以便旧脚本
 * 继续工作）、状态里不再有 `mode` 字段、串口不再有 `@P`/`@O`。
 * 0.5.0: 板子与判决方（主机 agent）之间的分工与协议定型，两处行为变化：
 *   - 一发无人判决即结束战役（旧行为是继续打下一个坐标，最多打到 `max`，等于让一张没人
 *     看过的卡挨最长两小时的盲打）。无人应答只有一个含义：读 BAR0 的那一侧不在了。
 *   - 默认 I²C 引脚回到 47/48，即本台架的实际接线；0.4.0 的 6/7 只是纸面更优、从未上板。
 * 新增：串口 `@V`（交判决）与 `@A`（心跳，回包带回页面留下的开跑请求）、`GET /api/req`
 * （网页递交开跑意图，10 s 无人领取即作废）、`/api/status` 的 `peer` 块。波形、时序与
 * 其余遥测字段与 0.4.1 一致。
 * 0.5.1: 修 0.5.0 的自锁——`max_uri_handlers` 用 SDK 默认的 8，而路由已是 9 条，注册失败
 * 撞上 ESP_ERROR_CHECK 直接 abort，板子反复重启且 HTTP 从不开台。现在表开到 16，注册失败
 * 只点名那条路由并继续跑：台架上"盒子不说话了"不该是加一条路由的后果。
 * 0.5.2: 串口命令改为整行分派（含单字符命令）。0.5.1 及之前，`used == 0` 时收到 `r`/`u`/`z`
 * 之类就当场执行，而判决方心跳是一段 JSON：一行没能完整到达时，载荷里的字母便落在"行首"，本台架
 * 上实测一行 49 B 的心跳换来 48 行错误，并且 `r` 真的执行了一次 PEX_RST 脉冲（当时那两根线
 * 没接，所以落在了空处）。现在杂散字节只会是一行错误，永远不会是一个动作。
 * 心跳的发送预算：一次 write() 不超过 100 B，更长就分块并留 ≥40 ms——这条线的可用速率远低于
 * 115200 线速（单块 107 B 完整、185 B 几乎全废），测量记录见 CHANGELOG.zh-CN.md 的 0.5.2 一节。
 * 0.5.3: 毛刺看门狗不再咬掉整批。四次 `WATCHDOG reason=3 step=11 act=8 t=704us` 自复位都落在
 * 时间表的最后一笔写（毛刺后恢复 VOUT_MIN，act=8=GA_VMIN，t=704us 正是它）。原因是拉伸容忍：
 * `scl_wait_us=10 ms` 是**每根时钟线**的上限，而 20 ms 的传输超时只在事务边界检查，于是卡死
 * 的从机能让一笔 9 时钟的写拖到 250 ms 以上——照章重启，战役剩余的发数随之蒸发（2026-11-25
 * 三次，每次吞掉 7、46、24 发）。现在：时间表用的设备把拉伸容忍压到 2 ms（实测最坏单步 95 us，
 * 20 倍余量），并且时间序列期间总线若已掉线就直接判这一写失败，不再走波特率阶梯（那是唯一一
 * 条可能长过 250 ms 的路径）；收尾那 500 ms 睡眠照例喂心跳。看门狗保留原样：真卡死的写仍然
 * 在 250 ms 后被咬，只是现在被咬之前先记下真正的原因。
 * 顺带修留痕的谎话：`phase` 会在 esp_restart 落地前被应用核改回 PH_GLITCH_PREP，所以每一次咬
 * 都记成 phase=2——一个看门狗根本不开火的相位，害我们以为现场在别处。重启前先把当时的相位与
 * 心跳陈旧度抄进 trip_phase/trip_us，这两个字段只有看门狗自己写。
 * 注意 GA_BUILD_STAMP 取的是本编译单元的编译时刻，增量构建时它可能停留原地：自证"是不是刚编的
 * 这一次"要靠 app 哈希，不能只靠这个时间戳。
 * 0.5.4: 把 `swin`（0x8b 轨压采样窗口）与 `revert`（时间表内恢复）交回调用方——此前它们被
 * campaign.c 写死成 0，于是"发"这一层永远拿不到物理反馈：170 多发全记成 under，而我们既不知道
 * 那 222 us 的坑在轨上是不是真的存在（只有 0x8b 能回答），也不知道 11-24 同坐标 5/5 存活的
 * revert=1 为什么在战役里从来没被用过。两个字段本来就在一笔写的事务边界之外，验证仍走
 * glitch_args_validate，起批路径（HTTP 与 @C）各自已有的校验不变。
 * 另补 0.5.3 那句"收尾那 500 ms 睡眠照例喂心跳"：当时并未真的做，现在 restore_locked 在睡醒后
 * 打点，陈旧度从醒来这一刻算起——那 500 ms 是对着已经掉链的卡等的，不该记成总线卡死。
 * 0.5.5: 三件事，全部来自 2026-11-26 对"准确度与流程"评审时逐条核对代码的结果。
 * (1) 梯子按 PMBus 码走，不再按 mV。VOUT_COMMAND 的 LSB 是 6.25 mV（glitch_plan.c 的
 *     mv_to_code），所以 366/368/370 是同一个码 59、372/374/376/378 是同一个码 60：11-21 那次
 *     命中的"372→370→368→366 阶梯"里三发打的是同一坐标，而 300..380 这段合法带只有 14 个坐标，
 *     不是 81 个。梯子现在量化到码、并且不重复发本 point 已经判过的码（tried 位图）；判过的码
 *     全打完了就 phase=exhausted，而不是继续抄同一发。VD=MINV=MAXV 的锤击模式照旧一码到底，
 *     "你已经试过"不是对锤击的回答。状态里多给 v_code 与 codes_tried：tries=40 / codes_tried=14
 *     放在一起，才是那批"看着像搜索、其实是重复"的完整故事。
 * (2) lost 不再记成 over（campaign.c:267 旧账）。卡离开 PCIe 只说明读不到，不说明坑打深了；
 *     旧代码把它计入 over，梯子于是往浅处爬，而每一发都打在一辆看不见的卡上（11-20 实测：三个
 *     lost 轮各 tries=2，其中一发谁也没判过，正是判据「lost 之后补一发 → poison」（docs/UNLOCK_AND_POISON.md §四））。
 *     现在 lost 不动梯子，同一 point 两次或连续三次直接 phase=lost 收摊。
 * (3) 每发事后补读 STATUS_WORD(0x0B) 与 STATUS_VOUT(0x79)：0x78 bit4 一直是被命名为 vout_uv 的
 *     CML 位，真正的 VOUT_UV_FAULT 在 0x79 bit4。两笔读都在时间表之后，动不到坑；可选寄存器
 *     NACK 不算失败（不并进 diag_ok），值与 _ok 一起给，避免把没读到的 0x00 当成 ACK 的 0x00。
 * 顺带 GLITCH_MAX_HOLD_MS 10000→1000：停钟过几十 ms 从机会 abort 事务，而那一发照样报
 * writes_failed=0——这是本台架最毒的一种假发；战役从来不用 hold，收紧只会关掉误用。
 * 0.5.6: 只有可观测性，波形/时序/判决字段与 0.5.5 逐字节一致 —— 0.5.5 攒下的坐标与战绩对本版
 * 仍然有效，跨版本的台账可以直接比。两条都来自 2026-11-26 操作员的一句"这提示有误吧"：
 * (1) 页面的"判决方"那一格原本写"未运行：判决机上跑 host/ga100vfi_agent.py，**经串口连本板**"。
 *     通道是错的（agent 与板子之间只有 WiFi/HTTP；串口是**人**的逃生舱 bench/console.sh），而
 *     "未运行"更是把"板子没收到心跳"当成"没人判决"这个事实陈述。现在分成三态：在线（给出快照
 *     序号与年龄）、本次开机以来从没见过（并给出正确的启动命令）、见过之后断了（提示先分清是
 *     agent 退出还是板子先重启）。没有心跳时固件把 age_s 填成 -1，所以不在线那一支不许再拿它编
 *     一个"多少秒前"。
 * (2) 主机侧代理开始真的发 `@A` 心跳。这条路固件从 0.5.0 就实现好了（peer.c + main.c:peer_cmd，
 *     回包还会带回页面留下的开跑请求），但 host/ga100vfi_agent.py 里从来没有一行发它 —— 于是
 *     crack.sh 的代理明明在判每一发，页面仍然显示"未运行"，而操作员唯一能看到的"有没有人判决"
 *     的信号就是错的。心跳同时喂上页面里一直空着的 last/ledger/halt 三格：代理为什么停手
 *     （presence/poison/lost/reacted）从此在批中就看得见，而不是等批末翻 agent.log。
 *   心跳是 best-effort：它失败只记 peer_ok/peer_err，绝不影响判决（可观测性不是闸门）；且在
 *   phase=executing/settle 时尽量不发 —— 本台架量过"观测本身就是扰动"（0x8b 那 7 笔把
 *   reset_slip 从 8~231 µs 推到 370 µs），一次 HTTP 落在毛刺的计划表附近是同一类扰动；但超过
 *   半个过期周期（2.5 s）仍然照发，宁可多发一个请求也不让观测面撒谎。回包里的页面开跑请求
 *   只打印不执行：代管开火等于绕过 crack.sh 的发前闸门（poison 预检、锁状态、固件版本）。 */
/* 0.5.7: 补 0.5.6 自己漏掉的那一半 —— 0.5.6 刚刷上板就量到：把代理杀掉之后 6 s，
 * /api/status 仍然回 {"present":true,"age_s":6}。原因在 peer.c：`present` 是"本次开机以来收到过
 * 快照"的闩锁（peer_store 置位，只有 peer_init 清），过期只影响 age_s 这个数，不影响 present。
 * 于是 0.5.6 那三态文案按 present 分支就必然走错：判决方已经不在了，页面显示"在线"。这比 0.5.5
 * 的"未运行"更毒 —— 那句只是不精确，这句是反话，后果还包括**开火按钮继续可用**。
 * 现在：peer.h 新增 PEER_STALE_US(5 s)，present 由 age_us 算出来（闸门只有一份，页面不再自己抄
 * 一个 5）；快照本身（seq/info）仍然闩锁保留，因为代理死了之后，它留下的 halt 是唯一现场说明。
 * 这条规则其实 test_peer.c 的文件头早就写了（"a heartbeat that goes stale"），只是从来没人实现，
 * 也从来没人为它写断言 —— 0.5.7 起 test_heartbeat_expires 钉住：窗口内 present=true、窗口外
 * present=false 但 seq/info 仍在、重新收到 @A 就复活、seq=0 与 seq>0 两种空态分得开（57→74 断言）。 */
/* 0.5.8: 抬执行器优先级 + 近端禁调度自旋，因为归因表第一次给出了方向（`bench/timeline.sh`，
 * 2026-11-26 两批共 120 发）：`GA_RST_REL` 到达偏差 p50=118 µs / max=219 µs，`reset_slip` 4..224 µs，
 * 而命中窗是 ~20 µs 量级。执行器原本优先级 8，而**本板自己的心跳回调是 esp_timer 任务（prio 22、
 * 同绑 core 1）每 50 ms 一次**，看着正好是 100~200 µs 量级的嫌疑人。改法两条（都能用 -D 退回旧值
 * 做 A/B）：执行器抬到 GA_EXEC_PRIO=24（高于 esp_timer，低于 flash/cache 那几个必须让本核停的操作），
 * 以及到点前 GA_CRIT_SPIN_US=3000 µs 内禁调度自旋（带 4 倍硬超时兜底，禁调度期间没人救得了我们）。
 * 长等待照旧 vTaskDelay 让出，所以高优先级只占用近端几 ms；pex_guard(6) 与心跳比较的是时间戳，
 * 延后几 ms 无影响。`/api/status` 的 board 段多 `exec_prio`（**实际**优先级，不是请求值）与
 * `crit_spin_us`：A/B 的台账必须能证明这一发跑在哪种设置下。
 * ⚠️ 0.5.8 自己的 A/B 结果（0.5.9 写在这里之前先记下）：**抢占不是那个随机源**。同一坐标下
 * GA_RST_REL 偏差从 +118/+219/+16 µs 变成 +132 µs —— 没塌。逐 act 原文给出真机制：那笔坑写的
 * dur 是 211/309/114/223 µs，结束于 +114/+212/+13/+127，而释放沿落在 +118/+219/+16/+132：
 * **释放沿只是排在阻塞式总线写后面**，与调度器无关。真正的处理是 0.5.9 的重锚。 */
/* 0.5.9: 重锚（GA_REANCHOR）—— 让波形执行的是配方自己写的东西。机制（同上一段）：卡在被拉住复位
 * 时拉伸 SCL，坑的第一笔 VOUT 写实际花 114~309 µs（计划 36 µs），于是"坑开始"跟着这份迟到漂，
 * 而"坑结束"仍钉在原计划时刻（+pw）上 —— 结果**坑的实际长度**在 108~202 µs 之间随机，配方写的是
 * 222 µs。"同一发打 60 次"在物理上是 60 个不同长度的坑，这才是 240 发 0 反应期间最可疑的一件事。
 * 改法：把坑的第一笔写标成 GA_ANCHOR，它落地之后把后面每一步整体平移同样的量（相对关系 pw/gap/td
 * 一个字不改），于是释放沿 = 坑落下 + (0 − (td−delay))、坑长 = pw，第一次成为定义而不是巧合。
 * 释放沿与坑的相对关系本来就紧（实测 +3~+7 µs），重锚只是把这份紧从意外变成定义。
 * 卡侧那份迟到不会消失，它换了一个名字被记下来：`glitch done … stretch_us=N reanchor=1`，
 * timeline 的 stretch_us 分布就是"这张卡此刻有多忙"的曲线。-DGA_REANCHOR=0 回旧几何做 A/B（E4）。 */
#define GA_FW_VERSION "0.5.9"
#define GA_BUILD_STAMP __DATE__ " " __TIME__
#define GA_BUILD GA_FW_VERSION " (" GA_BUILD_STAMP ")"

// I2C (PMBus) to the NVVDD controller MP2888B @ 0x25. Any GPIO works via the
// matrix; card-side 3V3 SMBus pull-ups, 3.3 V pads, direct connection.
// GPIO47/48 is what this bench is physically wired with, and it carries the whole
// archive: all 112 shots, every calibration and every timing figure below were taken
// on it. 0.4.0 moved these defaults to GPIO6/7 because that pair is cleaner on paper
// (no shared pad with the module's onboard WS2812), but 6/7 never went on hardware --
// and a default that nobody has ever measured against is not a default, it is a guess.
// So the default is the wiring that exists, and the caveat is stated where it can be
// acted on: if anything ever writes WS2812 data on this module, that traffic looks to
// the PMBus exactly like a stuck slave. Our firmware never does, and the bus is
// re-probed at every bring-up, so the failure would be a printed degradation, not a
// silent one. The banner prints these two numbers for the same reason: the mismatch
// that hurts is firmware expecting one pair while the cables sit in another.
// Re-wiring changes the bus, not just the pin number: the 1 MHz timing baseline is
// re-probed at every bring-up (see CHANGELOG 0.3.1), and a bus that cannot answer at
// 1 MHz degrades with a printed reason -- check the banner says 1000000 Hz before
// trusting any shot geometry, because every dip width in the archive is 1 MHz math.
#define PIN_I2C_SDA  6
#define PIN_I2C_SCL  7

// Fundamental reset. Open-drain only: assert = drive low, release = hi-Z.
#define PIN_PEX_RST  10

// ---- compile-time checks -------------------------------------------------

// Strapping pins: keep their boot pull/level semantics free of our nets.
#if PIN_I2C_SDA == 0 || PIN_I2C_SDA == 3 || PIN_I2C_SDA == 45 || PIN_I2C_SDA == 46
#error "PIN_I2C_SDA is an S3 strapping pin (0/3/45/46)"
#endif
#if PIN_I2C_SCL == 0 || PIN_I2C_SCL == 3 || PIN_I2C_SCL == 45 || PIN_I2C_SCL == 46
#error "PIN_I2C_SCL is an S3 strapping pin (0/3/45/46)"
#endif
#if PIN_PEX_RST == 0 || PIN_PEX_RST == 3 || PIN_PEX_RST == 45 || PIN_PEX_RST == 46
#error "PIN_PEX_RST is an S3 strapping pin (0/3/45/46); its boot pull-up hits the 1V8 net"
#endif

// GPIO19/20 are native USB D-/D+ — the console dies if we touch them.
#if PIN_I2C_SDA == 19 || PIN_I2C_SDA == 20 || PIN_I2C_SCL == 19 || PIN_I2C_SCL == 20 || \
    PIN_PEX_RST == 19 || PIN_PEX_RST == 20
#error "GPIO19/20 are USB-Serial-JTAG D-/D+ on the S3"
#endif

// GPIO26-32 are the SPI flash bus on every S3 module.
#if (PIN_I2C_SDA >= 26 && PIN_I2C_SDA <= 32) || (PIN_I2C_SCL >= 26 && PIN_I2C_SCL <= 32) || \
    (PIN_PEX_RST >= 26 && PIN_PEX_RST <= 32)
#error "GPIO26-32 are the SPI0/1 flash pins on every ESP32-S3 module"
#endif

// GPIO33-37 additionally collide with octal flash/PSRAM — used up by the
// R8 octal PSRAM on N16R8 modules.
#if !defined(CONFIG_GA_ALLOW_OCTAL_PINS) && !defined(GA_ALLOW_OCTAL_PINS) && \
    ((PIN_I2C_SDA >= 33 && PIN_I2C_SDA <= 37) || (PIN_I2C_SCL >= 33 && PIN_I2C_SCL <= 37) || \
     (PIN_PEX_RST >= 33 && PIN_PEX_RST <= 37))
#error "GPIO33-37 collide with octal flash/PSRAM modules (enable GA_ALLOW_OCTAL_PINS if sure)"
#endif

// No two roles on one pin.
#if PIN_I2C_SDA == PIN_I2C_SCL || PIN_I2C_SDA == PIN_PEX_RST || \
    PIN_I2C_SCL == PIN_PEX_RST
#error "board.h pins must be distinct"
#endif

#endif  // BOARD_H
