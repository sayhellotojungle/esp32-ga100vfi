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
/* A lost verdict says the card left PCIe; it says nothing about dip depth. It used to be
 * charged to `over`, which made the ladder climb *shallower* while every remaining shot was
 * fired at an absent card. Counted on its own now, and it cannot be ground forever either:
 * twice at one point, or three times in a row, ends the campaign (2026-11-20's post-mortem:
 * "lost -> one more shot -> poison"). */
#define CAMPAIGN_MAX_LOST_POINT   2
#define CAMPAIGN_MAX_LOST_STREAK  3
/* Width of point_t.tried in codes (64 * 6.25 mV = 400 mV of search band). */
#define LADDER_WINDOW_CODES       64

typedef enum {
    ST_IDLE, ST_READY, ST_EXECUTING, ST_SETTLE, ST_WAIT_VERDICT,
    ST_RESTORE, ST_RESTORING
} state_t;

typedef struct {
    int vd, step, direction;
    int under, over, hit, lost, visit_tries;
    bool searching;
    /* Ladder codes already fired at this point, bit (code - code_base). This exists because
     * the VRM's LSB is 6.25 mV: a millivolt ladder can spend four shots on two coordinates
     * (2026-11-26: 372/370/368/366 -> codes 60/59/59/59). Refusing to move onto a code we
     * already judged is the difference between searching 14 coordinates and burning a batch.
     * Only used when the band holds more than one code — see ladder_next_locked(). */
    uint64_t tried;
} point_t;

typedef struct {
    bool running, goldilocks, awaiting_verdict, stop_requested, restore_ok;
    char phase[20];
    int delay, v_dip, v_code, step;
    int codes_tried;                        /* distinct ladder codes this point has fired */
    int tries, valid, lost, nack, timeouts, under, over, hit;
    int lost_streak;                          /* consecutive lost verdicts, any point */
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
    /* The code is the coordinate; v_dip is only what the operator typed. Reporting both is
     * what keeps a 2 mV "ladder" from being read back as four different shots (glitch_plan.h). */
    s_st.v_code = glitch_mv_to_code(s_points[s_point].vd);
    s_st.step = s_points[s_point].step;
}

/* ---- The ladder walks PMBus codes, not millivolts ----
 *
 * `step` stays in the millivolts the operator typed (it is what the console and the status
 * block echo), but every move is quantised through glitch_mv_to_code() with a floor of one
 * code. Without that, step=2 mV asks for 372 -> 370 -> 368 -> 366 and the VRM sees
 * code 60 -> 59 -> 59 -> 59: three shots spent, two coordinates covered. Codes already fired
 * at this point are skipped, because re-landing on one is the identical physical shot, not a
 * retry of a noisy measurement.
 */
static int ladder_bit(int code, int lo)
{
    const int idx = code - lo;
    return (idx >= 0 && idx < LADDER_WINDOW_CODES) ? idx : -1;
}

/* Millivolts to *fire* for a ladder position. A code we have not moved off keeps the number
 * the operator typed: code 60 is 372 mV and 375 mV at once, and re-deriving it as 375 would
 * split one physical coordinate across two entries in the ledger (verdicts.tsv and the
 * envelope's family key both index on the millivolts we asked for). */
static int ladder_mv(const point_t *p, int code)
{
    return code == glitch_mv_to_code(p->vd) ? p->vd : glitch_code_to_mv(code);
}

/* Returns the millivolts to fire next; *exhausted says every code between min_v and max_v has
 * already been fired at this point, which is a result, not a reason to keep shooting.
 * *new_direction may flip: when the requested direction runs into a code already fired (or
 * into the wall of the band), the honest move is the nearest untried code, which can be
 * behind us. */
