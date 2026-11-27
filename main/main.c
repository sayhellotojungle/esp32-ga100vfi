// All hardware operations run on the core-1 executor. HTTP/UART submit whole
// commands; campaign_poll owns its lifecycle. The existing software scheduler
// records the actual reset edge and reports blocking-I2C/dispatch timing slip.
#include <ctype.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_attr.h"
#include "esp_system.h"
#include "esp_timer.h"

#include "board.h"
#include "board_runtime.h"
#include "campaign.h"
#include "console.h"
#include "glitch_plan.h"
#include "net.h"
#include "peer.h"
#include "pmbus.h"
#include "protocol.h"

#define printf(...) console_printf(__VA_ARGS__)
// 'GAF3'. Bumped from 'GAF2' together with the two watchdog fields below: an image that
// reads a record written by the older layout would print garbage as the bite site, and a
// stale breadcrumb in the reset note is worse than no breadcrumb.
#define FX_COOKIE 0x47414633u
#define FX_MAGIC_WDT 0xfa170002u
#define FX_MAGIC_SW 0xfa170003u
#define GA_VS_MAX 48

enum { PH_IDLE, PH_CMD, PH_GLITCH_PREP, PH_GLITCH_RUN };
typedef struct {
    uint32_t cookie, cookie_inv, magic, phase, pc, lr;
    int32_t step_t;
    uint32_t step_act, step_idx, boot_count;
    // The watchdog's own words. `phase` is rewritten by the app core the moment a blocked
    // write finally lands -- esp_restart() is only a request -- so every bite on
    // 2026-11-25 was recorded as phase=2 (PREP), a phase the watchdog cannot even fire in.
    // These two fields are written only inside the timer callback.
    uint32_t bite_phase, bite_us;
} fx_t;
RTC_NOINIT_ATTR static volatile fx_t s_fx;
DRAM_ATTR static volatile uint32_t s_hb;
static SemaphoreHandle_t s_hardware;
static portMUX_TYPE s_state_lock = portMUX_INITIALIZER_UNLOCKED;
static bool s_busy;
static char reset_note[192];
static int64_t s_boot_t0;

typedef struct {
    bool valid, diag_ok;
    int vd, wfail, uv;
    uint16_t sw;
    /* Post-shot witness from the VRM's own latches (0.5.5): STATUS_WORD 0x0B (PMBus: low byte
     * = STATUS_BYTE, high byte = STATUS_MISC — one transaction for both) and STATUS_VOUT 0x79,
     * which is where VOUT_UV_FAULT actually lives (bit 4). Read *after* the timeline so they
     * cannot perturb the dip, and best-effort because this part is allowed not to implement
     * them — _ok says which case we are in, and a NACK here must never become diag_ok=false.
     * Raw values only, no decoding: the old `vout_uv` name took bit 4 of 0x78 (STATUS_BYTE),
     * which is the CML bit, not an undervoltage bit. */
    uint16_t sw0b;
    uint8_t svout;
    bool sw0b_ok, svout_ok;
    uint32_t seq;
    int32_t slip_us, reset_slip_us;
    int64_t time_s;
} last_t;
static last_t s_last;
static DRAM_ATTR gstep_t s_plan[GLITCH_MAX_STEPS];
static DRAM_ATTR struct { int32_t t_us; uint16_t raw; } s_vsamp[GA_VS_MAX];
static int s_vsamp_n;
static int32_t s_glitch_slip_us, s_reset_slip_us;

/* Per-step timing profile: what the plan asked for vs. when the step really ran and how
 * long it blocked. Free to take (two esp_timer reads per step, no bus traffic), and it
 * answers the question that cost us the whole day of 2026-11-21: the dip's TRAILING edge
 * is a blocking 1 MHz PMBus write too, so `pw` only says when we *ask* for the rail back.
 * Measured reset_slip ranged 0..245 us, which is wider than the ~20 us window a hit needs
 * -- that is why 60+ shots produced no DSOV movement while the author's bench hits 14 %.
 * Recorded in DRAM inside the IRAM loop and printed after the run: printf in the loop
 * would itself become the disturbance we are trying to measure. */
#define GA_PROF_MAX 24
static DRAM_ATTR struct {
    int32_t want, got;
    int16_t dur;
    uint8_t act;
} s_prof[GA_PROF_MAX];
static int s_prof_n;

static int64_t IRAM_ATTR now_us(void) { return esp_timer_get_time(); }
static void IRAM_ATTR hb_touch(void) { s_hb = (uint32_t)now_us(); }

static bool hardware_enter(void)
{
    if (!s_hardware || xSemaphoreTake(s_hardware, 0) != pdTRUE) return false;
    portENTER_CRITICAL(&s_state_lock); s_busy = true; portEXIT_CRITICAL(&s_state_lock);
    return true;
}
static void hardware_leave(void)
{
    s_fx.phase = PH_IDLE;
    portENTER_CRITICAL(&s_state_lock); s_busy = false; portEXIT_CRITICAL(&s_state_lock);
    xSemaphoreGive(s_hardware);
}
bool board_is_busy(void)
{
    portENTER_CRITICAL(&s_state_lock); bool busy = s_busy; portEXIT_CRITICAL(&s_state_lock);
    return busy;
}
bool board_begin_maintenance(void)
{
    if (campaign_is_running() || !hardware_enter()) return false;
    if (campaign_is_running()) { hardware_leave(); return false; }
    return true;
}
void board_end_maintenance(void) { hardware_leave(); }

