#include "glitch_plan.h"

#include <ctype.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// VOUT_COMMAND: 6.25 mV/LSB, with a wide intermediate.
static int mv_to_code(int mv) { return (int)(((int64_t)mv * 16 + 50) / 100); }

void glitch_args_defaults(glitch_args_t *a)
{
    a->vd = 325; a->td = -100; a->vr = 650; a->tr = -20; a->tv = 0;
    a->fast = 1; a->hold_ms = 0; a->pulses = 1; a->pw = 0; a->gap = 0;
    a->delay = 75; a->revert = 1; a->swin = 0;
}

bool glitch_args_parse(glitch_args_t *a, const char *rest)
{
    if (!a || !rest) return false;
    glitch_args_t parsed;
    glitch_args_defaults(&parsed);
    int *field[] = {
        &parsed.vd, &parsed.td, &parsed.vr, &parsed.tr, &parsed.tv,
        &parsed.fast, &parsed.hold_ms, &parsed.pulses, &parsed.pw,
        &parsed.gap, &parsed.delay, &parsed.revert, &parsed.swin,
    };
    size_t i = 0;
    const char *p = rest;
    while (true) {
        while (isspace((unsigned char)*p)) p++;
        if (!*p) break;
        if (i == sizeof field / sizeof field[0]) return false;
        errno = 0;
        char *end;
        long v = strtol(p, &end, 10);
        if (end == p || errno == ERANGE || v < INT_MIN || v > INT_MAX ||
            (*end && !isspace((unsigned char)*end))) return false;
        *field[i++] = (int)v;
        p = end;
    }
    *a = parsed;
    return true;
}

static bool invalid(char *error, size_t n, const char *message)
{
    if (error && n) snprintf(error, n, "%s", message);
    return false;
}

static bool within(int v, int lo, int hi) { return v >= lo && v <= hi; }

bool glitch_args_validate(const glitch_args_t *a, char *error, size_t n)
{
    if (!a) return invalid(error, n, "missing glitch arguments");
    if (!within(a->vd, GLITCH_MIN_MV, GLITCH_MAX_MV) ||
        !within(a->vr, GLITCH_MIN_MV, GLITCH_MAX_MV))
        return invalid(error, n, "vd/vr must be 0..1000 mV");
    if (!within(a->td, -GLITCH_MAX_TIME_US, GLITCH_MAX_TIME_US) ||
        !within(a->tr, -GLITCH_MAX_TIME_US, GLITCH_MAX_TIME_US) ||
        !within(a->tv, 0, GLITCH_MAX_TIME_US) ||
        !within(a->delay, 0, GLITCH_MAX_TIME_US) ||
        !within(a->pw, -GLITCH_MAX_TIME_US, GLITCH_MAX_TIME_US) ||
        !within(a->gap, -GLITCH_MAX_TIME_US, GLITCH_MAX_TIME_US))
        return invalid(error, n, "time fields exceed bounds (tv/delay must be nonnegative)");
    if (!within(a->fast, 0, 1) || !within(a->revert, 0, 1))
        return invalid(error, n, "fast/revert must be 0 or 1");
    if (!within(a->pulses, 1, GLITCH_MAX_PULSES))
        return invalid(error, n, "pulses must be 1..16");
    if (!within(a->hold_ms, 0, GLITCH_MAX_HOLD_MS))
        return invalid(error, n, "hold must be 0..10000 ms");
    if (!within(a->swin, 0, GLITCH_MAX_SAMPLE_US))
        return invalid(error, n, "sample window must be 0..20000 us");

    int64_t pw = a->pw;
    if (pw <= 0) {
        if (a->tr <= a->td)
            return invalid(error, n, "auto pulse width requires tr > td");
        pw = ((int64_t)a->tr - a->td) / (2 * a->pulses - 1);
        if (pw < 1) pw = 1;
    }
    int64_t gap = a->gap > 0 ? a->gap : pw;
    int64_t first = (int64_t)a->td - a->delay;
    if (first < -900000)
        return invalid(error, n, "first pulse precedes preparation by >900 ms");
    int64_t train_end = first + (a->pulses - 1) * (pw + gap) + pw;
    int64_t post = a->tv;
    if (train_end > post) post = train_end + gap;
    int64_t end = post + 60;
    if (a->swin > end) end = a->swin;
    if (a->revert && post + 500000 > end) end = post + 500000;
    if (a->hold_ms) {
        int64_t hold_end = post + 100 + (int64_t)a->hold_ms * 1000;
        if (a->revert) hold_end++;
        if (hold_end > end) end = hold_end;
    }
    int64_t prep_base = (first < -1000) ? first - 1100 : -2100;
    if (end - prep_base > GLITCH_MAX_DURATION_US)
        return invalid(error, n, "derived timeline exceeds 15 seconds");
    if (error && n) error[0] = 0;
    return true;
}