static int ladder_next_locked(const point_t *p, int direction, bool *exhausted,
                              int *new_direction)
{
    const int lo = glitch_mv_to_code(s_cfg.min_v), hi = glitch_mv_to_code(s_cfg.max_v);
    *exhausted = false;
    *new_direction = direction;

    int codes = (s_cfg.step * GLITCH_MV_PER_CODE_NUM +
                 GLITCH_MV_PER_CODE_DEN / 2) / GLITCH_MV_PER_CODE_DEN;
    if (codes < 1) codes = 1;                       /* one code is the smallest real step */
    const int cur = glitch_mv_to_code(p->vd);
    if (hi < lo) return p->vd;
    /* VD=MINV=MAXV is the hammer: one coordinate, fired again and again on purpose. "You
     * already tried that" is not an answer to that request, so the whole walk is off. */
    if (hi == lo) return p->vd;
    /* The mask only covers LADDER_WINDOW_CODES coordinates. A band wider than that is a
     * coarse grid the operator is rotating through on purpose; keep the old clamp behaviour
     * instead of pretending we know what has been tried. */
    if (hi - lo + 1 > LADDER_WINDOW_CODES) {
        int code = cur + direction * codes;
        if (code < lo) code = lo;
        if (code > hi) code = hi;
        return ladder_mv(p, code);
    }

    int cand = cur + direction * codes;
    if (cand < lo) cand = lo;
    if (cand > hi) cand = hi;
    int bit = ladder_bit(cand, lo);
    if (bit < 0 || !(p->tried & (UINT64_C(1) << bit))) return ladder_mv(p, cand);

    /* Nearest untried code in the requested direction, then coming back the other way. */
    for (int pass = 0; pass < 2; pass++) {
        const int dir = pass ? -direction : direction;
        for (int code = cur + dir * codes; code >= lo && code <= hi; code += dir) {
            const int b = ladder_bit(code, lo);
            if (b >= 0 && (p->tried & (UINT64_C(1) << b))) continue;
            *new_direction = dir;
            return ladder_mv(p, code);
        }
    }
    *exhausted = true;
    return p->vd;
}

static void ladder_mark_locked(point_t *p, int mv)
{
    const int lo = glitch_mv_to_code(s_cfg.min_v);
    const int bit = ladder_bit(glitch_mv_to_code(mv), lo);
    if (bit >= 0) p->tried |= UINT64_C(1) << bit;
}

static int ladder_tried_count(const point_t *p)
{
    int n = 0;
    for (uint64_t m = p->tried; m; m >>= 1) n += (int)(m & 1u);
    return n;
}

void campaign_args_for(const campaign_cfg_t *cfg, int vd, int delay,
                     glitch_args_t *args)
{
    glitch_args_defaults(args);
    args->vd = vd; args->td = cfg->td;
    args->vr = cfg->vr; args->tr = cfg->tr;
    args->tv = 0; args->fast = 1; args->hold_ms = 0;
    args->pulses = cfg->pulses; args->pw = cfg->pw; args->gap = cfg->gap;
    args->delay = delay; args->revert = cfg->revert; args->swin = cfg->swin;
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
            campaign_args_for(cfg, voltages[j], cfg->delays[i], &args);
            if (!glitch_args_validate(&args, error, n)) return false;
        }
    }
    return true;
}

void campaign_init(campaign_execute_fn execute, campaign_restore_fn restore)
{
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
    // Nothing to pick per run any more: every shot is judged by the host, so a
    // rejected or busy request cannot leave a stale verdict mode behind either.
    memset(&s_st, 0, sizeof s_st);
    s_st.running = true; s_st.restore_ok = true;
    s_st.session = session;
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
        } else if (s_st.running && !s_st.stop_requested &&
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
    /* Only a reading that came back says anything about depth. The streak is per run of
     * verdicts, so one judged shot between two losses does not buy a third lost. */
    if (verdict != ORACLE_LOST) s_st.lost_streak = 0;
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
    case ORACLE_LOST:
        /* 0.5.5: this used to be `p->over++`. A card that left PCIe is not evidence that the
         * dip was too deep: the ladder climbed shallower on every lost, and the shots kept
         * going out at a card nobody could read (2026-11-20: three lost rounds, each with
         * tries=2 — one judged lost and one nobody ever judged, docs/UNLOCK_AND_POISON.md §四). It now moves
         * nothing and stops the campaign instead. */
        p->lost++; s_st.lost_streak++;
        if (p->lost >= CAMPAIGN_MAX_LOST_POINT ||
            s_st.lost_streak >= CAMPAIGN_MAX_LOST_STREAK) {
            finish_locked("lost", false);
            return;
        }
        break;
    }
    if (p->searching && p->under + p->over + p->hit >= s_cfg.batch) {
        if (p->hit) p->searching = false;
        else if (p->under && p->over) {
            p->step = p->step / 2 < 6 ? 6 : p->step / 2;   /* 6 mV = one code: nothing finer */
        } else {
            int direction = p->under ? -1 : 1;
            if (p->direction && p->direction != direction)
                p->step = p->step / 2 < 6 ? 6 : p->step / 2;
            p->direction = direction;
            bool exhausted = false;
            const int next = ladder_next_locked(p, direction, &exhausted, &p->direction);
            if (exhausted) {
                /* Every code in [min_v,max_v] was fired at this delay. With a 6.25 mV LSB
                 * that band is 14 coordinates, not the 81 that the millivolt range suggests —
                 * so this point is done, and re-firing a duplicate is not searching. */
                finish_locked("exhausted", false);
                return;
            }
            p->vd = next;
        }
        p->under = p->over = p->hit = 0;
    }
    next_locked();
}

