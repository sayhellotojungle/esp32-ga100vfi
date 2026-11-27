/* Host-side unit tests for the glitch timeline builder (glitch_plan.c).
 * Compile with the plain host compiler; see run_tests.sh / run_tests.ps1.
 */
#include <stdio.h>
#include <string.h>
#include <limits.h>

#include "glitch_plan.h"

static int failures, checks;

#define CHECK(cond)                                                       \
    do {                                                                  \
        checks++;                                                         \
        if (!(cond)) {                                                    \
            failures++;                                                   \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);        \
        }                                                                 \
    } while (0)

static uint32_t W400 = 90;   /* glitch_pmbus_w_us(400000) */

static int build(const char *rest, uint32_t wus, gstep_t *st, int32_t *post)
{
    glitch_args_t a;
    if (!glitch_args_parse(&a, rest) || !glitch_args_validate(&a, NULL, 0)) return -1;
    glitch_args_finalize(&a, wus);
    if (!glitch_args_validate(&a, NULL, 0)) return -1;
    return glitch_build(&a, st, GLITCH_MAX_STEPS, post);
}

static int count_act(const gstep_t *st, int ns, uint8_t act, int32_t arg)
{
    int n = 0;
    for (int i = 0; i < ns; i++)
        if (st[i].act == act && (arg == -1 || st[i].arg == arg)) n++;
    return n;
}

static void check_sorted(const gstep_t *st, int ns)
{
    for (int i = 1; i < ns; i++) CHECK(st[i].t_us >= st[i - 1].t_us);
}

static void test_write_cost(void)
{
    CHECK(glitch_pmbus_w_us(1000000) == 36);
    CHECK(glitch_pmbus_w_us(400000) == 90);
    CHECK(glitch_pmbus_w_us(100000) == 360);
    CHECK(glitch_pmbus_w_us(0) == 36000000);
    CHECK(glitch_pmbus_w_us(UINT32_MAX) == 1);
}

/* The acceptance command from the task brief at 400 kHz. */
static void test_acceptance(void)
{
    gstep_t st[GLITCH_MAX_STEPS];
    int32_t post = -1;
    int ns = build(" 354 -60 750 -20 0 1 0 3 198 150 117 0", W400, st, &post);
    CHECK(ns == 14);
    check_sorted(st, ns);
    CHECK(post == 867);

    /* Stable tie order at t=-1000: RST_LOW enqueued before VOUT(600). */
    CHECK(st[0].t_us == -2100 && st[0].act == GA_CLR);
    CHECK(st[1].t_us == -1300 && st[1].act == GA_VMIN && st[1].arg == 0x20);
    CHECK(st[2].t_us == -1200 && st[2].act == GA_MODE_ON);
    CHECK(st[3].t_us == -1000 && st[3].act == GA_RST_LOW);
    CHECK(st[4].t_us == -1000 && st[4].act == GA_VOUT && st[4].arg == 96);
    CHECK(st[5].t_us == -177 && st[5].act == GA_VOUT && st[5].arg == 57);
    CHECK(st[6].t_us == 0 && st[6].act == GA_RST_REL);

    /* 354 mV dip codes to 57, 750 mV restore to 120; three pulses spaced by
     * pw+gap=348 with the delay=117 advance. */
    int dip[3], ris[3], nd = 0, nr = 0;
    for (int i = 0; i < ns; i++) {
        if (st[i].act != GA_VOUT) continue;
        if (st[i].arg == 57 && nd < 3) dip[nd++] = st[i].t_us;
        if (st[i].arg == 120 && nr < 3) ris[nr++] = st[i].t_us;
    }
    CHECK(nd == 3 && nr == 3);
    CHECK(dip[0] == -177 && dip[1] == 171 && dip[2] == 519);
    CHECK(ris[0] == 21 && ris[1] == 369 && ris[2] == 717);
    CHECK(count_act(st, ns, GA_VOUT, 0x7E) == 1);
    CHECK(count_act(st, ns, GA_VMIN, 0x30) == 1);
    CHECK(count_act(st, ns, GA_MODE, -1) == 0);   /* revert=0 */
    CHECK(count_act(st, ns, GA_SCL_LOW, -1) == 0);

    /* Pulse edges are never closer than one 16-bit PMBus write. */
    for (int i = 1; i < ns; i++) {
        int wr = st[i].act == GA_VOUT && (st[i].arg == 57 || st[i].arg == 120);
        int pw_prev = st[i - 1].act == GA_VOUT &&
                      (st[i - 1].arg == 57 || st[i - 1].arg == 120);
        if (wr && pw_prev)
            CHECK(st[i].t_us - st[i - 1].t_us >= (int32_t)W400);
    }
}

