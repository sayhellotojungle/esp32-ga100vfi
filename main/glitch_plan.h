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
/* 0.5.5: 10 s -> 1 s. A timeline that stops the clock longer than a few tens of ms makes an
 * SMBus slave abort its transaction and come back holding SDA, which is the one failure this
 * bench cannot tell apart from a fired shot: the write still reports wfail=0. Nothing in any
 * campaign uses hold (campaign.c pins hold_ms=0; test/test_main.c uses 2 ms to prove the bus
 * comes back), so the only honest ceiling is "long enough to be a diagnostic, too short to
 * wedge the bus". It stays out of timing strategies. */
#define GLITCH_MAX_HOLD_MS 1000
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

/* flags：目前只有 GA_ANCHOR。它标的是**整个波形里唯一一件真正发生了的事**——坑的第一笔写
 * 落到总线上的那一刻。为什么需要它（2026-11-26 逐 act 原文实测，两批 5 发同一形状）：
 * 卡在被拉住复位时会拉伸 SCL，所以那笔 16-bit 写的实际耗时是 114/211/223/309 µs（计划值 36 µs），
 * 而它后面每一步都排在这笔写**后面**，于是整条波形被整体推后 13~219 µs。更糟的是"坑开始"跟着
 * 漂而"坑结束"仍钉在原计划时刻上，于是**坑的实际长度**在 108~202 µs 之间随机（配方写的是 222）。
 * 也就是说"同一发打 60 次"在物理上不是 60 个相同实验，是 60 个不同深度的坑。
 * 打了 GA_ANCHOR 的那一步执行完之后，运行器把**后面所有步**按它的迟到量整体平移（见 main.c 的
 * GA_REANCHOR），于是 pw/gap/td 这些相对关系第一次真正成立。释放沿与坑的相对关系本来就紧
 * （实测 +3~+7 µs），平移只是把这份紧从"意外"变成"定义"。 */
#define GA_ANCHOR 0x01      /* 坑的第一笔 VOUT 写：后面的步以它为准 */

typedef struct { int32_t t_us; uint8_t act; int32_t arg; uint8_t flags; } gstep_t;

/* ---- The only coordinate that physically exists: the PMBus code ----
 *
 * VOUT_COMMAND is Linear11 ULINEAR with a 6.25 mV LSB, so a commanded millivolt is only ever
 * realisable as round(mv*16/100). Two millivolt numbers that round to the same code are the
 * *same* physical shot: measured against the first 0x8b rail samples on 2026-11-26,
 * 366/368/370 all -> code 59 and 372/374/376/378 all -> code 60. The 2 mV ladder of the
 * 2026-11-21 hit replica fired three of its eight shots at a coordinate it had already
 * fired, and the whole 300..380 mV band is 14 coordinates, not 81.
 *
 * Anything that searches depth searches codes (campaign.c's ladder); anything that reports a
 * coordinate reports the code beside it (board status, host agent, bench ledger).
 */
#define GLITCH_MV_PER_CODE_NUM 16   /* code = (mv * 16 + 50) / 100 */
#define GLITCH_MV_PER_CODE_DEN 100

int glitch_mv_to_code(int mv);                  /* the rounding the timeline itself uses */
int glitch_code_to_mv(int code);                /* nearest mV that maps back to this code */
int glitch_codes_apart(int mv_a, int mv_b);     /* 0 => the same physical shot */

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
