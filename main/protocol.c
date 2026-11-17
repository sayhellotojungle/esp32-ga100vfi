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
        if (ch == '@' || ch == 'g') { p->line[p->used++] = (char)ch; return 0; }
        if (one_char(ch)) { out[0] = (char)ch; out[1] = 0; return 1; }
        return -1;
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
        else if (!strcmp(tk, "mode")) next.oracle_mode = v;
        else if (!strcmp(tk, "wait")) next.restore_wait_ms = v;
        else goto bad;
    }
    *cfg = next; if (error && error_size) error[0] = 0; return true;
bad:
    if (error && error_size) snprintf(error, error_size, "invalid campaign option");
    return false;
}