/* 重锚用的那面旗（0.5.9）：只有坑的第一笔写被标，别的都不许标。
 * 为什么值得钉住：整条波形的"物理零点"就是这个步的执行完成时刻，标错了地方等于把整发
 * 平移错——而平移错在 timeline 表里看起来仍然"很准点"（因为 want 也一起平移了），
 * 也就是说这个 bug 不会让表变难看，只会让几何悄悄换掉。必须有断言。 */
static void test_anchor_flag(void)
{
    /* 冻结配方：vd=372 td=-40 vr=750 tr=-20 tv=0 pulses=2 pw=222 gap=150 delay=64 revert=0 */
    gstep_t st[GLITCH_MAX_STEPS];
    int32_t post = -1;
    int ns = build(" 372 -40 750 -20 0 1 0 2 222 150 64 0", W400, st, &post);
    CHECK(ns > 0);
    check_sorted(st, ns);

    int n_anchor = 0, anchor_at = -1, rel_at = -1;
    for (int i = 0; i < ns; i++) {
        if (st[i].flags & GA_ANCHOR) { n_anchor++; anchor_at = i; }
        if (st[i].act == GA_RST_REL) rel_at = i;
        CHECK(st[i].flags == 0 || st[i].act == GA_VOUT);   /* 只可能标在 VOUT 上 */
    }
    CHECK(n_anchor == 1);
    CHECK(anchor_at >= 0 && st[anchor_at].arg == glitch_mv_to_code(372));
    CHECK(st[anchor_at].t_us == -40 - 64);          /* 坑按计划提前 delay 那么多 */
    CHECK(rel_at >= 0 && !(st[rel_at].flags & GA_ANCHOR));
    CHECK(anchor_at < rel_at);                      /* 锚点在释放沿之前，否则释放沿会被自己平移 */

    /* 第二发的坑不许再标：重锚只做一次，否则 pw 会被平移两次。 */
    int dips = 0;
    for (int i = 0; i < ns; i++)
        if (st[i].act == GA_VOUT && st[i].arg == glitch_mv_to_code(372)) dips++;
    CHECK(dips == 2);
}

/* Single-pulse default: auto pw/gap = tr-td = 80 -> floored to 90 at 400k. */
static void test_autosize_and_floor(void)
{
    gstep_t st[GLITCH_MAX_STEPS];
    glitch_args_t a;
    CHECK(glitch_args_parse(&a, " 325"));
    CHECK(a.pulses == 1 && a.delay == 75 && a.revert == 1);
    glitch_args_finalize(&a, W400);
    CHECK(a.pw == 90 && a.gap == 90);

    int32_t post = -1;
    int ns = glitch_build(&a, st, GLITCH_MAX_STEPS, &post);
    check_sorted(st, ns);
    CHECK(post == 0);                       /* train_end=-85 <= tv=0: pico semantics */
    CHECK(count_act(st, ns, GA_MODE, -1) == 1);   /* revert=1 */
    for (int i = 0; i < ns; i++)
        if (st[i].act == GA_MODE) CHECK(st[i].t_us == 500000);
}

static void test_pulses_limit_16(void)
{
    gstep_t st[GLITCH_MAX_STEPS];
    int32_t post;
    CHECK(build(" 354 -60 750 -20 0 1 0 100 100 100 117 0", W400, st, &post) == -1);
    int ns = build(" 354 -60 750 -20 0 1 0 16 100 100 117 0", W400, st, &post);
    CHECK(ns <= GLITCH_MAX_STEPS);
    /* 16 pulses -> 32 dip/restore VOUT writes, plus the -1000 prep write. */
    CHECK(count_act(st, ns, GA_VOUT, 57) == 16);
    CHECK(count_act(st, ns, GA_VOUT, 120) == 16);
    check_sorted(st, ns);
}

