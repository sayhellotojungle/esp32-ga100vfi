/* See peer.h for what this module is for. Everything here is RAM-resident and
 * bounded: two fixed buffers, one spinlock, and a clock read only to age them.
 *
 * Two rules are worth stating where the code enforces them:
 *   - The agent's snapshot must be one balanced ASCII JSON object. A half-line from a
 *     serial adapter that dropped characters would otherwise render as a page that
 *     reports the bench as healthy while showing nothing.
 *   - A start request expires. The agent is started by a human when needed, so a click
 *     made while nothing is listening must not fire a shot whenever one next appears:
 *     the human would no longer be watching, and the whole point of the agent is that
 *     somebody is.
 */
#include "peer.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_timer.h"
#include "freertos/FreeRTOS.h"

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

/* agent -> page */
static char s_info[PEER_INFO_MAX];
static uint32_t s_info_seq;
static int64_t s_info_t;
static bool s_present;

/* page -> agent */
static char s_req[PEER_REQ_MAX];
static uint32_t s_req_seq;
static int64_t s_req_t;
static uint32_t s_adopted;   /* last request the agent said it started */
static uint32_t s_expired;   /* last request that aged out unclaimed   */

static int64_t peer_now(void) { return esp_timer_get_time(); }

void peer_init(void)
{
    s_info[0] = 0; s_info_seq = 0; s_info_t = 0; s_present = false;
    s_req[0] = 0; s_req_seq = 0; s_req_t = 0;
    s_adopted = 0; s_expired = 0;
}

static void refuse(char *error, int n, const char *why)
{
    if (error && n > 0) snprintf(error, (size_t)n, "%s", why);
}

/* Printable ASCII only: the snapshot is embedded in /api/status verbatim, so a stray
 * control byte would truncate the response for every reader, and non-ASCII prose does
 * not belong in a machine field anyway. Braces are counted outside string literals so
 * `{` inside a reason code cannot make a malformed object look whole. */
static bool ascii_json_object(const char *s, char *error, int n)
{
    if (!s || !s[0]) return refuse(error, n, "agent snapshot is empty"), false;
    size_t len = strlen(s);
    if (len >= PEER_INFO_MAX)
        return refuse(error, n, "agent snapshot exceeds 255 bytes"), false;
    if (s[0] != '{' || s[len - 1] != '}')
        return refuse(error, n, "agent snapshot must be one JSON object"), false;
    int depth = 0;
    bool in_string = false, escaped = false;
    for (size_t i = 0; i < len; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c < 0x20 || c > 0x7e)
            return refuse(error, n, "agent snapshot must be printable ASCII"), false;
        if (in_string) {
            if (escaped) escaped = false;
            else if (c == '\\') escaped = true;
            else if (c == '"') in_string = false;
            continue;
        }
        if (c == '"') in_string = true;
        else if (c == '{') depth++;
        else if (c == '}') {
            if (--depth < 0)
                return refuse(error, n, "agent snapshot has unbalanced braces"), false;
            if (depth == 0 && i != len - 1)
                return refuse(error, n, "agent snapshot must be a single JSON object"), false;
        }
    }
    if (depth || in_string)
        return refuse(error, n, "agent snapshot is truncated"), false;
    return true;
}

bool peer_store(const char *json, uint32_t seq, char *error, int n)
{
    refuse(error, n, "");
    if (!ascii_json_object(json, error, n)) return false;
    portENTER_CRITICAL(&s_mux);
    snprintf(s_info, sizeof s_info, "%s", json);
    s_info_seq = seq;
    s_info_t = peer_now();
    s_present = true;
    portEXIT_CRITICAL(&s_mux);
    return true;
}

/* Request bodies are campaign parameters the board hands over unread, so the charset
 * is the one the page can produce without percent-encoding anything: `op=start` first,
 * then `key=value` pairs. Anything else is refused rather than passed along, because an
 * agent would otherwise have to guess whether a garbled run was meant to be shallow. */
