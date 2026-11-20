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
#define GA_BAUD_HZ 400000
#endif

// SoftAP fallback: always up, so the board is reachable even before any
// station network is configured (http://192.168.4.1/).
#define GA_AP_SSID "GA100VFI"
#define GA_AP_PASS "ga100glitch"
/* 版本号只在这里定义一次；横幅与 /api/status 都取这两个宏。
 * 编译时刻单独成串，是为了能区分同版本号的不同构建。 */
#define GA_FW_VERSION "0.3.0"
#define GA_BUILD_STAMP __DATE__ " " __TIME__
#define GA_BUILD GA_FW_VERSION " (" GA_BUILD_STAMP ")"

// I2C (PMBus) to the NVVDD controller MP2888B @ 0x25. Any GPIO works via the
// matrix; card-side 3V3 SMBus pull-ups, 3.3 V pads, direct connection.
// Wired to GPIO47/48 on this bench. 6/7 would be the safer default (never the
// onboard WS2812 pin), so if the bus reads no-ack here, check the wiring before
// doubting the code; nothing else in the firmware claims 47 or 48.
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