static void test_hold_and_revert(void)
{
    gstep_t st[GLITCH_MAX_STEPS];
    int32_t post = -1;
    int ns = build(" 354 -60 750 -20 0 1 5 1 120 120 117 1", W400, st, &post);
    check_sorted(st, ns);
    int low_at = -1, rel_at = -1, mode_at = -1;
    for (int i = 0; i < ns; i++) {
        if (st[i].act == GA_SCL_LOW) low_at = st[i].t_us;
        if (st[i].act == GA_SCL_REL) rel_at = st[i].t_us;
        if (st[i].act == GA_MODE) mode_at = st[i].t_us;
    }
    CHECK(low_at == post + 100);
    CHECK(rel_at == post + 100 + 5000);
    CHECK(mode_at == post + 500000);
}

/* 100 kHz: the floor grows to 360 us and pw/gap must follow. */
static void test_floor_100k(void)
{
    gstep_t st[GLITCH_MAX_STEPS];
    glitch_args_t a;
    CHECK(glitch_args_parse(&a, " 354 -60 750 -20 0 1 0 3 198 150 117 0"));
    glitch_args_finalize(&a, glitch_pmbus_w_us(100000));
    CHECK(a.pw == 360 && a.gap == 360);
    int32_t post;
    int ns = glitch_build(&a, st, GLITCH_MAX_STEPS, &post);
    check_sorted(st, ns);
}

static void test_parse_rejects_malformed(void)
{
    const char *bad[] = {
        "325oops", "x 1", "325 -", "2147483648", "-2147483649",
        "999999999999999999999999999999999999999999", "325 0x20",
        "325 -100 650 -20 0 1 0 1 0 0 75 1 0 9",
    };
    glitch_args_t a, before;
    glitch_args_defaults(&before);
    before.vd = 400;
    for (unsigned i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        a = before;
        CHECK(!glitch_args_parse(&a, bad[i]));
        CHECK(memcmp(&a, &before, sizeof a) == 0);
    }
    CHECK(glitch_args_parse(&a, " \t325\t-100 650 -20\n"));
    CHECK(a.vd == 325 && a.delay == 75 && a.swin == 0);
    CHECK(glitch_args_parse(&a, ""));
    CHECK(glitch_args_validate(&a, NULL, 0));
    CHECK(!glitch_args_parse(NULL, ""));
    CHECK(!glitch_args_parse(&a, NULL));
}

