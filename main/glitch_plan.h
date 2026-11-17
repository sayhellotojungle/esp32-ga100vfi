// Glitch timeline: pure logic, no IDF headers, compiled for target and host.
// Port of pico-ga100vfi pwmvid.c glitch_cmd() scheduling.
#ifndef GLITCH_PLAN_H
#define GLITCH_PLAN_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define GLITCH_MAX_STEPS 96
#define GLITCH_MIN_MV 0
#define GLITCH_MAX_MV 1000
#define GLITCH_MAX_PULSES 16
#define GLITCH_MAX_TIME_US 1000000
#define GLITCH_MAX_HOLD_MS 10000
#define GLITCH_MAX_SAMPLE_US 20000
#define GLITCH_MAX_DURATION_US 15000000

// Action ids — identical numbering to the RP2350 firmware.
enum {
    GA_VOUT    = 1,   // arg = VOUT_COMMAND code (6.25 mV/LSB)
    GA_MODE    = 2,   // E1 <- 0xC2EA (PWM-VID mode)
    GA_RST_LOW = 3,   // drive PEX_RST low
    GA_RST_REL = 4,   // release PEX_RST to hi-Z (t = 0 is this edge)
    GA_SCL_LOW = 5,   // hold SCL low (bus deinit first)
    GA_SCL_REL = 6,   // release SCL to hi-Z
    GA_MODE_ON = 7,   // E1 <- 0xC2FA (PMBus/DVID mode)
    GA_VMIN    = 8,   // arg = VOUT_MIN code
    GA_VS      = 9,   // sample READ_VOUT (0x8b) telemetry into the ring
    GA_CLR     = 10,  // CLEAR_FAULTS (0x03) — drop latched UVP/OCP first
};

typedef struct { int32_t t_us; uint8_t act; int32_t arg; } gstep_t;

typedef struct {
    int vd, td, vr, tr, tv;
    int fast, hold_ms, pulses, pw, gap, delay, revert;
    int swin;                               // telemetry sample window us, 0=off
} glitch_args_t;

// Defaults identical to the RP2350 firmware.
void glitch_args_defaults(glitch_args_t *a);

// Parse the text after 'g' (space separated, missing fields keep defaults).
// Reject overflow, malformed tokens and extra fields; leave *a unchanged on
// failure. Semantic bounds are checked separately by glitch_args_validate().
bool glitch_args_parse(glitch_args_t *a, const char *rest);

// Software bounds, preparation ordering and derived duration checks. Call
// before and after finalize; <=0 pw/gap retain their auto meaning. These
// bounds prevent invalid arithmetic/commands, not a hardware safety guarantee.
bool glitch_args_validate(const glitch_args_t *a, char *error, size_t n);

// One 4-byte PMBus transaction (addr+W + cmd + 2 data + overheads) is
// ~36 bit-times: 36 us @ 1 MHz, 90 us @ 400 kHz, 360 us @ 100 kHz.
uint32_t glitch_pmbus_w_us(uint32_t baud_hz);

// Auto-size pw/gap (<=0) and floor both at one PMBus write.
void glitch_args_finalize(glitch_args_t *a, uint32_t pmbus_write_us);

// Fill st[] sorted by t_us (stable insertion sort, as on the Pico). Call
// finalize first. Returns -1 on invalid arguments or insufficient capacity,
// without changing st[] or *out_post; otherwise returns the full step count.
// *out_post = restore anchor (tv, or train_end+gap when the pulse train is last).
int glitch_build(const glitch_args_t *a, gstep_t *st, int max_steps,
                 int32_t *out_post);

#endif  // GLITCH_PLAN_H
