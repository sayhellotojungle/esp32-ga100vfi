#ifndef PMBUS_H
#define PMBUS_H

#include <stdbool.h>
#include <stdint.h>

#define PMBUS_ADDR   0x25
#define I2C_TO_MS    20

// Effective bus speed (drives the glitch step clamp). Call pmbus_up first;
// until initialization confirms pull-ups this reports the requested speed.
uint32_t pmbus_baud_hz(void);
uint32_t pmbus_requested_baud_hz(void);
uint32_t pmbus_write_us(void);

// Bring the bus up: float pins, verify/derive pull-ups (detect.c logic:
// still low against internal ~45k pulls => unpowered card, ESD diodes clamp
// the bus, or a line is shorted), then create the i2c_master bus + 0x25 dev.
bool pmbus_up(void);
void pmbus_down(void);
bool pmbus_set_baud(unsigned long hz);

// Raw transfers against 0x25 (bring the bus up first). Requests fail while
// SCL is intentionally held; they cannot silently release an active hold.
bool pmbus_w(uint8_t reg, const uint8_t *d, int n);
bool pmbus_w16(uint8_t reg, uint16_t v);
bool pmbus_r(uint8_t reg, uint8_t *d, int n);

// Raw framed transfers against any 7-bit address (SMBPBI uses 0x3A; the
// caller owns the framing bytes, including any SMBus count byte).
bool pmbus_raw_write(uint8_t addr, const uint8_t *buf, int n);
bool pmbus_raw_read(uint8_t addr, uint8_t *buf, int n);

// PEX_RST pad is permanently open-drain; "high" == hi-Z.
void pex_init(void);
void pex_hold(bool low);

// Hold SCL low (deinit the bus first) / release it to hi-Z. Used by @L/@U
// and by the glitch timeline's SCL-hold steps.
void pmbus_scl_hold(bool low);

// Serial-protocol layer (line has no leading '@').
void pmbus_cmd(char *opline);

#endif  // PMBUS_H
