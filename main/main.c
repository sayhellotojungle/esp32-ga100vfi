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
#define FX_COOKIE 0x47414632u
#define FX_MAGIC_WDT 0xfa170002u
#define FX_MAGIC_SW 0xfa170003u
#define GA_VS_MAX 48

enum { PH_IDLE, PH_CMD, PH_GLITCH_PREP, PH_GLITCH_RUN };
typedef struct {
    uint32_t cookie, cookie_inv, magic, phase, pc, lr;
    int32_t step_t;
    uint32_t step_act, step_idx, boot_count;
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
                 "%s reason=%d phase=%u step=%u act=%u t=%ldus pc=%08lx lr=%08lx",
                 kind, (int)reason, (unsigned)s_fx.phase, (unsigned)s_fx.step_idx,
                 (unsigned)s_fx.step_act, (long)s_fx.step_t,
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
    if (s_fx.phase == PH_GLITCH_RUN &&
        (uint32_t)((uint32_t)now_us() - s_hb) >= 250000u) {
        s_fx.magic = FX_MAGIC_WDT;
        pex_hold(false);
        esp_restart();
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

static int IRAM_ATTR glitch_run(const gstep_t *st, int ns, int64_t t0)
{
    int wfail = 0;
    int64_t actual_reset = -1;
    s_glitch_slip_us = 0; s_reset_slip_us = 0; s_vsamp_n = 0; s_prof_n = 0;
    for (int i = 0; i < ns; i++) {
        const gstep_t *s = &st[i];
        int64_t want = t0 + s->t_us;
        while (now_us() < want) {
            hb_touch();
            /* Long holds/revert waits must let the idle task run. */
            if (want - now_us() > (int64_t)portTICK_PERIOD_MS * 1000 + 1500)
                vTaskDelay(1);
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
            s_reset_slip_us = (int32_t)(actual_reset - t0);
            break;
        case GA_SCL_LOW: pmbus_scl_hold(true); break;
        case GA_SCL_REL: pmbus_scl_hold(false); break;
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
            s_prof[s_prof_n].want = s->t_us;
            s_prof[s_prof_n].got = (int32_t)(step_start - t0);
            s_prof[s_prof_n].dur = (int16_t)(now_us() - step_start);
            s_prof[s_prof_n].act = (uint8_t)s->act;
            s_prof_n++;
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
    wfail = glitch_run(s_plan, ns, t0);
    s_fx.phase = PH_GLITCH_PREP;
    if (wfail) (void)restore_locked();
    else if (args.fast && !profile_apply(false)) { wfail++; (void)restore_locked(); }
    vTaskDelay(pdMS_TO_TICKS(30));
    uint8_t sw[2] = {0}, cml = 0, e1[2] = {0};
    diag_ok = pmbus_r(0x78, sw, 2);
    diag_ok = pmbus_r(0x7E, &cml, 1) && diag_ok;
    diag_ok = pmbus_r(0xE1, e1, 2) && diag_ok;
    printf("diag sw=0x%02x%02x vout_uv=%d valid=%d\n", sw[1],sw[0],(sw[0] >> 4) & 1,diag_ok);
    /* Telemetry comes before the complete end marker, never after it. */
    for (int i = 0; i < s_vsamp_n; i++) {
        int ex = (s_vsamp[i].raw >> 12) & 15; if (ex > 7) ex -= 16;
        long man = s_vsamp[i].raw & 0xfff;
        long mv = ex >= 0 ? man << ex : man >> -ex;
        printf("vs t=%+ldus v=%ldmV\n", (long)(s_vsamp[i].t_us - s_reset_slip_us), mv);
    }
    last_t result = { .valid=true,.diag_ok=diag_ok,.vd=args.vd,.wfail=wfail,
        .uv=(sw[0] >> 4) & 1,.sw=(uint16_t)(sw[0] | (sw[1] << 8)),
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
    printf("glitch done pwd=%d e1=0x%02x%02x writes_failed=%d slip=%ld reset_slip=%ld diag_ok=%d\n",
           !!(cml & 8),e1[1],e1[0],wfail,(long)s_glitch_slip_us,(long)s_reset_slip_us,diag_ok);
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
    char memo[320], note[192];
    size_t i = 0;
    for (const char *p = reset_note; *p && i < sizeof note - 1; p++)
        note[i++] = (*p == '"' || *p == '\\' || (unsigned char)*p < 32) ? ' ' : *p;
    note[i] = 0;
    int64_t now = now_us() / 1000000;
    if (last.valid) snprintf(memo,sizeof memo,
        "{\"vd\":%d,\"wfail\":%d,\"sw\":\"0x%04x\",\"vout_uv\":%d,\"diag_ok\":%s,\"seq\":%lu,\"age_s\":%lld,\"slip_us\":%ld,\"reset_slip_us\":%ld}",
        last.vd,last.wfail,last.sw,last.uv,last.diag_ok?"true":"false",(unsigned long)last.seq,
        (long long)(now-last.time_s),(long)last.slip_us,(long)last.reset_slip_us);
    else strcpy(memo,"null");
    int wrote = snprintf(out,(size_t)n,
        "{\"uptime_s\":%lld,\"boot\":%lu,\"reset\":\"%s\",\"baud\":%lu,\"requested_baud\":%lu,\"phase\":\"%s\",\"busy\":%s,\"reset_timing\":\"software\",\"last\":%s}",
        (long long)(now-s_boot_t0),(unsigned long)s_fx.boot_count,note,
        (unsigned long)pmbus_baud_hz(),(unsigned long)pmbus_requested_baud_hz(),ph[s_fx.phase & 3],
        busy?"true":"false",memo);
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
    /* Below esp_timer's priority 22; all hardware driver init happens on core1. */
    ESP_ERROR_CHECK(xTaskCreatePinnedToCore(executor_task,"ga_executor",12288,NULL,
                                          8,NULL,1) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
    // Small stack, low priority, no core pinning: it only polls a timestamp every 10 ms.
    ESP_ERROR_CHECK(xTaskCreate(pex_guard_task,"ga_pex_guard",2048,NULL,
                                6,NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
}
#ifndef ARDUINO
void app_main(void) { ga_app_main(); }
#endif
