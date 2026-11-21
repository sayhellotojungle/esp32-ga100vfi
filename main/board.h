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
 * 拿旧版说明书接线的人会在总线上拿到 NACK，这件事必须从版本号上就能看出来。 */
#define GA_FW_VERSION "0.4.0"
#define GA_BUILD_STAMP __DATE__ " " __TIME__
#define GA_BUILD GA_FW_VERSION " (" GA_BUILD_STAMP ")"

// I2C (PMBus) to the NVVDD controller MP2888B @ 0x25. Any GPIO works via the
// matrix; card-side 3V3 SMBus pull-ups, 3.3 V pads, direct connection.
// GPIO6/7: neither is a strapping pin (0/3/45/46), neither is USB D-/D+ (19/20),
// neither is on the flash bus (26..32) or the octal-PSRAM bus (33..37), and neither
// drives an onboard peripheral. The previous bench wiring was GPIO47/48, which works
// but sits on the same pad as the module's onboard WS2812 -- a strip of LED data
// traffic there looks to the PMBus exactly like a stuck slave. The compile-time
// checks below are what make "pick any free GPIO" a rule instead of a hope.
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
