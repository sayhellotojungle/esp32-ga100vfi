/* Native regression tests of the real campaign FSM and the verdict token rules.
 * Hardware, clock and FreeRTOS mutexes are replaced with deterministic fakes.
 * Since 0.4.1 the board has no judge of its own: every shot is settled and then
 * waits for the host, so these tests feed verdicts in through
 * campaign_submit_verdict() and check what the ladder does with them.
 * Optional: -DCAMPAIGN_TEST_THREADS -pthread also tests concurrent starts.
 */
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef CAMPAIGN_TEST_THREADS
#include <pthread.h>
#endif

#include "campaign.h"
#include "freertos/semphr.h"

static int checks, failures;
#define CHECK(c) do { checks++; if (!(c)) { failures++; \
    printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); } } while (0)

struct mock_mutex { atomic_flag held; };
SemaphoreHandle_t xSemaphoreCreateMutex(void)
{
    struct mock_mutex *m = malloc(sizeof *m);
    if (!m) abort();
    atomic_flag_clear(&m->held);
    return m;
}
int xSemaphoreTake(SemaphoreHandle_t m, TickType_t timeout)
{
    (void)timeout;
    while (atomic_flag_test_and_set_explicit(&m->held, memory_order_acquire)) {}
    return 1;
}
int xSemaphoreGive(SemaphoreHandle_t m)
{
    atomic_flag_clear_explicit(&m->held, memory_order_release);
    return 1;
}

static int64_t fake_time;
static atomic_uint random_counter = 100;
int64_t esp_timer_get_time(void) { return fake_time; }
uint32_t esp_random(void) { return atomic_fetch_add(&random_counter, 1) + 1; }
int console_printf(const char *fmt, ...)
{
    char out[1024];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(out, sizeof out, fmt, ap);
    va_end(ap);
    return n;
}

static int executions, restorations;
static bool execute_ok, restore_ok, stop_in_execute;
static int execute_wfail;
static glitch_args_t executed[512];
static bool execute(const glitch_args_t *args, int *wfail, uint32_t *sequence)
{
    CHECK(executions < (int)(sizeof executed / sizeof executed[0]));
    if (executions < (int)(sizeof executed / sizeof executed[0]))
        executed[executions] = *args;
    executions++;
    CHECK(args->revert == 0 && args->hold_ms == 0);
    CHECK(campaign_is_running()); // Callback must run outside the status lock.
    *wfail = execute_wfail;
    *sequence = (uint32_t)executions;
    if (stop_in_execute) campaign_stop();
    return execute_ok;
}
static bool restore(void)
{
    restorations++;
    CHECK(campaign_is_running()); // New starts remain excluded during restore.
    return restore_ok;
}

static void reset_fixture(void)
{
    campaign_stop();
    for (int i = 0; i < 3 && campaign_is_running(); i++) campaign_poll();
    CHECK(!campaign_is_running());
    fake_time = 0;
    executions = restorations = 0;
    execute_ok = restore_ok = true;
    stop_in_execute = false; execute_wfail = 0;
}
static char status[768];
static const char *field(const char *key)
{
    char pattern[80];
    campaign_status_json(status, sizeof status);
    snprintf(pattern, sizeof pattern, "\"%s\":", key);
    const char *p = strstr(status, pattern);
    CHECK(p != NULL);
    return p ? p + strlen(pattern) : "0";
}
static uint32_t number(const char *key) { return (uint32_t)strtoul(field(key), NULL, 10); }
static bool flag(const char *key) { return !strncmp(field(key), "true", 4); }
static bool phase(const char *name)
{
    const char *p = field("phase");
    return p[0] == '"' && !strncmp(p + 1, name, strlen(name)) &&
           p[strlen(name) + 1] == '"';
}
static campaign_cfg_t manual_config(int max)
{
    campaign_cfg_t c = campaign_defaults;
    c.max_attempts = max;
    return c;
}
static void ready_verdict(void)
{
    campaign_poll();
    fake_time += 300000;
    campaign_poll();
    CHECK(flag("awaiting_verdict"));
}
static void feed(oracle_t o)
{
    uint32_t session = number("session"), attempt = number("attempt");
    CHECK(campaign_submit_verdict(session, attempt, o));
    campaign_poll();
}
static void run_manual(oracle_t o, int max_polls)
{
    for (int i = 0; i < max_polls && campaign_is_running(); i++) {
        campaign_poll();
        if (flag("awaiting_verdict")) feed(o);
        fake_time += 500000;
    }
    CHECK(!campaign_is_running());
}

