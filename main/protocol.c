#include "protocol.h"
#include <ctype.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static bool one_char(unsigned char c) { return c && strchr("vuzywkKrjJb", c) != NULL; }

bool ga_command_normalize(const char *src, size_t len, char out[GA_COMMAND_MAX])
{
    if (!src || !out || !len || len >= GA_COMMAND_MAX || memchr(src, 0, len)) return false;
    while (len && isspace((unsigned char)*src)) { src++; len--; }
    while (len && isspace((unsigned char)src[len - 1])) len--;
    if (!len || memchr(src, '\n', len) || memchr(src, '\r', len)) return false;
    for (size_t i = 0; i < len; i++)
        if ((unsigned char)src[i] < 32 && src[i] != '\t') return false;
    if (src[0] != '@' && src[0] != 'g' && !(len == 1 && one_char(src[0]))) return false;
    if (src[0] == '@' && len < 2) return false;
    memcpy(out, src, len); out[len] = 0;
    return true;
}

int ga_uart_feed(ga_uart_parser_t *p, unsigned char ch, char out[GA_COMMAND_MAX])
{
    if (p->dropping) {
        if (ch != '\n' && ch != '\r') return 0;
        p->dropping = false; p->used = 0; return -1;
    }
    if (!p->used) {
        if (isspace(ch)) return 0;
        /* Everything is line-dispatched, single-character commands included.  Executing them
         * the moment a byte arrived looked convenient and was not safe: the judge host pushes
         * JSON heartbeats over this same wire, the link has no flow control and absorbs well
         * under the wire rate (PORT_NOTES rule 4), and a heartbeat that does not arrive whole
         * left payload letters at "line start".  On the bench the `r` inside a snapshot's
         * `"ledger"` ran a PEX_RST pulse (both nets were unwired at the time, so it hit
         * nothing) and one 49-byte line produced 48 error lines.  A stray byte is an error
         * now, never an action. */
        p->line[p->used++] = (char)ch; return 0;
    }
    if (ch == '\n' || ch == '\r') {
        bool ok = ga_command_normalize(p->line, p->used, out);
        p->used = 0; return ok ? 1 : -1;
    }
    if (ch == '\b' || ch == 127) { if (p->used > 1) p->used--; return 0; }
    if (p->used >= GA_COMMAND_MAX - 1 || ch == 0 || (ch < 32 && ch != '\t')) {
        p->dropping = true; p->used = 0; return 0;
    }
    p->line[p->used++] = (char)ch;
    return 0;
}

bool ga_parse_int(const char *text, int *out)
{
    if (!text || !*text || !out) return false;
    errno = 0; char *end; long v = strtol(text, &end, 10);
    if (end == text || *end || errno == ERANGE || v < INT_MIN || v > INT_MAX) return false;
    *out = (int)v; return true;
}

bool ga_parse_delays(const char *text, int *out, int *count, int capacity)
{
    if (!text || !*text || !out || !count || capacity <= 0) return false;
    int n = 0; const char *p = text;
    do {
        if (!*p || n == capacity || isspace((unsigned char)*p)) return false;
        errno = 0; char *end; long v = strtol(p, &end, 10);
        if (end == p || errno == ERANGE || v < INT_MIN || v > INT_MAX || (*end && *end != ',')) return false;
        out[n++] = (int)v;
        if (!*end) break;
        p = end + 1;
    } while (true);
    *count = n; return true;
}

bool ga_campaign_options(const char *args, campaign_cfg_t *cfg, char *error, size_t error_size)
{
    if (!args || !cfg || strlen(args) >= GA_COMMAND_MAX) goto bad;
    campaign_cfg_t next = *cfg;
    char buf[GA_COMMAND_MAX]; strcpy(buf, args);
    char *save = NULL; bool first = true;
    for (char *tk = strtok_r(buf, " \t", &save); tk; tk = strtok_r(NULL, " \t", &save)) {
        if (first && !strcmp(tk, "start")) { first = false; continue; }
        first = false;
        char *eq = strchr(tk, '='); if (!eq) goto bad;
        *eq++ = 0;
        if (!strcmp(tk, "delay") || !strcmp(tk, "delays")) {
            if (!ga_parse_delays(eq, next.delays, &next.ndelays, 6)) goto bad;
            continue;
        }
        int v; if (!ga_parse_int(eq, &v)) goto bad;
        if (!strcmp(tk, "vd")) next.vd = v;
        else if (!strcmp(tk, "batch")) next.batch = v;
        else if (!strcmp(tk, "max")) next.max_attempts = v;
        else if (!strcmp(tk, "minv")) next.min_v = v;
        else if (!strcmp(tk, "maxv")) next.max_v = v;
        else if (!strcmp(tk, "step")) next.step = v;
        else if (!strcmp(tk, "pw")) next.pw = v;
        else if (!strcmp(tk, "gap")) next.gap = v;
        else if (!strcmp(tk, "td")) next.td = v;
        else if (!strcmp(tk, "vr")) next.vr = v;
        else if (!strcmp(tk, "tr")) next.tr = v;
        else if (!strcmp(tk, "pulses")) next.pulses = v;
        else if (!strcmp(tk, "mode")) {
            // 0.4.1 removed the on-board SMBPBI oracle. `mode=1` (the host
            // judges) is accepted so an old command line still starts; `mode=0`
            // is refused rather than ignored — a caller that asked the board to
            // judge itself must be told nobody will, and every shot would time
            // out after MANUAL_TIMEOUT_US.
            if (v != 1) goto bad_mode;
        }
        else if (!strcmp(tk, "wait")) next.restore_wait_ms = v;
        else goto bad;
    }
    *cfg = next; if (error && error_size) error[0] = 0; return true;
bad:
    if (error && error_size) snprintf(error, error_size, "invalid campaign option");
    return false;
bad_mode:
    if (error && error_size)
        snprintf(error, error_size, "mode removed: the host judges every shot (mode=1)");
    return false;
}