uint32_t glitch_pmbus_w_us(uint32_t baud_hz)
{
    if (baud_hz == 0) baud_hz = 1;
    return (uint32_t)((36000000ULL + baud_hz - 1u) / baud_hz);
}

void glitch_args_finalize(glitch_args_t *a, uint32_t pmbus_write_us)
{
    if (!a) return;
    if (a->pulses < 1) a->pulses = 1;
    if (a->pulses > GLITCH_MAX_PULSES) a->pulses = GLITCH_MAX_PULSES;
    int64_t pw = a->pw;
    if (pw <= 0) pw = ((int64_t)a->tr - a->td) / (2 * a->pulses - 1);
    int64_t gap = a->gap > 0 ? a->gap : pw;
    int64_t floor = pmbus_write_us ? pmbus_write_us : 1;
    if (pw < floor) pw = floor;
    if (gap < floor) gap = floor;
    // Invalid input stays rejectable by validate instead of overflowing.
    a->pw = (int)(pw > INT_MAX ? INT_MAX : pw);
    a->gap = (int)(gap > INT_MAX ? INT_MAX : gap);
}

static bool append(gstep_t *st, int *ns, int64_t t, uint8_t act, int arg)
{
    if (*ns >= GLITCH_MAX_STEPS || t < INT32_MIN || t > INT32_MAX) return false;
    st[(*ns)++] = (gstep_t){ (int32_t)t, act, arg };
    return true;
}

int glitch_build(const glitch_args_t *a, gstep_t *st, int max_steps, int32_t *out_post)
{
    if (!st || max_steps <= 0 || !glitch_args_validate(a, NULL, 0) ||
        a->pw <= 0 || a->gap <= 0) return -1;

    // Never expose a partial plan lacking restoration steps to the caller.
    gstep_t plan[GLITCH_MAX_STEPS];
    int ns = 0;
#define ADD(t, act, arg) do { if (!append(plan, &ns, (t), (act), (arg))) return -1; } while (0)
    // Early-dip plans (first < -1000 us) must anchor the fault-clear, VMIN and
    // reset-assert block BEFORE the dip, so the rail is already deep when the
    // card boots out of reset. Standard plans keep the original -2100 anchor.
    int64_t first = (int64_t)a->td - a->delay;
    int64_t prep_base = (first < -1000) ? first - 1100 : -2100;
    ADD(prep_base, GA_CLR, 0);
    ADD(prep_base + 800, GA_VMIN, 0x20);
    ADD(prep_base + 900, GA_MODE_ON, 0);
    ADD(prep_base + 1100, GA_RST_LOW, 0);
    ADD(prep_base + 1100, GA_VOUT, mv_to_code(600));

    int64_t train_end = 0;
    int64_t t = (int64_t)a->td - a->delay;
    for (int p = 0; p < a->pulses; p++) {
        ADD(t, GA_VOUT, mv_to_code(a->vd));
        train_end = t + a->pw;
        ADD(train_end, GA_VOUT, mv_to_code(a->vr));
        t += (int64_t)a->pw + a->gap;
    }
    ADD(0, GA_RST_REL, 0);

    int64_t post = a->tv;
    if (train_end > post) post = train_end + a->gap;
    ADD(post + 10, GA_VOUT, 0x7E);
    ADD(post + 60, GA_VMIN, 0x30);
    int64_t hold_start = post + 100;
    int64_t hold_end = hold_start + (int64_t)a->hold_ms * 1000;
    if (a->hold_ms > 0) {
        ADD(hold_start, GA_SCL_LOW, 0);
        ADD(hold_end, GA_SCL_REL, 0);
    }
    if (a->revert) {
        int64_t mode_at = post + 500000;
        if (a->hold_ms && mode_at <= hold_end) mode_at = hold_end + 1;
        ADD(mode_at, GA_MODE, 0);
    }

    if (a->swin > 0) {
        for (int64_t at = (prep_base < -2000 ? prep_base : -2000); at <= a->swin; at += 500) {
            if (a->hold_ms && at >= hold_start && at < hold_end) continue;
            ADD(at, GA_VS, 0);
        }
    }
#undef ADD
    if (ns > max_steps) return -1;
    for (int i = 1; i < ns; i++) {
        gstep_t k = plan[i];
        int j = i - 1;
        while (j >= 0 && plan[j].t_us > k.t_us) { plan[j + 1] = plan[j]; j--; }
        plan[j + 1] = k;
    }
    memcpy(st, plan, (size_t)ns * sizeof *st);
    if (out_post) *out_post = (int32_t)post;
    return ns;
}