static void test_single_cap_and_goldilocks(void)
{
    reset_fixture();
    campaign_cfg_t c = manual_config(1);
    CHECK(campaign_start(&c));
    CHECK(executions == 0); // Starting from HTTP cannot touch hardware.
    ready_verdict();
    feed(ORACLE_UNDER);
    CHECK(!campaign_is_running() && executions == 1 && restorations == 1);
    CHECK(number("tries") == 1 && number("under") == 1 && phase("limit"));
    for (int i = 0; i < 5; i++) campaign_poll();
    CHECK(executions == 1);

    reset_fixture();
    CHECK(campaign_start(&c));
    ready_verdict();
    feed(ORACLE_GOLDILOCKS);
    CHECK(flag("goldilocks") && phase("done"));
    CHECK(!campaign_is_running() && executions == 1 && restorations == 0);
    campaign_stop(); campaign_poll();
    CHECK(restorations == 0);
}

static void test_tokens_and_duplicates(void)
{
    reset_fixture();
    campaign_cfg_t c = manual_config(3);
    CHECK(campaign_start(&c));
    uint32_t session = number("session");
    CHECK(session != 0);
    CHECK(!campaign_submit_verdict(session, 1, ORACLE_UNDER));
    campaign_poll(); // Execute, but still settling.
    CHECK(!flag("awaiting_verdict"));
    CHECK(!campaign_submit_verdict(session, 1, ORACLE_UNDER));
    fake_time += 300000; campaign_poll();
    CHECK(flag("awaiting_verdict"));
    CHECK(!campaign_submit_verdict(session + 1, 1, ORACLE_UNDER));
    CHECK(!campaign_submit_verdict(session, 2, ORACLE_UNDER));
    CHECK(!campaign_submit_verdict(session, 1, (oracle_t)99));
    CHECK(campaign_submit_verdict(session, 1, ORACLE_UNDER));
    CHECK(campaign_submit_verdict(session, 1, ORACLE_UNDER));
    CHECK(!campaign_submit_verdict(session, 1, ORACLE_OVER));
    campaign_poll();
    CHECK(executions == 1); // A verdict does not execute the next attempt inline.
    CHECK(number("under") == 1);
    ready_verdict();
    CHECK(number("attempt") == 2);
    CHECK(campaign_submit_verdict(session, 1, ORACLE_UNDER)); // Retry ACK only.
    CHECK(flag("awaiting_verdict") && number("under") == 1);
    feed(ORACLE_OVER);
    CHECK(!campaign_submit_verdict(session, 1, ORACLE_UNDER));
    campaign_stop(); campaign_poll();
    CHECK(!campaign_is_running());
    CHECK(campaign_start(&c));
    CHECK(number("session") != session);
    CHECK(!campaign_submit_verdict(session, 2, ORACLE_OVER));
    campaign_stop(); campaign_poll();
}

static void test_exact_grid_budgets(void)
{
    const int limits[] = { 1, 5, 200 };
    for (unsigned k = 0; k < sizeof limits / sizeof limits[0]; k++) {
        reset_fixture();
        campaign_cfg_t c = manual_config(limits[k]);
        c.ndelays = 6; c.batch = 0;
        for (int i = 0; i < 6; i++) c.delays[i] = 10 * i;
        CHECK(campaign_start(&c));
        run_manual(ORACLE_UNDER, limits[k] * 4 + 10);
        CHECK(executions == limits[k] && restorations == 1);
        int counts[6] = { 0 };
        for (int i = 0; i < executions; i++) counts[executed[i].delay / 10]++;
        for (int i = 0; i < 6; i++)
            CHECK(counts[i] == limits[k] / 6 + (i < limits[k] % 6));
    }
}

