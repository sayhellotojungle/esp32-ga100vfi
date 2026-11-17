#include "campaign.h"
#include "console.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_random.h"
#include "esp_timer.h"

#define CAMPAIGN_MAX_ATTEMPTS 1000000
#define CAMPAIGN_MAX_BATCH    10000
#define MANUAL_TIMEOUT_US    INT64_C(120000000)
#define RETRY_DELAY_US       INT64_C(400000)

typedef enum {
    ST_IDLE, ST_READY, ST_EXECUTING, ST_SETTLE, ST_WAIT_VERDICT,
    ST_CHECKING, ST_RESTORE, ST_RESTORING
} state_t;

typedef struct {
    int vd, step, direction;
    int under, over, hit, visit_tries;
    bool searching;
} point_t;

typedef struct {
    bool running, goldilocks, awaiting_verdict, stop_requested, restore_ok;
    char phase[20];
    int delay, v_dip, step, mode;
    int tries, valid, lost, nack, timeouts, under, over, hit;
    int best_delay, best_vdip;
    uint32_t session, attempt, sequence;
} stat_t;

static SemaphoreHandle_t s_mx;
static campaign_execute_fn s_execute;
static campaign_restore_fn s_restore;
static campaign_cfg_t s_cfg;
static stat_t s_st;
static point_t s_points[6];
static int s_point;
static state_t s_state;
static int64_t s_deadline;
static bool s_dirty, s_pending;
static oracle_t s_pending_verdict;
static uint32_t s_accepted_attempt;
static oracle_t s_accepted_verdict;
static char s_finish_phase[20];

static void phase_locked(const char *phase)
{
    snprintf(s_st.phase, sizeof s_st.phase, "%s", phase);
}

static void point_status_locked(void)
{
    s_st.delay = s_cfg.delays[s_point];
    s_st.v_dip = s_points[s_point].vd;
    s_st.step = s_points[s_point].step;
}

static void args_for(const campaign_cfg_t *cfg, int vd, int delay,
                     glitch_args_t *args)
{
    glitch_args_defaults(args);
    args->vd = vd; args->td = cfg->td;
    args->vr = cfg->vr; args->tr = cfg->tr;
    args->tv = 0; args->fast = 1; args->hold_ms = 0;
    args->pulses = cfg->pulses; args->pw = cfg->pw; args->gap = cfg->gap;
    args->delay = delay; args->revert = 0; args->swin = 0;
}

static bool config_error(char *error, size_t n, const char *message)
{
    if (error && n) snprintf(error, n, "%s", message);
    return false;
}

bool campaign_cfg_validate(const campaign_cfg_t *cfg, char *error, size_t n)
{
    if (error && n) error[0] = 0;
    if (!cfg) return config_error(error, n, "missing campaign config");
    if (cfg->ndelays < 1 || cfg->ndelays > 6)
        return config_error(error, n, "ndelays must be 1..6");
    if (cfg->oracle_mode < -1 || cfg->oracle_mode > 1)
        return config_error(error, n, "mode must be 0 or 1");
    if (cfg->max_attempts < 0 || cfg->max_attempts > CAMPAIGN_MAX_ATTEMPTS)
        return config_error(error, n, "max must be 0..1000000");
    if (cfg->batch < 0 || cfg->batch > CAMPAIGN_MAX_BATCH)
        return config_error(error, n, "batch must be 0..10000");
    if (cfg->min_v < GLITCH_MIN_MV || cfg->max_v > GLITCH_MAX_MV ||
        cfg->min_v > cfg->max_v || cfg->vd < cfg->min_v || cfg->vd > cfg->max_v)
        return config_error(error, n, "require safe min_v <= vd <= max_v");
    if (cfg->step < 1 || cfg->step > GLITCH_MAX_MV)
        return config_error(error, n, "step must be 1..1000 mV");
    if (cfg->restore_wait_ms < 250 || cfg->restore_wait_ms > 60000)
        return config_error(error, n, "settle must be 250..60000 ms");
    for (int i = 0; i < cfg->ndelays; i++) {
        const int voltages[] = { cfg->vd, cfg->min_v, cfg->max_v };
        for (unsigned j = 0; j < sizeof voltages / sizeof voltages[0]; j++) {
            glitch_args_t args;
            args_for(cfg, voltages[j], cfg->delays[i], &args);
            if (!glitch_args_validate(&args, error, n)) return false;
        }
    }
    return true;
}