/* ---------------------------------------------------- 0.5.0 judge-host channel --- */

static bool reject(char *error, size_t size, const char *why)
{
    if (error && size) snprintf(error, size, "%s", why);
    return false;
}

static bool u32_text(const char *text, uint32_t *out)
{
    uint64_t n = 0;
    if (!text || !*text) return false;
    for (const char *p = text; *p; p++) {
        if (*p < '0' || *p > '9') return false;
        n = n * 10u + (unsigned)(*p - '0');
        if (n > UINT32_MAX) return false;
    }
    *out = (uint32_t)n;
    return true;
}

/* `@V <session> <attempt> <word>`: a verdict arriving over the serial line. It accepts
 * exactly the words /api/verdict accepts, because two transports with two vocabularies
 * is how a bench ends up keeping two meanings for `lost`. */
bool ga_verdict_line(const char *args, uint32_t *session, uint32_t *attempt,
                     oracle_t *verdict, char *error, size_t error_size)
{
    char buf[GA_COMMAND_MAX], *save = NULL, *t_session, *t_attempt, *t_verdict;
    uint32_t s, a;
    oracle_t o;

    if (error && error_size) error[0] = 0;
    if (!args || !session || !attempt || !verdict || strlen(args) >= GA_COMMAND_MAX)
        return reject(error, error_size, "usage: @V <session> <attempt> <verdict>");
    strcpy(buf, args);
    t_session = strtok_r(buf, " \t", &save);
    t_attempt = strtok_r(NULL, " \t", &save);
    t_verdict = strtok_r(NULL, " \t", &save);
    if (!t_session || !t_attempt || !t_verdict || strtok_r(NULL, " \t", &save))
        return reject(error, error_size, "usage: @V <session> <attempt> <verdict>");
    if (!u32_text(t_session, &s) || !s || !u32_text(t_attempt, &a) || !a)
        return reject(error, error_size, "session and attempt must be non-zero decimal");
    if (!strcmp(t_verdict, "goldilocks")) o = ORACLE_GOLDILOCKS;
    else if (!strcmp(t_verdict, "hit")) o = ORACLE_HIT;
    else if (!strcmp(t_verdict, "under")) o = ORACLE_UNDER;
    else if (!strcmp(t_verdict, "over")) o = ORACLE_OVER;
    else if (!strcmp(t_verdict, "lost")) o = ORACLE_LOST;
    else return reject(error, error_size, "unknown verdict word");
    *session = s; *attempt = a; *verdict = o;
    return true;
}

/* `@A [seq=N] [adopted=N] {json}`: the judge host's heartbeat. `json` points into
 * `args`, so the caller must copy it before the command buffer is reused; peer_store()
 * is what checks it is a whole object, because a serial line that dropped bytes must
 * be refused here rather than rendered as a healthy-looking bench. */
bool ga_agent_frame(const char *args, uint32_t *seq, uint32_t *adopted,
                    const char **json, char *error, size_t error_size)
{
    char buf[GA_COMMAND_MAX], *save = NULL;
    uint32_t s = 0, a = 0;

    if (error && error_size) error[0] = 0;
    if (!args || !seq || !adopted || !json || strlen(args) >= GA_COMMAND_MAX)
        return reject(error, error_size, "usage: @A [seq=N] [adopted=N] {json}");
    const char *brace = strchr(args, '{');
    if (!brace)
        return reject(error, error_size, "agent snapshot must be a JSON object");
    size_t prefix = (size_t)(brace - args);
    if (prefix >= sizeof buf) return reject(error, error_size, "agent frame too long");
    memcpy(buf, args, prefix);
    buf[prefix] = 0;
    for (char *tk = strtok_r(buf, " \t", &save); tk; tk = strtok_r(NULL, " \t", &save)) {
        char *eq = strchr(tk, '=');
        if (!eq) return reject(error, error_size, "agent frame options must be key=value");
        *eq++ = 0;
        if (!strcmp(tk, "seq")) {
            if (!u32_text(eq, &s)) return reject(error, error_size, "seq must be decimal");
        } else if (!strcmp(tk, "adopted")) {
            if (!u32_text(eq, &a)) return reject(error, error_size, "adopted must be decimal");
        } else return reject(error, error_size, "unknown agent frame option");
    }
    *seq = s; *adopted = a; *json = brace;
    return true;
}
