/* Native tests for the one page of paper between the board and the judge host.
 * cc -std=gnu11 -Wall -Wextra -Werror -Itest/stubs_campaign -Imain \
 *    test/test_peer.c main/peer.c -o test_peer
 * The clock is a variable: every rule worth testing here (a request that expires, a
 * heartbeat that goes stale) is a rule about elapsed time, and none of it should need
 * a real second to pass.
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "peer.h"

static int checks, failures;
#define CHECK(c) do { checks++; if (!(c)) { failures++; printf("FAIL %d: %s\n", __LINE__, #c); } } while (0)

static int64_t fake_now;
int64_t esp_timer_get_time(void) { return fake_now; }

static char out[1024], error[96];

static void reset(void)
{
    fake_now = 0;
    peer_init();
}

static const char *field(const char *json, const char *key)
{
    static char value[PEER_INFO_MAX];
    char pattern[40];
    snprintf(pattern, sizeof pattern, "\"%s\":", key);
    const char *at = strstr(json, pattern);
    if (!at) return NULL;
    at += strlen(pattern);
    const char *end = at;
    if (*at == '"') {
        at++;
        end = strchr(at, '"');
        if (!end) return NULL;
        size_t n = (size_t)(end - at);
        if (n >= sizeof value) return NULL;
        memcpy(value, at, n);
        value[n] = 0;
        return value;
    }
    while (*end && *end != ',' && *end != '}') end++;
    if (end == at) return NULL;
    size_t n = (size_t)(end - at);
    if (n >= sizeof value) return NULL;
    memcpy(value, at, n);
    value[n] = 0;
    return value;
}

static bool render_field(const char *key, const char *expected)
{
    peer_render(out, sizeof out);
    const char *got = field(out, key);
    return got && expected && !strcmp(got, expected);
}

static void test_empty_board_reports_honesty(void)
{
    reset();
    peer_render(out, sizeof out);
    CHECK(render_field("present", "false"));
    CHECK(render_field("req", "none"));
    CHECK(render_field("seq", "0"));
    CHECK(strstr(out, "\"info\":null") != NULL);
    CHECK(!peer_pending_request(out, sizeof out, NULL));
    CHECK(!peer_request_cancel());
}

static void test_snapshot_rules(void)
{
    reset();
    CHECK(peer_store("{\"state\":\"running\"}", 7, error, sizeof error));
    peer_render(out, sizeof out);
    CHECK(render_field("present", "true"));
    CHECK(render_field("seq", "7"));
    CHECK(render_field("age_s", "0"));
    CHECK(strstr(out, "\"info\":{\"state\":\"running\"}") != NULL);

    fake_now = 4500000;                       // 4.5 s later: still the same snapshot
    CHECK(render_field("age_s", "4"));

    // A snapshot is data, not prose: these are the shapes that must not be stored,
    // because each one is what a serial line looks like when it drops characters.
    fake_now = 0;
    CHECK(!peer_store("", 8, error, sizeof error));
    CHECK(error[0]);
    CHECK(!peer_store("{\"state\":\"idle\"", 8, error, sizeof error));       // truncated
    CHECK(!peer_store("state=idle", 8, error, sizeof error));                // not an object
    CHECK(!peer_store("{\"a\":1}{\"b\":2}", 8, error, sizeof error));        // two objects
    CHECK(!peer_store("{\"a\":1}}", 8, error, sizeof error));                // unbalanced
    CHECK(!peer_store("{\"a\":\"line\nbreak\"}", 8, error, sizeof error));   // control byte
    CHECK(!peer_store("{\"a\":\"café\"}", 8, error, sizeof error));          // non-ASCII
    char too_long[PEER_INFO_MAX + 32];
    memset(too_long, 'x', sizeof too_long);
    too_long[0] = '{';
    too_long[sizeof too_long - 2] = '}';
    CHECK(!peer_store(too_long, 8, error, sizeof error));
    CHECK(peer_store("{\"a\":\"}\"}", 8, error, sizeof error));  // brace inside a string
    peer_render(out, sizeof out);
    CHECK(render_field("seq", "8"));       // the refusals above changed nothing stored
    CHECK(render_field("present", "true"));
}

static void test_heartbeat_expires(void)
{
    /* The rule this file's own header promised ("a heartbeat that goes stale") and the
     * implementation never had: `s_present` used to latch on the first `@A`, so a board
     * whose judge had been killed kept answering `present:true` forever. Measured on the
     * bench 2026-11-26: agent stopped, and 6 s later /api/status still said present=true,
     * age_s=6. The page keys its 判决方 line off that field, so the field itself had to
     * stop lying -- and the snapshot must NOT age out with it, because a dead judge's
     * `halt` is the only surviving account of why it stopped. */
    reset();
    CHECK(peer_store("{\"halt\":\"presence\"}", 41, error, sizeof error));
    fake_now = PEER_STALE_US - 1;
    CHECK(render_field("present", "true"));      // one step inside the window
    CHECK(render_field("age_s", "4"));
    CHECK(strstr(out, "\"info\":{\"halt\":\"presence\"}") != NULL);

    fake_now = PEER_STALE_US + 1;
    CHECK(render_field("present", "false"));     // same snapshot, now a stale claim
    CHECK(render_field("age_s", "5"));
    CHECK(render_field("seq", "41"));            // the snapshot itself is kept...
    CHECK(strstr(out, "\"info\":{\"halt\":\"presence\"}") != NULL);   // ...including the reason

    // A fresh heartbeat revives presence: the page must not need a reboot to recover.
    fake_now = PEER_STALE_US + 10000000;
    CHECK(render_field("present", "false"));
    CHECK(peer_store("{\"halt\":\"presence\"}", 42, error, sizeof error));
    CHECK(render_field("present", "true"));
    CHECK(render_field("seq", "42"));
    CHECK(render_field("age_s", "0"));

    // 从没说过话 ≠ 说过之后断了：这两个状态操作员查的方向完全不同（agent 没起 / agent 退了），
    // 分开它们的依据就是闩锁的 seq 是否为 0。
    reset();
    peer_render(out, sizeof out);
    CHECK(render_field("present", "false"));
    CHECK(render_field("seq", "0"));
    CHECK(render_field("age_s", "-1"));
    CHECK(strstr(out, "\"info\":null") != NULL);
}