void campaign_init(campaign_execute_fn execute, campaign_restore_fn restore)
{
    oracle_init();
    if (!s_mx) s_mx = xSemaphoreCreateMutex();
    s_execute = execute;
    s_restore = restore;
    if (s_mx) {
        xSemaphoreTake(s_mx, portMAX_DELAY);
        s_st.restore_ok = true;
        if (!s_st.running) phase_locked("idle");
        xSemaphoreGive(s_mx);
    }
}

bool campaign_is_running(void)
{
    if (!s_mx) return false;
    xSemaphoreTake(s_mx, portMAX_DELAY);
    bool running = s_st.running;
    xSemaphoreGive(s_mx);
    return running;
}

bool campaign_start(const campaign_cfg_t *cfg)
{
    if (!s_mx || !s_execute || !s_restore ||
        !campaign_cfg_validate(cfg, NULL, 0)) return false;
    xSemaphoreTake(s_mx, portMAX_DELAY);
    if (s_st.running) { xSemaphoreGive(s_mx); return false; }
    uint32_t previous_session = s_st.session;
    uint32_t session;
    do { session = esp_random(); } while (!session || session == previous_session);
    s_cfg = *cfg;
    int mode = cfg->oracle_mode < 0 ? oracle_get_mode() : cfg->oracle_mode;
    // No rejected/busy request may change the oracle of an active campaign.
    oracle_set_mode(mode);
    memset(&s_st, 0, sizeof s_st);
    s_st.running = true; s_st.restore_ok = true;
    s_st.session = session; s_st.mode = mode;
    memset(s_points, 0, sizeof s_points);
    for (int i = 0; i < cfg->ndelays; i++) {
        s_points[i].vd = cfg->vd;
        s_points[i].step = cfg->step;
        s_points[i].searching = cfg->batch > 0;
    }
    s_point = 0;
    s_state = ST_READY;
    s_deadline = 0;
    s_dirty = false; s_pending = false;
    s_accepted_attempt = 0;
    point_status_locked();
    phase_locked(cfg->batch > 0 ? "search" : "grind");
    xSemaphoreGive(s_mx);
    console_printf("campaign: session=%" PRIu32 " started\n", session);
    return true;
}

void campaign_stop(void)
{
    if (!s_mx) return;
    xSemaphoreTake(s_mx, portMAX_DELAY);
    if (s_st.running) {
        s_st.stop_requested = true;
        s_st.awaiting_verdict = false;
    }
    xSemaphoreGive(s_mx);
}

bool campaign_submit_verdict(uint32_t session, uint32_t attempt, oracle_t verdict)
{
    if (!s_mx || !session || !attempt ||
        verdict < ORACLE_GOLDILOCKS || verdict > ORACLE_LOST) return false;
    xSemaphoreTake(s_mx, portMAX_DELAY);
    bool ok = false;
    if (session == s_st.session) {
        if (attempt == s_accepted_attempt) {
            ok = verdict == s_accepted_verdict;
        } else if (s_st.running && !s_st.stop_requested && s_st.mode == 1 &&
                   s_state == ST_WAIT_VERDICT && s_st.awaiting_verdict &&
                   attempt == s_st.attempt && !s_pending) {
            s_pending_verdict = verdict;
            s_pending = true;
            s_accepted_attempt = attempt;
            s_accepted_verdict = verdict;
            s_st.awaiting_verdict = false;
            phase_locked("verdict");
            ok = true;
        }
    }
    xSemaphoreGive(s_mx);
    return ok;
}

static void finish_locked(const char *phase, bool success)
{
    s_st.awaiting_verdict = false;
    s_pending = false;
    if (!success && s_dirty) {
        snprintf(s_finish_phase, sizeof s_finish_phase, "%s", phase);
        s_state = ST_RESTORE;
        phase_locked("restoring");
    } else {
        s_st.running = false;
        s_state = ST_IDLE;
        phase_locked(phase);
    }
}

static int point_budget_locked(void)
{
    if (s_cfg.max_attempts > 0)
        return s_cfg.max_attempts / s_cfg.ndelays +
               (s_point < s_cfg.max_attempts % s_cfg.ndelays);
    if (s_cfg.ndelays == 1) return 0;
    // An unlimited grid rotates, retaining each point's adaptive state.
    return s_cfg.batch > 20 ? s_cfg.batch : 20;
}