static void pins_safe_boot(void)
{
    const int pins[] = { PIN_I2C_SDA, PIN_I2C_SCL, PIN_PEX_RST };
    for (unsigned i = 0; i < sizeof pins / sizeof pins[0]; i++) {
        gpio_set_direction(pins[i], GPIO_MODE_INPUT);
        gpio_set_pull_mode(pins[i], GPIO_FLOATING);
    }
}

static void reset_history_init(void)
{
    esp_reset_reason_t reason = esp_reset_reason();
    bool retained = reason != ESP_RST_POWERON && reason != ESP_RST_EXT &&
                    s_fx.cookie == FX_COOKIE && s_fx.cookie_inv == ~FX_COOKIE &&
                    s_fx.phase <= PH_GLITCH_RUN;
    uint32_t boots = retained ? s_fx.boot_count : 0;
    if (!retained) {
        snprintf(reset_note, sizeof reset_note, "power-on / reset %d (no retained record)", (int)reason);
    } else if (reason == ESP_RST_SW && s_fx.magic == FX_MAGIC_SW) {
        snprintf(reset_note, sizeof reset_note, "software restart ('b')");
    } else {
        const char *kind = reason == ESP_RST_PANIC ? "PANIC" :
                           reason == ESP_RST_INT_WDT ? "INT_WDT" :
                           reason == ESP_RST_BROWNOUT ? "BROWNOUT" :
                           reason == ESP_RST_SW && s_fx.magic == FX_MAGIC_WDT ? "WATCHDOG" : "reset";
        snprintf(reset_note, sizeof reset_note,
                 "%s reason=%d phase=%u step=%u act=%u t=%ldus stale=%uus pc=%08lx lr=%08lx",
                 kind, (int)reason,
                 (unsigned)(s_fx.magic == FX_MAGIC_WDT ? s_fx.bite_phase : s_fx.phase),
                 (unsigned)s_fx.step_idx,
                 (unsigned)s_fx.step_act, (long)s_fx.step_t,
                 (unsigned)s_fx.bite_us,
                 (unsigned long)s_fx.pc, (unsigned long)s_fx.lr);
    }
    /* Read old breadcrumbs BEFORE initializing this boot's record. */
    memset((void *)&s_fx, 0, sizeof s_fx);
    s_fx.cookie = FX_COOKIE; s_fx.cookie_inv = ~FX_COOKIE;
    s_fx.boot_count = boots == UINT32_MAX ? 1 : boots + 1;
    s_fx.phase = PH_IDLE;
}

