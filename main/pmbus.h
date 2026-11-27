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

// Microseconds PEX_RST has been held low, or 0 while released. The console 'j'
// command has no blocking counterpart -- it returns immediately and leaves the
// pad low until 'J' -- so this is what the guard task watches. See
// GA_PEX_MAX_HOLD_MS in board.h: a long manual hold is a hardware action we
// stopped trusting people to remember (2026-11-21).
int64_t pex_held_us(void);

// Hold SCL low (deinit the bus first) / release it to hi-Z. Used by @L/@U
// and by the glitch timeline's SCL-hold steps.
void pmbus_scl_hold(bool low);

// Timeline guard. While a glitch timeline runs, transfers must not start a bus bring-up:
// a step that finds the bus down fails, it does not spend hundreds of ms re-detecting
// pull-ups and walking the baud ladder (see pmbus.c). pmbus_seq_reopen() is the explicit
// exception, for the step that ends an intentional SCL hold.
void pmbus_seq_begin(void);
void pmbus_seq_end(void);
bool pmbus_seq_reopen(void);

// Serial-protocol layer (line has no leading '@').
void pmbus_cmd(char *opline);

#endif  // PMBUS_H