static void test_validation_and_arithmetic_bounds(void)
{
    glitch_args_t a;
    char message[80];
    glitch_args_defaults(&a);
    CHECK(glitch_args_validate(&a, message, sizeof message) && !message[0]);
    int *fields[] = { &a.vd, &a.td, &a.vr, &a.tr, &a.tv, &a.fast,
        &a.hold_ms, &a.pulses, &a.pw, &a.gap, &a.delay, &a.revert, &a.swin };
    for (unsigned i = 0; i < sizeof fields / sizeof fields[0]; i++) {
        glitch_args_defaults(&a);
        *fields[i] = INT_MAX;
        CHECK(!glitch_args_validate(&a, message, sizeof message) && message[0]);
        glitch_args_defaults(&a);
        *fields[i] = INT_MIN;
        CHECK(!glitch_args_validate(&a, NULL, 0));
    }
    glitch_args_defaults(&a);
    a.vd = GLITCH_MIN_MV; a.vr = GLITCH_MAX_MV;
    CHECK(glitch_args_validate(&a, NULL, 0));
    a.vd = -1;
    CHECK(!glitch_args_validate(&a, NULL, 0));
    a.vd = 325; a.vr = GLITCH_MAX_MV + 1;
    CHECK(!glitch_args_validate(&a, NULL, 0));
    glitch_args_defaults(&a);
    a.td = -926;  // first = -1001: early dip re-anchors preparation
    CHECK(glitch_args_validate(&a, NULL, 0));
    a.td = -900100;  // first = -900100: beyond the re-anchor budget
    CHECK(!glitch_args_validate(&a, NULL, 0));
    glitch_args_defaults(&a);
    a.td = -3000; a.delay = 0; a.pw = 5000; a.gap = 1000;
    CHECK(glitch_args_validate(&a, NULL, 0));
    {
        gstep_t st[GLITCH_MAX_STEPS];
        int32_t post = -1;
        int ns = glitch_build(&a, st, GLITCH_MAX_STEPS, &post);
        CHECK(ns > 0);
        check_sorted(st, ns);
        CHECK(st[0].t_us == -4100 && st[0].act == GA_CLR);
        CHECK(st[3].t_us == -3000 && st[3].act == GA_RST_LOW);
        CHECK(st[4].t_us == -3000 && st[4].act == GA_VOUT);
    }
    glitch_args_defaults(&a);
    a.td = -926;
    CHECK(glitch_args_validate(&a, NULL, 0));
    a.tr = a.td;
    CHECK(!glitch_args_validate(&a, NULL, 0));
    glitch_args_defaults(&a);
    a.pulses = 16; a.pw = a.gap = 1000000;
    CHECK(!glitch_args_validate(&a, NULL, 0));  // derived duration >15 s
    glitch_args_defaults(&a);
    a.td = INT_MIN; a.tr = INT_MAX;
    glitch_args_finalize(&a, UINT32_MAX);  // must not overflow even before validation
    CHECK(a.pw == INT_MAX && a.gap == INT_MAX);
    CHECK(!glitch_args_validate(&a, NULL, 0));
}

static void test_capacity_is_atomic(void)
{
    glitch_args_t a;
    glitch_args_defaults(&a);
    glitch_args_finalize(&a, W400);
    gstep_t st[GLITCH_MAX_STEPS], untouched[GLITCH_MAX_STEPS];
    memset(untouched, 0xa5, sizeof untouched);
    int ns = glitch_build(&a, st, GLITCH_MAX_STEPS, NULL);
    CHECK(ns > 0);
    for (int capacity = -1; capacity < ns; capacity++) {
        memcpy(st, untouched, sizeof st);
        int32_t post = 12345;
        CHECK(glitch_build(&a, st, capacity, &post) == -1);
        CHECK(memcmp(st, untouched, sizeof st) == 0 && post == 12345);
    }
    CHECK(glitch_build(&a, st, ns, NULL) == ns);
    CHECK(glitch_build(&a, NULL, GLITCH_MAX_STEPS, NULL) == -1);
    CHECK(glitch_build(NULL, st, GLITCH_MAX_STEPS, NULL) == -1);
    glitch_args_defaults(&a);
    CHECK(glitch_build(&a, st, GLITCH_MAX_STEPS, NULL) == -1);  // finalize required
}

static void test_full_sample_window_and_hold_conflicts(void)
{
    gstep_t st[GLITCH_MAX_STEPS];
    int32_t post;
    int ns = build("354 -60 750 -20 0 1 0 16 100 100 117 1 20000",
                   W400, st, &post);
    CHECK(ns > 0 && ns <= GLITCH_MAX_STEPS);
    CHECK(count_act(st, ns, GA_VS, -1) == 45);
    CHECK(count_act(st, ns, GA_VOUT, 57) == 16);
    CHECK(count_act(st, ns, GA_VOUT, 120) == 16);
    check_sorted(st, ns);
    /* Ceiling is GLITCH_MAX_HOLD_MS=1000 since 0.5.5: an SMBus slave aborts a transaction
     * whose clock is frozen for seconds, and the shot still reports writes_failed=0. */
    const int holds[] = { 5, 500, 600, 1000 };
    {
        gstep_t rejected[GLITCH_MAX_STEPS];
        int32_t dummy;
        CHECK(build("354 -60 750 -20 0 1 1001 1 120 120 117 1 20000", W400, rejected, &dummy) < 0);
    }
    for (unsigned h = 0; h < sizeof holds / sizeof holds[0]; h++) {
        char args[120];
        snprintf(args, sizeof args, "354 -60 750 -20 0 1 %d 1 120 120 117 1 20000", holds[h]);
        ns = build(args, W400, st, &post);
        CHECK(ns > 0);
        bool held = false;
        int release = -1, mode = -1;
        for (int i = 0; i < ns; i++) {
            if (st[i].act == GA_SCL_LOW) held = true;
            else if (st[i].act == GA_SCL_REL) { held = false; release = st[i].t_us; }
            else if (held) CHECK(st[i].act != GA_VS && st[i].act != GA_MODE &&
                                 st[i].act != GA_VOUT && st[i].act != GA_VMIN);
            if (st[i].act == GA_MODE) mode = st[i].t_us;
        }
        CHECK(!held && release == post + 100 + holds[h] * 1000);
        CHECK(mode > release);
        if (holds[h] < 500) CHECK(mode == post + 500000);
    }
}