static void wdt_timer_cb(void *arg)
{
    (void)arg;
    if (s_fx.phase == PH_GLITCH_RUN) {
        uint32_t stale = (uint32_t)((uint32_t)now_us() - s_hb);
        if (stale >= 250000u) {
            s_fx.magic = FX_MAGIC_WDT;
            s_fx.bite_phase = s_fx.phase;
            s_fx.bite_us = stale;
            pex_hold(false);
            esp_restart();
        }
    }
}
static void wdt_start(void)
{
    esp_timer_handle_t timer;
    const esp_timer_create_args_t cfg = { .callback = wdt_timer_cb, .name = "ga_wdt" };
    ESP_ERROR_CHECK(esp_timer_create(&cfg, &timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(timer, 50000));
}

static const struct { uint8_t reg; uint16_t stock, fast; } PROFILE[] = {
    {0xBA,0x0019,0x0001}, {0xB9,0x0032,0x0001}, {0x30,0xCE64,0xCE04},
    {0x31,0x0072,0x0002}, {0x32,0x7F81,0x0F81}, {0x45,0x7018,0x7218},
};
static bool profile_apply(bool fast)
{
    bool ok = true;
    for (unsigned i = 0; i < sizeof PROFILE / sizeof PROFILE[0]; i++)
        if (!pmbus_w16(PROFILE[i].reg, fast ? PROFILE[i].fast : PROFILE[i].stock)) ok = false;
    return ok;
}
static void profile_cmd(bool fast)
{
    bool ok = profile_apply(fast);
    char out[256]; int used = snprintf(out, sizeof out, "profile %s:", fast ? "fast" : "stock");
    for (unsigned i = 0; i < sizeof PROFILE / sizeof PROFILE[0]; i++) {
        uint8_t b[2];
        if (pmbus_r(PROFILE[i].reg, b, 2))
            used += snprintf(out + used, sizeof out - (size_t)used, " %02x=%02x%02x", PROFILE[i].reg, b[1], b[0]);
        else { ok = false; used += snprintf(out + used, sizeof out - (size_t)used, " %02x=ERR", PROFILE[i].reg); }
    }
    printf("%s %s\n", out, ok ? "OK" : "ERR");
}

/* Unlock the PMBus write protect, and do it in the order the part actually requires.
 *
 * CLEAR_FAULTS (0x03) goes in *before* the password. Our own glitch drives an
 * over/under-voltage event; while that fault is latched the controller answers reads and
 * refuses PW. The command that clears the fault is GA_CLR, GA_CLR lives in the timeline,
 * and the timeline is gated behind this function -- so the password-first order deadlocks
 * with no exit. Measured 2026-11-19/20 with exactly that order: `u` answered
 * "unlock ERR" forever, every shot returned here before GA_RST_LOW, and twenty attempts
 * bought twenty "under" verdicts from a card that had never been touched. The bench
 * conclusion at the time was "the VRM latches solid, only removing VDD33 recovers it",
 * which was wrong: 0x03 followed by the password unlocks it in one try, no power cycle.
 *
 * 0x03 is a plain command, not a protected write, so the write protect does not gate it;
 * only 0x13/0x1F/0x21/0xE1 are. It is sent unconditionally rather than behind a decoded
 * fault bit because which STATUS_CML bits mean "fault latched" on this part is not
 * documented, and one extra byte write costs microseconds while a wrong guess costs
 * another day of no-op shots.
 *
 * config-id 0x1229 (register 0x46) is part of unlocking, not a "fast profile" tweak.
 * Reference implementation pwmvid.c:215-225 writes it in `u` and its README is explicit
 * about why: it stops the GA100's devinit from re-locking the VRM. Our per-shot path used
 * to make every write except this one, so the first glitch released PEX_RST, the GPU ran
 * devinit, the VRM locked again, and attempts 2..12 all died here ("locked/unreachable",
 * 11 nacks) even though the driver had never been loaded. Measured 2026-11-20. The write
 * is verified by readback because the part ACKs protected writes it then discards. */
enum { UNLOCK_OK = 0, UNLOCK_BUS, UNLOCK_REFUSED, UNLOCK_CONFIGID };

static int ensure_unlocked(void)
{
    uint8_t cml;
    if (!pmbus_r(0x7E, &cml, 1)) return UNLOCK_BUS;
    if (!(cml & 0x08)) {
        pmbus_w(0x03, NULL, 0);                              // clear the fault, then ask
        if (!pmbus_r(0x7E, &cml, 1)) return UNLOCK_BUS;
        const uint8_t pw[2] = {0x25,0x7C};
        if (!pmbus_w(0xF2, pw, 2) || !pmbus_r(0x7E, &cml, 1) || !(cml & 0x08)) return UNLOCK_REFUSED;
    }
    const uint8_t wp = 0;
    if (!pmbus_w(0x13, &wp, 1)) return UNLOCK_BUS;
    if (!pmbus_w16(0x46, 0x1229)) return UNLOCK_CONFIGID;
    uint8_t cfg[2];
    if (!pmbus_r(0x46, cfg, 2) || cfg[0] != 0x29 || cfg[1] != 0x12) return UNLOCK_CONFIGID;
    return UNLOCK_OK;
}
static void unlock_cmd(void)
{
    /* One line per failure mode, deliberately. This printed the same "unlock ERR" for a
     * dead bus, a refused password and a refused follow-up write, and that ambiguity is
     * what turned an ordinary ordering bug into a wrong hardware conclusion ("the VRM is
     * latched, remove VDD33") and cost a day of no-op shots. "check your wiring", "the
     * part said no" and "the password took but something else nacked" call for three
     * different actions, so they have to be three different lines. */
    uint8_t cml = 0;
    if (!pmbus_r(0x7E, &cml, 1))
    {
        printf("unlock ERR unreachable (VRM 不应答：查供电/接线/地址/总线占用)\n");
        return;
    }
    const int ul = ensure_unlocked();
    switch (ul)
    {
    case UNLOCK_OK: break;
    case UNLOCK_BUS:
        printf("unlock ERR unreachable（总线/供电：读 CML 或写普通命令失败）\n");
        return;
    case UNLOCK_REFUSED:
        printf("unlock ERR refused（CLEAR_FAULTS 后密码仍不匹配：pwd=0 真拒，需冷启）\n");
        return;
    default:
        printf("unlock ERR config-id（密码已过，但 0x46=0x1229 写不进/回读不符 → VRM 被 devinit 锁死，需冷启）\n");
        return;
    }
    printf("unlock MATCH\n");
}
static void lock_status_cmd(void)
{
    uint8_t cml, e1[2], v21[2];
    if (!pmbus_r(0x7E, &cml, 1) || !pmbus_r(0xE1, e1, 2) || !pmbus_r(0x21, v21, 2)) {
        printf("ERR status read\n"); return;
    }
    printf("cml=0x%02x pwd=%d e1=0x%02x%02x vout=0x%02x%02x\n",
           cml, !!(cml & 8), e1[1], e1[0], v21[1], v21[0]);
}

/* No extra PEX pulse: this only restores the controller's normal control path. */
static bool restore_locked(void)
{
    s_fx.phase = PH_GLITCH_PREP;
    pex_hold(false);
    pmbus_scl_hold(false);
    bool min_ok = pmbus_w16(0x1F, 0x30);
    bool vout_ok = pmbus_w16(0x21, 0x7E);
    vTaskDelay(pdMS_TO_TICKS(500));
    hb_touch();   // 这 500 ms 是对着可能已经掉链的卡等的，不该算成总线卡死
    bool mode_ok = pmbus_w16(0xE1, 0xC2EA);
    bool profile_ok = profile_apply(false);
    printf("revert vout=%d mode=%d vmin=%d profile=%d\n", vout_ok, mode_ok, min_ok, profile_ok);
    return min_ok && vout_ok && mode_ok && profile_ok;
}
static bool board_restore_control(void)
{
    if (!hardware_enter()) return false;
    bool ok = restore_locked(); hardware_leave(); return ok;
}

/* 计划步为什么迟到：全部来自**被抢占**。执行器原本在优先级 8，而本板自己的心跳回调是 esp_timer
 * 任务（优先级 22、同绑 core 1）每 50 ms 跑一次，一次就是 100~200 µs —— 与 2026-11-26 的
 * timeline 表里 `GA_RST_REL` 到达偏差 p50=118 µs / max=219 µs 同形状，而命中窗是 ~20 µs 量级。
 * "同一发打 60 次"其实是 60 个不同相位：**我们自己就是那个随机源**。
 * 两道一起上：
 *   (1) 执行器优先级抬到 esp_timer 之上（GA_EXEC_PRIO）——关掉任务级抢占；
 *   (2) 到点前 GA_CRIT_SPIN_US 内**禁调度自旋**——连"让给同优先级的别人"都不发生。
 * 长等待照旧 vTaskDelay 让出（>2.5 ms 就睡），所以高优先级只占用近端这几 ms；pex_guard(6) 与心跳
 * 都不靠抢 CPU 保命：守卫比较的是时间戳，被延后几 ms 无影响（GA_PEX_MAX_HOLD_MS 是秒级）。
 * `-DGA_CRIT_SPIN_US=0` / `-DGA_EXEC_PRIO=8` 可退回旧行为做 A/B（E4）。验收不看感觉：看同一坐标下
 * `bench/timeline.sh` 里 GA_RST_REL 到达偏差的分布有没有塌下来。 */
#ifndef GA_EXEC_PRIO
#define GA_EXEC_PRIO 24
#endif
#ifndef GA_CRIT_SPIN_US
#define GA_CRIT_SPIN_US 3000
#endif
/* GA_REANCHOR：坑的第一笔写落下之后，把剩下的步按它的迟到量整体平移（见 glitch_plan.h GA_ANCHOR）。
 * 这不是"调配方"，是**让配方执行的是它自己写的东西**：卡拉伸 SCL 使那笔 16-bit 写实测花了
 * 114/211/223/309 µs（计划 36 µs），而坑的结束仍钉在原计划时刻上，于是坑的实际长度在
 * 108~202 µs 之间随机（配方写的是 222）。平移之后 pw/gap/td 第一次是真实几何。
 * -DGA_REANCHOR=0 退回旧行为做 A/B（E4）；/api/status 的 reanchor 与 `glitch done` 行的
 * anchor_shift= 让台账能证明每一发跑在哪种模式下、平移了多少。 */
#ifndef GA_REANCHOR
#define GA_REANCHOR 1
#endif
static TaskHandle_t s_executor;   /* 只为把**实际**优先级报进 /api/status（请求值不算数） */
static int32_t s_anchor_shift_us; /* 这一发坑真正落下的时刻比计划晚了多久（=卡拉伸了多少） */

static int IRAM_ATTR glitch_run(const gstep_t *st, int ns, int64_t t0)
{
    int wfail = 0;
    int64_t actual_reset = -1;
    int64_t shift = 0;      /* 重锚平移量：只在锚点之后非零 */
    s_glitch_slip_us = 0; s_reset_slip_us = 0; s_vsamp_n = 0; s_prof_n = 0; s_anchor_shift_us = 0;
    for (int i = 0; i < ns; i++) {
        const gstep_t *s = &st[i];
        /* want 是这一步**本轮真正该发生的时刻**：锚点之前的步照原计划，锚点之后的步跟着坑走。
         * 记进 profile 的也必须是这个值，否则 timeline 会把"坑迟到 200 µs"记成"后面每一步都
         * 迟到 200 µs"，而那是两件事（前者是卡侧的，后者才是我们的准点率）。 */
        int64_t want = t0 + s->t_us + ((s->flags & GA_ANCHOR) ? 0 : shift);
        while (now_us() < want) {
            hb_touch();
            /* Long holds/revert waits must let the idle task run. */
            if (want - now_us() > (int64_t)portTICK_PERIOD_MS * 1000 + 1500)
                vTaskDelay(1);
            if (GA_CRIT_SPIN_US > 0 && want - now_us() <= GA_CRIT_SPIN_US)
                break;      /* 进近端：剩下的路用禁调度自旋走，不再交给调度器 */
        }
        if (GA_CRIT_SPIN_US > 0 && now_us() < want) {
            /* 最后 GA_CRIT_SPIN_US：禁调度。带**硬超时**兜底 —— 禁调度期间没人能救我们，
             * 万一时间戳因为某种原因不前进，宁可退回旧行为（准点率低但不等死），也不能把整块
             * 板子锁死在临界区里。 */
            const int64_t guard = now_us();
            vTaskSuspendAll();
            while (now_us() < want && now_us() - guard < 4 * GA_CRIT_SPIN_US) { }
            xTaskResumeAll();
            hb_touch();
        }
        int32_t slip = (int32_t)(now_us() - want);
        if (slip > s_glitch_slip_us) s_glitch_slip_us = slip;
        // When the step actually started, recorded before the action blocks. Two counter
        // reads and nothing else: no printf, no bus traffic inside the timing loop, so the
        // measurement itself cannot become the disturbance.
        int64_t step_start = now_us();
        s_fx.step_idx = (uint32_t)i; s_fx.step_act = s->act; s_fx.step_t = s->t_us;
        bool ok = true;
        switch (s->act) {
        case GA_CLR: ok = pmbus_w(0x03, NULL, 0); break;
        case GA_VMIN: ok = pmbus_w16(0x1F, (uint16_t)s->arg); break;
        case GA_MODE_ON: ok = pmbus_w16(0xE1, 0xC2FA); break;
        case GA_VOUT: ok = pmbus_w16(0x21, (uint16_t)s->arg); break;
        case GA_MODE: ok = pmbus_w16(0xE1, 0xC2EA); break;
        case GA_RST_LOW: pex_hold(true); break;
        case GA_RST_REL:
            pex_hold(false);
            actual_reset = now_us();
            /* 相对这一步自己的计划时刻（重锚之后那就是"坑落下 +104 µs"），而不是相对 t0：
             * 后者把卡侧的拉伸算进我们的准点率里，是 0.5.5 之前 reset_slip 0..245 µs 这个
             * "命中窗比它还宽"结论的真正来源。 */
            s_reset_slip_us = (int32_t)(actual_reset - want);
            break;
        case GA_SCL_LOW: pmbus_scl_hold(true); break;
        case GA_SCL_REL: pmbus_scl_hold(false); ok = pmbus_seq_reopen(); break;
        case GA_VS: {
            uint8_t d[2];
            if (s_vsamp_n < GA_VS_MAX && pmbus_r(0x8b, d, 2)) {
                s_vsamp[s_vsamp_n].t_us = (int32_t)(now_us() - t0);
                s_vsamp[s_vsamp_n++].raw = (uint16_t)(d[0] | (d[1] << 8));
            }
        } break;
        default: ok = false; break;
        }
        hb_touch();
        if (s_prof_n < GA_PROF_MAX) {   // end of step = when that write really landed
            s_prof[s_prof_n].want = (int32_t)(want - t0);   // 本轮真正该发生的时刻（含重锚平移）
            s_prof[s_prof_n].got = (int32_t)(step_start - t0);
            s_prof[s_prof_n].dur = (int16_t)(now_us() - step_start);
            s_prof[s_prof_n].act = (uint8_t)s->act;
            s_prof_n++;
        }
        if (ok && (s->flags & GA_ANCHOR)) {
            /* 坑第一次真的落在总线上了。late 就是"卡把 SCL 拉伸了多少"（实测 114~309 µs，而计划
             * 值 36 µs）——它无论重锚与否都要报，因为它是这张卡此刻的真实行为。
             * GA_REANCHOR 只决定这份迟到要不要转成后面每一步的平移：平移之后 pw/gap/td 才是
             * 真实几何（否则坑的实际长度随机在 108~202 µs，而配方写的是 222）。 */
            int64_t late = now_us() - want;
            s_anchor_shift_us = (int32_t)late;
            if (GA_REANCHOR) shift = late;
        }
        if (!ok) { wfail++; break; }   // stop actuation; caller performs recovery
    }
    pex_hold(false);
    return wfail;
}

static bool board_execute_glitch(const glitch_args_t *input, int *writes_failed, uint32_t *sequence)
{
    if (writes_failed) *writes_failed = 0;
    if (sequence) *sequence = 0;
    char error[96];
    if (!glitch_args_validate(input, error, sizeof error)) {
        printf("glitch ERR %s\n", error); return false;
    }
    if (!hardware_enter()) { printf("glitch ERR hardware busy\n"); return false; }
    s_fx.phase = PH_GLITCH_PREP;
    s_fx.pc = (uint32_t)(uintptr_t)&board_execute_glitch;
    s_fx.lr = (uint32_t)(uintptr_t)__builtin_return_address(0);
    bool prepared = false, diag_ok = false;
    int wfail = 0; uint32_t seq = 0;
    glitch_args_t args = *input;
    if (!pmbus_up()) {
        printf("glitch ERR bus down/held\n"); goto finish;
    }
    /* Pull-up detection may lower the effective bus speed. */
    glitch_args_finalize(&args, pmbus_write_us());
    if (!glitch_args_validate(&args, error, sizeof error)) {
        printf("glitch ERR %s\n", error); goto finish;
    }
    int32_t post;
    int ns = glitch_build(&args, s_plan, GLITCH_MAX_STEPS, &post);
    (void)post;
    if (ns <= 0) { printf("glitch ERR invalid or oversized timeline\n"); goto finish; }
    const int ul = ensure_unlocked();
    if (ul != UNLOCK_OK) {
        printf("glitch ERR controller %s (ul=%d)\n",
               ul == UNLOCK_REFUSED ? "password-refused"
               : ul == UNLOCK_CONFIGID ? "config-id-0x1229-refused" : "unreachable", ul);
        goto finish;
    }
    if (args.fast && !profile_apply(true)) {
        printf("glitch ERR fast profile write failed\n"); (void)restore_locked(); goto finish;
    }
    printf("glitch vd=%d td=%d vr=%d tr=%d tv=%d fast=%d hold=%d pulses=%d pw=%d gap=%d delay=%d revert=%d\n",
           args.vd,args.td,args.vr,args.tr,args.tv,args.fast,args.hold_ms,args.pulses,
           args.pw,args.gap,args.delay,args.revert);
    int64_t t0 = now_us() - s_plan[0].t_us;
    hb_touch(); s_fx.phase = PH_GLITCH_RUN; prepared = true;
    pmbus_seq_begin();
    wfail = glitch_run(s_plan, ns, t0);
    pmbus_seq_end();
    s_fx.phase = PH_GLITCH_PREP;
    if (wfail) (void)restore_locked();
    else if (args.fast && !profile_apply(false)) { wfail++; (void)restore_locked(); }
    vTaskDelay(pdMS_TO_TICKS(30));
    uint8_t sw[2] = {0}, cml = 0, e1[2] = {0};
    diag_ok = pmbus_r(0x78, sw, 2);
    diag_ok = pmbus_r(0x7E, &cml, 1) && diag_ok;
    diag_ok = pmbus_r(0xE1, e1, 2) && diag_ok;
    /* 0.5.5: the UV witness this bench never had, from the two registers that carry it.
     * NOT chained into diag_ok — an unimplemented optional register NACKing must not turn a
     * clean shot into a failed one (test/test_main.c pins that), and the values are printed
     * with their own _ok flag so a 0x00 read is never mistaken for an ACKed zero.
     * uv079 is (bit4 of STATUS_VOUT), i.e. the bit the old `vout_uv` name claimed and was not. */
    uint8_t w0b[2] = {0}, v079 = 0;
    const bool sw0b_ok = pmbus_r(0x0B, w0b, 2);
    const bool svout_ok = pmbus_r(0x79, &v079, 1);
    printf("diag sw=0x%02x%02x vout_uv=%d status_word=0x%02x%02x status_word_ok=%d "
           "status_vout=0x%02x uv079=%d status_vout_ok=%d valid=%d\n",
           sw[1], sw[0], (sw[0] >> 4) & 1, w0b[1], w0b[0], (int)sw0b_ok,
           v079, (v079 >> 4) & 1, (int)svout_ok, diag_ok);
    /* Telemetry comes before the complete end marker, never after it. */
    for (int i = 0; i < s_vsamp_n; i++) {
        int ex = (s_vsamp[i].raw >> 12) & 15; if (ex > 7) ex -= 16;
        long man = s_vsamp[i].raw & 0xfff;
        long mv = ex >= 0 ? man << ex : man >> -ex;
        printf("vs t=%+ldus v=%ldmV\n", (long)(s_vsamp[i].t_us - s_reset_slip_us), mv);
    }
    last_t result = { .valid=true,.diag_ok=diag_ok,.vd=args.vd,.wfail=wfail,
        .uv=(sw[0] >> 4) & 1,.sw=(uint16_t)(sw[0] | (sw[1] << 8)),
        .sw0b=(uint16_t)(w0b[0] | (w0b[1] << 8)),.svout=v079,
        .sw0b_ok=sw0b_ok,.svout_ok=svout_ok,
        .slip_us=s_glitch_slip_us,.reset_slip_us=s_reset_slip_us,.time_s=now_us()/1000000 };
    portENTER_CRITICAL(&s_state_lock);
    result.seq = s_last.seq + 1; if (!result.seq) result.seq = 1;
    s_last = result; seq = result.seq;
    portEXIT_CRITICAL(&s_state_lock);
    // The dip's TRAILING edge is a blocking PMBus write too, so `pw` only says when we
    // *ask* for the rail back. These lines turn that into a measured number: got= is when
    // the step ran relative to plan, dur= is how long its bus transaction blocked.
    for (int i = 0; i < s_prof_n; i++)
        printf("st %d act=%d want=%ld got=%ld dur=%dus\n", i, s_prof[i].act,
               (long)s_prof[i].want, (long)s_prof[i].got, (int)s_prof[i].dur);
    printf("glitch done pwd=%d e1=0x%02x%02x writes_failed=%d slip=%ld reset_slip=%ld diag_ok=%d"
           " stretch_us=%ld reanchor=%d\n",
           !!(cml & 8),e1[1],e1[0],wfail,(long)s_glitch_slip_us,(long)s_reset_slip_us,diag_ok,
           (long)s_anchor_shift_us,GA_REANCHOR);
finish:
    if (writes_failed) *writes_failed = wfail;
    if (sequence) *sequence = seq;
    hardware_leave();
    return prepared && diag_ok;
}

void board_status_json(char *out, int n)
{
    if (!out || n <= 0) return;
    static const char *ph[] = {"idle","cmd","prep","glitch"};
    portENTER_CRITICAL(&s_state_lock); last_t last = s_last; bool busy = s_busy; portEXIT_CRITICAL(&s_state_lock);
    char memo[448], note[192];
    size_t i = 0;
    for (const char *p = reset_note; *p && i < sizeof note - 1; p++)
        note[i++] = (*p == '"' || *p == '\\' || (unsigned char)*p < 32) ? ' ' : *p;
    note[i] = 0;
    int64_t now = now_us() / 1000000;
    if (last.valid) snprintf(memo,sizeof memo,
        "{\"vd\":%d,\"wfail\":%d,\"sw\":\"0x%04x\",\"vout_uv\":%d,"
        "\"status_word\":\"0x%04x\",\"status_word_ok\":%s,\"status_vout\":\"0x%02x\",\"status_vout_ok\":%s,"
        "\"diag_ok\":%s,\"seq\":%lu,\"age_s\":%lld,\"slip_us\":%ld,\"reset_slip_us\":%ld}",
        last.vd,last.wfail,last.sw,last.uv,
        last.sw0b,last.sw0b_ok?"true":"false",last.svout,last.svout_ok?"true":"false",
        last.diag_ok?"true":"false",(unsigned long)last.seq,
        (long long)(now-last.time_s),(long)last.slip_us,(long)last.reset_slip_us);
    else strcpy(memo,"null");
    int wrote = snprintf(out,(size_t)n,
        "{\"uptime_s\":%lld,\"boot\":%lu,\"reset\":\"%s\",\"baud\":%lu,\"requested_baud\":%lu,\"phase\":\"%s\",\"busy\":%s,\"reset_timing\":\"software\",\"exec_prio\":%u,\"crit_spin_us\":%d,\"reanchor\":%d,\"last\":%s}",
        (long long)(now-s_boot_t0),(unsigned long)s_fx.boot_count,note,
        (unsigned long)pmbus_baud_hz(),(unsigned long)pmbus_requested_baud_hz(),ph[s_fx.phase & 3],
        busy?"true":"false",
        (unsigned)(s_executor ? uxTaskPriorityGet(s_executor) : 0),GA_CRIT_SPIN_US,GA_REANCHOR,memo);
    if (wrote < 0 || wrote >= n) snprintf(out,(size_t)n,"{}");
}

static void campaign_cmd(const char *args)
{
    if (!*args || !strcmp(args,"status")) { char js[1024]; campaign_status_json(js,sizeof js); printf("campaign %s\n",js); return; }
    if (!strcmp(args,"stop")) { campaign_stop(); printf("campaign: stopping\n"); return; }
    campaign_cfg_t cfg = campaign_defaults; char error[96];
    if (!ga_campaign_options(args,&cfg,error,sizeof error) || !campaign_cfg_validate(&cfg,error,sizeof error)) {
        printf("ERR %s\n",error); return;
    }
    if (!board_begin_maintenance()) { printf("ERR hardware/campaign busy\n"); return; }
    bool started = campaign_start(&cfg); board_end_maintenance();
    printf(started ? "campaign: started\n" : "ERR campaign busy or invalid config\n");
}

/* The judge host's side of the console. `@V` runs the same admission test the HTTP route
 * applies (same tokens, the same campaign function), because a verdict accepted over one
 * transport and refused over the other would put two meanings on `under`. `@A` is a
 * heartbeat: it stores the agent's snapshot for the page and hands back whatever start
 * request the page has left, so the agent never needs a second channel to learn that a
 * human clicked something.
 */
static void peer_cmd(char op, const char *args)
{
    char error[96];
    if (op == 'V') {
        uint32_t session, attempt;
        oracle_t verdict;
        if (!ga_verdict_line(args, &session, &attempt, &verdict, error, sizeof error)) {
            printf("verdict ERR %s\n", error);
            return;
        }
        if (campaign_submit_verdict(session, attempt, verdict))
            printf("verdict accepted session=%lu attempt=%lu r=%s\n",
                   (unsigned long)session, (unsigned long)attempt, oracle_name(verdict));
        else
            printf("verdict stale: no shot is waiting for that session/attempt\n");
        return;
    }
    uint32_t seq, adopted;
    const char *json;
    if (!ga_agent_frame(args, &seq, &adopted, &json, error, sizeof error) ||
        !peer_store(json, seq, error, sizeof error)) {
        printf("peer ERR %s\n", error);
        return;
    }
    if (adopted) peer_adopted(adopted);
    char req[PEER_REQ_MAX];
    uint32_t req_seq = 0;
    if (peer_pending_request(req, sizeof req, &req_seq))
        printf("peer stored seq=%lu req=%lu %s\n",
               (unsigned long)seq, (unsigned long)req_seq, req);
    else
        printf("peer stored seq=%lu req=none\n", (unsigned long)seq);
}

static void dispatch(char *line)
{
    char op = line[0];
    const char *args = line + 1;
    if (op == '@') { op = line[1]; args = line + 2; }
    while (*args == ' ' || *args == '\t') args++;
    if (line[0] == '@' && op == 'C') { campaign_cmd(args); return; }
    /* The judge host's two commands are handled before the busy test and without the
     * hardware lock: a verdict arrives while the campaign holds the bus, and a heartbeat
     * must answer even mid-shot, because a snapshot that has to wait for the shot to
     * finish is not evidence that the bench is alive. Neither touches hardware. */
    if (line[0] == '@' && (op == 'V' || op == 'A')) { peer_cmd(op, args); return; }
    if (campaign_is_running() && !(line[0] != '@' && op == 'v')) {
        printf("ERR campaign busy; stop it before manual hardware commands\n"); return;
    }
    if (line[0] == 'g') {
        glitch_args_t a;
        if (!glitch_args_parse(&a,line + 1)) { printf("glitch ERR invalid numeric arguments\n"); return; }
        (void)board_execute_glitch(&a,NULL,NULL); return;
    }
    if (!hardware_enter()) { printf("ERR hardware busy\n"); return; }
    s_fx.phase = PH_CMD;
    s_fx.pc = (uint32_t)(uintptr_t)&dispatch;
    s_fx.lr = (uint32_t)(uintptr_t)__builtin_return_address(0);
    if (line[0] == '@') {
        switch (op) {
        case 'W': net_wifi_cmd(args); break;
        // @P (SMBPBI probe) and @O (verdict mode) left with 0.4.1: this bench
        // never got a reply from the GPU's SMBus endpoint, so there is nothing
        // to probe and no mode to select. Unknown @ commands fall through to
        // pmbus_cmd and answer `ERR unknown command`, which is the honest reply.
        default: pmbus_cmd(line + 1); break;  // original text, no last-byte rewrite
        }
    } else switch (op) {
    case 'v':
        printf("clk=%u MHz last reset: %s\n",(unsigned)CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ,reset_note);
        break;
    case 'u': unlock_cmd(); break;
    case 'z': lock_status_cmd(); break;
    case 'w': (void)restore_locked(); break;
    case 'k': profile_cmd(true); break;
    case 'K': profile_cmd(false); break;
    case 'r':
        printf("PEX_RST_BUF* low 100 ms (PCIe reset)\n");
        pex_hold(true); vTaskDelay(pdMS_TO_TICKS(100)); pex_hold(false); printf("released\n"); break;
    case 'j': pex_hold(true); printf("PEX_RST_BUF* HELD low; send 'J' to release (auto-releases at %d ms)\n", GA_PEX_MAX_HOLD_MS); break;
    case 'J': pex_hold(false); printf("PEX_RST_BUF* released\n"); break;
    case 'b': s_fx.magic=FX_MAGIC_SW; printf("restart\n"); esp_restart(); break;
    default: printf("ERR unknown command\n"); break;
    }
    hb_touch(); hardware_leave();
}

/* Guards the one hardware knob a human can leave in a bad state: 'j' holds PEX_RST with
 * no upper bound, and a forgotten hold is indistinguishable from a dead card -- we burned
 * a power cycle on exactly that confusion (2026-11-21, one 10 s hold). 10 ms polling is
 * an order of magnitude slower than the slowest plan step (~1 ms), so this can never
 * interfere with shot timing; it only ever catches manual holds. */
/* The decision itself, separate from the task so the offline model can drive it.
 * Returns true when it forced the pad high. */
static bool pex_guard_once(void)
{
    int64_t held = pex_held_us();
    if (held <= (int64_t)GA_PEX_MAX_HOLD_MS * 1000) return false;
    pex_hold(false);
    // printf (not console_printf): main.c has the real printf and does not include
    // console.h; the web log ring still sees it through the console redirect.
    printf("PEX_RST held %lld ms > %d ms cap -> auto-released "
           "(a forgotten 'j' looks exactly like a dead card)\n",
           (long long)(held / 1000), GA_PEX_MAX_HOLD_MS);
    return true;
}

static void pex_guard_task(void *arg)
{
    (void)arg;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(10));
        (void)pex_guard_once();
    }
}