void campaign_poll(void)
{
    if (!s_mx) return;
    enum { NONE, EXECUTE, RESTORE } action = NONE;
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
                campaign_args_for(&s_cfg, s_st.v_dip, s_st.delay, &args);
                /* Record the coordinate as fired, so the ladder never spends another shot on
                 * a code it already judged. Marked here and not at the move, because a write
                 * that NACKs is retried at the same coordinate and was never judged. */
                ladder_mark_locked(&s_points[s_point], s_st.v_dip);
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
            // The settle interval is the card's own boot window; after it the
            // shot is only half-fired. Nothing on this board can tell whether the
            // fuse block moved (docs/UNLOCK_AND_POISON.md §一: the SMBPBI
            // endpoint never answered), so the campaign waits for the host.
            // MANUAL_TIMEOUT_US is the budget for that; nobody answering counts
            // the shot as timed out and infers no voltage direction.
            s_state = ST_WAIT_VERDICT;
            s_st.awaiting_verdict = true;
            s_deadline = now + MANUAL_TIMEOUT_US;
            phase_locked("waiting");
        } else if (s_state == ST_WAIT_VERDICT && now >= s_deadline) {
            s_st.awaiting_verdict = false;
            s_st.timeouts++;
            /* 0.5.0: the campaign ends here instead of taking the next coordinate.
             * Nobody answering no longer means "the human is busy and will get to it";
             * the agent is the judge and it is a process a human starts, so silence has
             * exactly one meaning: the side that reads BAR0 is gone. Continuing would
             * fire a fresh shot every 120 s at a card nobody is looking at -- with the
             * usual max=60 that is up to two hours of blind fuse attempts, and a
             * coordinate spent is a coordinate spent. Inferring no voltage direction was
             * already the rule; this makes the campaign stop drawing coordinates too. */
            finish_locked("no-judge", false);
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
        /* Pointed at from the same lock so the count cannot mix two points' masks while the
         * grid rotates. */
        s.codes_tried = ladder_tried_count(&s_points[s_point]);
        xSemaphoreGive(s_mx);
    }
    // The status block is copied under the mutex so a verdict cannot interleave
    // half-updated counters into /api/status.
    // v_code is the coordinate that physically exists (6.25 mV/LSB); v_dip is the millivolt
    // the operator typed. codes_tried says how much of [min_v,max_v] this point has spent —
    // the band is 14 codes wide, so "tries=40" and "codes_tried=14" together are the whole
    // story of a batch that looked like a search and was a re-run.
    snprintf(out, (size_t)n,
        "{\"running\":%s,\"phase\":\"%s\",\"delay\":%d,\"v_dip\":%d,\"v_code\":%d,"
        "\"codes_tried\":%d,"
        "\"tries\":%d,\"valid\":%d,\"lost\":%d,\"lost_streak\":%d,\"nack\":%d,\"timeouts\":%d,"
        "\"under\":%d,\"over\":%d,\"hit\":%d,\"goldilocks\":%s,"
        "\"best_delay\":%d,\"best_v_dip\":%d,"
        "\"session\":%" PRIu32 ",\"attempt\":%" PRIu32 ",\"sequence\":%" PRIu32 ","
        "\"awaiting_verdict\":%s,\"stop_requested\":%s,\"restore_ok\":%s}",
        s.running ? "true" : "false", s.phase, s.delay, s.v_dip, s.v_code,
        s.codes_tried,
        s.tries, s.valid, s.lost, s.lost_streak, s.nack, s.timeouts,
        s.under, s.over, s.hit, s.goldilocks ? "true" : "false",
        s.best_delay, s.best_vdip, s.session, s.attempt, s.sequence,
        s.awaiting_verdict ? "true" : "false",
        s.stop_requested ? "true" : "false", s.restore_ok ? "true" : "false");
}

const campaign_cfg_t campaign_defaults = {
    .vd = 354, .td = -60, .vr = 750, .tr = -20,
    .pulses = 3, .pw = 198, .gap = 150,
    .delays = { 117, 0, 0, 0, 0, 0 }, .ndelays = 1,
    .batch = 5, .min_v = 300, .max_v = 380, .step = 6,
    .max_attempts = 200, .restore_wait_ms = 300,
};