/* VOUT_COMMAND's LSB is 6.25 mV, so a commanded millivolt only exists through its code.
 * This is the arithmetic behind the 2026-11-26 review: the "2 mV ladder" of the 2026-11-21
 * hit replica (372/370/368/366) was three shots at two coordinates, and the 300..380 mV band
 * the campaign searches is 14 coordinates, not 81. */
static void test_code_grid(void)
{
    CHECK(glitch_mv_to_code(366) == 59);
    CHECK(glitch_mv_to_code(368) == 59);
    CHECK(glitch_mv_to_code(370) == 59);
    CHECK(glitch_mv_to_code(372) == 60);
    CHECK(glitch_mv_to_code(378) == 60);
    CHECK(glitch_mv_to_code(380) == 61);
    CHECK(glitch_codes_apart(366, 370) == 0);                 /* the same physical shot */
    CHECK(glitch_codes_apart(372, 366) == 1);
    CHECK(glitch_mv_to_code(380) - glitch_mv_to_code(300) == 13);   /* 14 codes in the band */

    /* code_to_mv lands back on the code it came from, for every code the part can take.
     * Without this the ladder cannot store a position as millivolts at all. */
    for (int code = 0; code <= GLITCH_MAX_MV * GLITCH_MV_PER_CODE_NUM / GLITCH_MV_PER_CODE_DEN; code++)
        CHECK(glitch_mv_to_code(glitch_code_to_mv(code)) == code);

    /* The two must agree with the timeline itself: the code the plan puts on the bus for a
     * commanded mV is the code the ladder names that mV. */
    gstep_t st[GLITCH_MAX_STEPS];
    int32_t post;
    const int ns = build("372 -40 750 -20 0 1 0 2 222 150 64 0", W400, st, &post);
    CHECK(ns > 0);
    int seen = -1;
    for (int i = 0; i < ns; i++)
        if (st[i].act == GA_VOUT && st[i].arg == glitch_mv_to_code(372)) seen = i;
    CHECK(seen >= 0);
}

static void test_no_silent_pulse_retiming(void)
{
    gstep_t st[GLITCH_MAX_STEPS];
    int32_t post;
    int ns = build("", W400, st, &post);
    bool rise = false, release = false;
    for (int i = 0; i < ns; i++) {
        if (st[i].act == GA_VOUT && st[i].arg == 104) {
            CHECK(st[i].t_us == -85); rise = true;
        }
        if (st[i].act == GA_RST_REL) {
            CHECK(st[i].t_us == 0); release = true;
        }
    }
    // The synchronous write overlaps the reset deadline. The executor reports
    // actual edge/slip; planning must not silently move the requested pulses.
    CHECK(rise && release);
}

int main(void)
{
    test_write_cost();
    test_anchor_flag();
    test_acceptance();
    test_autosize_and_floor();
    test_pulses_limit_16();
    test_hold_and_revert();
    test_floor_100k();
    test_parse_rejects_malformed();
    test_validation_and_arithmetic_bounds();
    test_capacity_is_atomic();
    test_full_sample_window_and_hold_conflicts();
    test_no_silent_pulse_retiming();
    test_code_grid();
    printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