static bool request_body_ok(const char *body, char *error, int n)
{
    if (!body || !body[0]) return refuse(error, n, "request body is empty"), false;
    if (strlen(body) >= PEER_REQ_MAX)
        return refuse(error, n, "request exceeds 191 bytes"), false;
    size_t op = strlen("op=start");
    if (strncmp(body, "op=start", op))
        return refuse(error, n, "request must start with op=start"), false;
    if (body[op] && body[op] != '&')
        return refuse(error, n, "op=start must be a whole parameter"), false;
    for (const char *p = body; *p; p++) {
        char c = *p;
        bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '_' || c == '=' ||
                  c == '&' || c == ',' || c == '.' || c == '-';
        if (!ok) return refuse(error, n, "request contains unsupported characters"), false;
    }
    return true;
}

/* Drops a request nobody claimed. Called with the lock held, from every entry point
 * that can observe the slot, so no timer is needed to notice a dead agent. */
static void reap_locked(int64_t now)
{
    if (s_req[0] && now - s_req_t > PEER_REQ_TTL_US) {
        s_expired = s_req_seq;
        s_req[0] = 0; s_req_seq = 0;
    }
}

bool peer_request_post(const char *body, uint32_t seq, char *error, int n)
{
    refuse(error, n, "");
    if (!seq) return refuse(error, n, "request sequence must be non-zero"), false;
    if (!request_body_ok(body, error, n)) return false;
    portENTER_CRITICAL(&s_mux);
    reap_locked(peer_now());
    snprintf(s_req, sizeof s_req, "%s", body);
    s_req_seq = seq;
    s_req_t = peer_now();
    portEXIT_CRITICAL(&s_mux);
    return true;
}

bool peer_request_cancel(void)
{
    bool cancelled;
    portENTER_CRITICAL(&s_mux);
    cancelled = s_req[0] != 0;
    s_req[0] = 0; s_req_seq = 0;
    /* Withdrawing clears the last resolution too: after a human says "not that one",
     * a page still reporting "request #5 adopted" would be describing a run they have
     * just disowned. */
    s_adopted = 0; s_expired = 0;
    portEXIT_CRITICAL(&s_mux);
    return cancelled;
}

bool peer_pending_request(char *out, int n, uint32_t *seq)
{
    bool pending;
    portENTER_CRITICAL(&s_mux);
    reap_locked(peer_now());
    pending = s_req[0] != 0;
    if (pending) {
        if (out && n > 0) snprintf(out, (size_t)n, "%s", s_req);
        if (seq) *seq = s_req_seq;
    }
    portEXIT_CRITICAL(&s_mux);
    return pending;
}

void peer_adopted(uint32_t seq)
{
    if (!seq) return;
    portENTER_CRITICAL(&s_mux);
    /* An adoption may arrive a hair after the reap (the agent claimed it at 9.999 s and
     * the board aged it out while the reply was in flight). Honour it anyway: the agent
     * did start that run, and telling the page "expired" would hide a real campaign. */
    if (s_req_seq == seq || s_expired == seq) {
        s_req[0] = 0; s_req_seq = 0; s_expired = 0;
        s_adopted = seq;
    }
    portEXIT_CRITICAL(&s_mux);
}

void peer_render(char *out, int n)
{
    if (!out || n <= 0) return;
    char info[PEER_INFO_MAX];
    bool present, pending;
    uint32_t seq, req_seq, resolved;
    int64_t age, req_age;
    const char *state;

    portENTER_CRITICAL(&s_mux);
    reap_locked(peer_now());
    present = s_present;
    snprintf(info, sizeof info, "%s", s_info);
    seq = s_info_seq;
    age = present ? (peer_now() - s_info_t) / 1000000 : -1;
    pending = s_req[0] != 0;
    req_seq = pending ? s_req_seq : (s_adopted ? s_adopted : s_expired);
    req_age = pending ? (peer_now() - s_req_t) / 1000000 : 0;
    resolved = pending ? 0 : (s_adopted && !s_expired ? s_adopted : s_expired);
    state = pending ? "pending" : (resolved ? (resolved == s_adopted ? "adopted" : "expired")
                                           : "none");
    portEXIT_CRITICAL(&s_mux);

    snprintf(out, (size_t)n,
             "{\"present\":%s,\"age_s\":%lld,\"seq\":%" PRIu32 ",\"req\":\"%s\","
             "\"req_seq\":%" PRIu32 ",\"req_age_s\":%lld,\"info\":%s}",
             present ? "true" : "false", (long long)age, seq, state, req_seq,
             (long long)req_age, present ? info : "null");
}