static void next_locked(void)
{
    if (s_st.stop_requested) { finish_locked("stopped", false); return; }
    if ((s_cfg.max_attempts && s_st.tries >= s_cfg.max_attempts) ||
        s_st.attempt == UINT32_MAX) {
        finish_locked("limit", false);
        return;
    }
    int budget = point_budget_locked();
    if (budget && s_points[s_point].visit_tries >= budget) {
        s_points[s_point].visit_tries = 0;
        s_point++;
        if (s_point == s_cfg.ndelays) {
            if (s_cfg.max_attempts) { finish_locked("limit", false); return; }
            s_point = 0;
        }
    }
    point_status_locked();
    s_state = ST_READY;
    s_deadline = 0;
    phase_locked(s_points[s_point].searching ? "search" : "grind");
}

static void outcome_locked(oracle_t verdict)
{
    point_t *p = &s_points[s_point];
    if (verdict == ORACLE_LOST) s_st.lost++;
    else s_st.valid++;
    switch (verdict) {
    case ORACLE_GOLDILOCKS:
        s_st.hit++; s_st.goldilocks = true;
        s_st.best_delay = s_st.delay; s_st.best_vdip = s_st.v_dip;
        // Preserve the successful card state: no reset and no VRM hand-back.
        finish_locked("done", true);
        return;
    case ORACLE_HIT:
        s_st.hit++; p->hit++;
        s_st.best_delay = s_st.delay; s_st.best_vdip = s_st.v_dip;
        break;
    case ORACLE_UNDER: s_st.under++; p->under++; break;
    case ORACLE_OVER:  s_st.over++; p->over++; break;
    case ORACLE_LOST:  p->over++; break;
    }
    if (p->searching && p->under + p->over + p->hit >= s_cfg.batch) {
        if (p->hit) p->searching = false;
        else if (p->under && p->over) {
            p->step = p->step / 2 < 6 ? 6 : p->step / 2;
        } else {
            int direction = p->under ? -1 : 1;
            if (p->direction && p->direction != direction)
                p->step = p->step / 2 < 6 ? 6 : p->step / 2;
            p->direction = direction;
            p->vd += direction * p->step;
            if (p->vd < s_cfg.min_v) p->vd = s_cfg.min_v;
            if (p->vd > s_cfg.max_v) p->vd = s_cfg.max_v;
        }
        p->under = p->over = p->hit = 0;
    }
    next_locked();
}