static void executor_task(void *arg)
{
    (void)arg;
    wdt_start();
    vTaskDelay(pdMS_TO_TICKS(1500));
    printf("\nesp32-ga100vfi " GA_FW_VERSION ": core1 executor, I2C %d/%d PEX_RST OD@%d bus=%lu Hz reset=software\n",
           PIN_I2C_SDA,PIN_I2C_SCL,PIN_PEX_RST,(unsigned long)pmbus_baud_hz());
    printf("last reset: %s\n",reset_note);
    printf("commands: v u z g<args> w k K r j J b; @F @s @r @w @q @L @U @x; @W @C start|stop|status\n");
    printf("judge host: @V <session> <attempt> <goldilocks|hit|under|over|lost>\n");
    printf("            @A [seq=N] [adopted=N] {\"ascii\":\"json\"}  (reply carries the page's start request)\n");
    net_init();
    char line[GA_COMMAND_MAX];
    for (;;) {
        int n = console_read_command(line,sizeof line,10);
        hb_touch();
        if (n > 0) dispatch(line);
        campaign_poll();
    }
}

void ga_app_main(void)
{
    pins_safe_boot(); pex_init(); reset_history_init();
    s_boot_t0 = now_us()/1000000;
    s_hardware = xSemaphoreCreateMutex();
    ESP_ERROR_CHECK(s_hardware ? ESP_OK : ESP_ERR_NO_MEM);
    console_init();
    campaign_init(board_execute_glitch,board_restore_control);
    peer_init();   // two RAM slots: the judge host's snapshot and the page's start request
    /* 优先级见 glitch_run 上方那段：必须**高于 esp_timer(22)**，否则本板自己的 50 ms 心跳回调
     * 就能把计划步推迟 100~200 µs（实测 GA_RST_REL 到达偏差 p50=118/max=219 µs）。24 高于
     * esp_timer，低于 IDF 少数 flash/cache 操作（那些操作期间本核本来就要停）。
     * 实际生效值报进 /api/status 的 exec_prio —— 请求值不算数（同 baud 那条闸门的原则）。 */
    ESP_ERROR_CHECK(xTaskCreatePinnedToCore(executor_task,"ga_executor",12288,NULL,
                                          GA_EXEC_PRIO,&s_executor,1) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
    // Small stack, low priority, no core pinning: it only polls a timestamp every 10 ms.
    ESP_ERROR_CHECK(xTaskCreate(pex_guard_task,"ga_pex_guard",2048,NULL,
                                6,NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
}
#ifndef ARDUINO
void app_main(void) { ga_app_main(); }
#endif