static void test_stop_paths(void)
{
    reset_fixture();
    campaign_cfg_t c = manual_config(0);
    CHECK(campaign_start(&c));
    campaign_stop(); campaign_poll();
    CHECK(!campaign_is_running() && executions == 0 && restorations == 0);

    reset_fixture();
    CHECK(campaign_start(&c));
    stop_in_execute = true;
    campaign_poll(); campaign_poll();
    CHECK(!campaign_is_running() && executions == 1 && restorations == 1);

    reset_fixture();
    CHECK(campaign_start(&c));
    ready_verdict();
    uint32_t session = number("session"), attempt = number("attempt");
    campaign_stop();
    CHECK(!campaign_submit_verdict(session, attempt, ORACLE_GOLDILOCKS));
    campaign_poll();
    CHECK(!campaign_is_running() && executions == 1 && restorations == 1);

    reset_fixture();
    CHECK(campaign_start(&c));
    ready_verdict();
    session = number("session"); attempt = number("attempt");
    CHECK(campaign_submit_verdict(session, attempt, ORACLE_GOLDILOCKS));
    campaign_stop(); campaign_poll(); // Already accepted success must survive.
    CHECK(flag("goldilocks") && restorations == 0);
}

static void test_failed_writes_timeout_and_restore(void)
{
    reset_fixture();
    campaign_cfg_t c = manual_config(2);
    CHECK(campaign_start(&c));
    execute_wfail = 1;
    campaign_poll();
    CHECK(number("nack") == 1 && number("lost") == 0);
    CHECK(!flag("awaiting_verdict"));
    CHECK(!campaign_submit_verdict(number("session"), number("attempt"), ORACLE_UNDER));
    fake_time = 399999; campaign_poll(); CHECK(executions == 1);
    fake_time++; campaign_poll(); CHECK(executions == 2);
    campaign_poll();
    CHECK(!campaign_is_running() && restorations == 1 && number("nack") == 2);

    reset_fixture();
    c.max_attempts = 1;
    CHECK(campaign_start(&c));
    ready_verdict();
    // The settle interval is over and the board is waiting: 0.4.1 removed the
    // on-board oracle, so there is nobody here who could call this shot anything
    // yet. `valid` must stay 0 until a verdict arrives from the host.
    CHECK(phase("waiting") && number("valid") == 0);
    fake_time += 120000000; campaign_poll();
    CHECK(number("timeouts") == 1 && number("lost") == 0 && number("nack") == 0);
    CHECK(number("valid") == 0);       // timing out is not a verdict
    CHECK(!campaign_is_running() && restorations == 1);

    /* 0.5.0: one unanswered shot ends the campaign even when the budget is not spent.
     * Silence has one meaning now -- the side that reads BAR0 is gone -- and the old
     * behaviour kept drawing coordinates on that assumption: with the usual max=60 that
     * is up to two hours of shots at a card nobody is looking at, and a coordinate spent
     * is a coordinate spent. */
    reset_fixture();
    c.max_attempts = 5;
    CHECK(campaign_start(&c));
    ready_verdict();
    CHECK(executions == 1 && phase("waiting"));
    fake_time += 120000000; campaign_poll();
    CHECK(number("timeouts") == 1 && phase("no-judge"));
    CHECK(!campaign_is_running() && restorations == 1 && executions == 1);
    fake_time += 10000000;
    campaign_poll(); campaign_poll();
    CHECK(executions == 1 && !campaign_is_running());   // no second shot into the void
    c.max_attempts = 1;

    reset_fixture();
    CHECK(campaign_start(&c));
    execute_ok = false; restore_ok = false;
    campaign_poll(); campaign_poll();
    CHECK(!campaign_is_running() && phase("restore_failed") && !flag("restore_ok"));
}