static void test_request_lifecycle(void)
{
    reset();
    CHECK(!peer_request_post("op=start&vd=340", 0, error, sizeof error));   // seq required
    CHECK(!peer_request_post("vd=340&op=start", 3, error, sizeof error));   // op must lead
    CHECK(!peer_request_post("op=startx&vd=340", 3, error, sizeof error));
    CHECK(!peer_request_post("op=start&note=a b", 3, error, sizeof error)); // space
    CHECK(!peer_request_post("op=start&ssid=caf%C3%A9", 3, error, sizeof error));
    CHECK(!peer_request_post("op=start#x", 3, error, sizeof error));
    CHECK(render_field("req", "none"));

    CHECK(peer_request_post("op=start&vd=340&delays=25,20&seq=3", 3, error, sizeof error));
    CHECK(error[0] == 0);
    CHECK(render_field("req", "pending"));
    CHECK(render_field("req_seq", "3"));
    CHECK(render_field("req_age_s", "0"));
    uint32_t seq = 0;
    CHECK(peer_pending_request(out, sizeof out, &seq) && seq == 3);
    CHECK(!strcmp(out, "op=start&vd=340&delays=25,20&seq=3"));   // verbatim, unread

    fake_now = PEER_REQ_TTL_US + 1;
    CHECK(render_field("req", "expired"));
    CHECK(render_field("req_seq", "3"));
    CHECK(!peer_pending_request(out, sizeof out, &seq));

    // A newest click replaces an older one: the last thing a human pressed is what they
    // meant, and quietly keeping both would make the run depend on arrival order.
    fake_now = 0;
    CHECK(peer_request_post("op=start&vd=346&seq=4", 4, error, sizeof error));
    CHECK(peer_request_post("op=start&vd=352&seq=5", 5, error, sizeof error));
    CHECK(peer_pending_request(out, sizeof out, &seq) && seq == 5);
    peer_adopted(4);                                    // a stale claim changes nothing
    CHECK(render_field("req", "pending"));
    peer_adopted(5);
    CHECK(render_field("req", "adopted"));
    CHECK(render_field("req_seq", "5"));
    CHECK(!peer_pending_request(out, sizeof out, &seq));

    CHECK(!peer_request_cancel());     // an adopted request already left the slot
    fake_now = 0;
    CHECK(peer_request_post("op=start&vd=340&seq=6", 6, error, sizeof error));
    CHECK(peer_request_cancel());
    CHECK(!peer_request_cancel());                      // and only once
    CHECK(render_field("req", "none"));
}

static void test_adoption_outranks_expiry(void)
{
    reset();
    CHECK(peer_request_post("op=start&vd=340&seq=9", 9, error, sizeof error));
    // The agent claimed it at 9.999 s; the board reaped it while the reply was in
    // flight. Saying "expired" there would hide a campaign that really is running.
    fake_now = PEER_REQ_TTL_US + 1;
    CHECK(render_field("req", "expired"));
    peer_adopted(9);
    CHECK(render_field("req", "adopted"));
    CHECK(render_field("req_seq", "9"));
}

int main(void)
{
    test_empty_board_reports_honesty();
    test_snapshot_rules();
    test_heartbeat_expires();
    test_request_lifecycle();
    test_adoption_outranks_expiry();
    printf("peer: %d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