void campaign_poll(void)
{
    if (!s_mx) return;
    enum { NONE, EXECUTE, CHECK, RESTORE } action = NONE;
    glitch_args_t args;
    uint32_t session = 0, attempt = 0;
    bool report = false;
    oracle_t reported = ORACLE_LOST;
    int64_t now = esp_timer_get_time();
    xSemaphoreTake(s_mx, portMAX_DELAY);
    if (!s_st.running) { xSemaphoreGive(s_mx); return; }
    if (s_pending) {
        reported = s_pending_verdict; report = true;
        session = s_st.session; attempt = s_st.attempt;
        s_pending = false;
        outcome_locked(reported);
    }
    if (s_st.running && s_st.stop_requested &&
        s_state != ST_RESTORE && s_state != ST_RESTORING)
        finish_locked("stopped", false);

    if (s_st.running) {
        if (s_state == ST_READY && now >= s_deadline && !report) {
            // Stop/max checks happen before every physical attempt.
            if ((s_cfg.max_attempts && s_st.tries >= s_cfg.max_attempts) ||
                s_st.attempt == UINT32_MAX) {
                finish_locked("limit", false);
            } else {
                args_for(&s_cfg, s_st.v_dip, s_st.delay, &args);
                s_st.tries++; s_st.attempt++;
                s_points[s_point].visit_tries++;
                s_st.sequence = 0;
                s_st.awaiting_verdict = false;
                s_dirty = true;
                s_state = ST_EXECUTING;
                phase_locked("executing");
                action = EXECUTE;
            }
        } else if (s_state == ST_SETTLE && now >= s_deadline) {
            if (s_st.mode == 1) {
                s_state = ST_WAIT_VERDICT;
                s_st.awaiting_verdict = true;
                s_deadline = now + MANUAL_TIMEOUT_US;
                phase_locked("waiting");
            } else {
                s_state = ST_CHECKING;
                phase_locked("checking");
                action = CHECK;
            }
        } else if (s_state == ST_WAIT_VERDICT && now >= s_deadline) {
            s_st.awaiting_verdict = false;
            s_st.timeouts++;
            next_locked();  // No verdict: do not infer a voltage direction.
        }
        if (s_state == ST_RESTORE) {
            s_state = ST_RESTORING;
            action = RESTORE;
        }
    }
    xSemaphoreGive(s_mx);

    if (report)
        console_printf("campaign: session=%" PRIu32 " attempt=%" PRIu32 " %s\n",
                       session, attempt, oracle_name(reported));
    if (action == EXECUTE) {
        int writes_failed = 0;
        uint32_t sequence = 0;
        bool ok = s_execute(&args, &writes_failed, &sequence);
        now = esp_timer_get_time();
        xSemaphoreTake(s_mx, portMAX_DELAY);
        s_st.sequence = sequence;
        if (!ok || writes_failed > 0) {
            s_st.nack++;
            next_locked();
            if (s_state == ST_READY) {
                s_deadline = now + RETRY_DELAY_US;
                phase_locked("retry");
            }
        } else if (s_st.stop_requested) {
            finish_locked("stopped", false);
        } else {
            s_state = ST_SETTLE;
            s_deadline = now + (int64_t)s_cfg.restore_wait_ms * 1000;
            phase_locked("settle");
        }
        xSemaphoreGive(s_mx);
    } else if (action == CHECK) {
        bool lost = false;
        oracle_t verdict = oracle_run_smbpbi(&lost);
        if (lost) verdict = ORACLE_LOST;
        xSemaphoreTake(s_mx, portMAX_DELAY);
        session = s_st.session; attempt = s_st.attempt;
        outcome_locked(verdict);
        xSemaphoreGive(s_mx);
        console_printf("campaign: session=%" PRIu32 " attempt=%" PRIu32 " %s\n",
                       session, attempt, oracle_name(verdict));
    } else if (action == RESTORE) {
        bool ok = s_restore();
        xSemaphoreTake(s_mx, portMAX_DELAY);
        s_st.restore_ok = ok;
        s_st.running = false;
        s_state = ST_IDLE;
        s_dirty = false;
        phase_locked(ok ? s_finish_phase : "restore_failed");
        xSemaphoreGive(s_mx);
        console_printf("campaign: finished (restore=%s)\n", ok ? "ok" : "failed");
    }
}

void campaign_status_json(char *out, int n)
{
    if (!out || n <= 0) return;
    stat_t s = { 0 };
    if (s_mx) {
        xSemaphoreTake(s_mx, portMAX_DELAY);
        s = s_st;
        xSemaphoreGive(s_mx);
    }
    // While idle the reported mode follows @O; an active run is immutable.
    if (!s.running) s.mode = oracle_get_mode();
    snprintf(out, (size_t)n,
        "{\"running\":%s,\"phase\":\"%s\",\"delay\":%d,\"v_dip\":%d,"
        "\"tries\":%d,\"valid\":%d,\"lost\":%d,\"nack\":%d,\"timeouts\":%d,"
        "\"under\":%d,\"over\":%d,\"hit\":%d,\"goldilocks\":%s,"
        "\"best_delay\":%d,\"best_v_dip\":%d,\"mode\":%d,"
        "\"session\":%" PRIu32 ",\"attempt\":%" PRIu32 ",\"sequence\":%" PRIu32 ","
        "\"awaiting_verdict\":%s,\"stop_requested\":%s,\"restore_ok\":%s}",
        s.running ? "true" : "false", s.phase, s.delay, s.v_dip,
        s.tries, s.valid, s.lost, s.nack, s.timeouts,
        s.under, s.over, s.hit, s.goldilocks ? "true" : "false",
        s.best_delay, s.best_vdip, s.mode, s.session, s.attempt, s.sequence,
        s.awaiting_verdict ? "true" : "false",
        s.stop_requested ? "true" : "false", s.restore_ok ? "true" : "false");
}

const campaign_cfg_t campaign_defaults = {
    .vd = 354, .td = -60, .vr = 750, .tr = -20,
    .pulses = 3, .pw = 198, .gap = 150,
    .delays = { 117, 0, 0, 0, 0, 0 }, .ndelays = 1,
    .batch = 5, .min_v = 300, .max_v = 380, .step = 6,
    .max_attempts = 200, .restore_wait_ms = 300, .oracle_mode = -1,
};