static void test_lost_adaptation_and_unlimited_grid(void)
{
    reset_fixture();
    campaign_cfg_t c = manual_config(2);
    c.batch = 1;
    CHECK(campaign_start(&c));
    ready_verdict(); feed(ORACLE_LOST);
    CHECK(number("lost") == 1 && number("nack") == 0 && number("valid") == 0);
    CHECK(number("v_dip") == (uint32_t)(c.vd + c.step));
    campaign_stop(); campaign_poll();

    reset_fixture();
    c = manual_config(0); c.ndelays = 2; c.delays[0] = 60; c.delays[1] = 90;
    c.batch = 1;
    CHECK(campaign_start(&c));
    for (int i = 0; i < 41; i++) {
        ready_verdict(); feed(ORACLE_UNDER);
    }
    CHECK(executions == 41);
    CHECK(executed[0].delay == 60 && executed[19].delay == 60);
    CHECK(executed[20].delay == 90 && executed[39].delay == 90);
    CHECK(executed[40].delay == 60 && executed[40].vd == c.min_v);
    campaign_stop(); campaign_poll();
}

static void test_config_validation_and_busy(void)
{
    reset_fixture();
    campaign_cfg_t c = campaign_defaults, bad;
    char error[128];
    CHECK(campaign_cfg_validate(&c, error, sizeof error));
    CHECK(error[0] == 0);
    CHECK(!campaign_cfg_validate(NULL, error, sizeof error));
    CHECK(error[0] != 0);
    bad = c; bad.ndelays = 7; CHECK(!campaign_start(&bad));
    bad = c; bad.max_attempts = -1; CHECK(!campaign_start(&bad));
    bad = c; bad.min_v = c.vd + 1; CHECK(!campaign_start(&bad));
    bad = c; bad.max_v = c.vd - 1; CHECK(!campaign_start(&bad));
    bad = c; bad.step = 0; CHECK(!campaign_start(&bad));
    bad = c; bad.delays[0] = 1000000; CHECK(!campaign_start(&bad));
    bad = c; bad.pulses = 17; CHECK(!campaign_start(&bad));
    CHECK(!campaign_is_running() && executions == 0);
    CHECK(campaign_start(&c));
    bad = c;
    CHECK(!campaign_start(&bad));                 // busy: a running campaign is immutable
    /* 0.4.1: the status block reports no verdict mode; "waiting for the host" is
     * the only state there is, and `awaiting_verdict` says it. */
    campaign_status_json(status, sizeof status);
    CHECK(strstr(status, "\"mode\"") == NULL);
    campaign_stop(); campaign_poll();
}

#ifdef CAMPAIGN_TEST_THREADS
static atomic_bool start_threads;
struct start_arg { campaign_cfg_t cfg; bool result; };
static void *start_worker(void *arg)
{
    struct start_arg *a = arg;
    while (!atomic_load_explicit(&start_threads, memory_order_acquire)) {}
    a->result = campaign_start(&a->cfg);
    return NULL;
}
static void test_concurrent_starts(void)
{
    reset_fixture();
    struct start_arg a[2] = {
        { .cfg = campaign_defaults, .result = false },
        { .cfg = campaign_defaults, .result = false },
    };
    pthread_t threads[2];
    atomic_store(&start_threads, false);
    CHECK(pthread_create(&threads[0], NULL, start_worker, &a[0]) == 0);
    CHECK(pthread_create(&threads[1], NULL, start_worker, &a[1]) == 0);
    atomic_store_explicit(&start_threads, true, memory_order_release);
    pthread_join(threads[0], NULL); pthread_join(threads[1], NULL);
    CHECK(a[0].result != a[1].result);            // exactly one start wins the mutex
    CHECK(executions == 0);
    campaign_stop(); campaign_poll();
}
#endif

int main(void)
{
    campaign_init(execute, restore);
    test_single_cap_and_goldilocks();
    test_tokens_and_duplicates();
    test_exact_grid_budgets();
    test_stop_paths();
    test_failed_writes_timeout_and_restore();
    test_lost_adaptation_and_unlimited_grid();
    test_config_validation_and_busy();
#ifdef CAMPAIGN_TEST_THREADS
    test_concurrent_starts();
#endif
    printf("campaign: %d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
