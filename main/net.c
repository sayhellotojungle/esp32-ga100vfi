#include "net.h"
#include "console.h"
#include "campaign.h"
#include "oracle.h"
#include "peer.h"
#include "webui.h"
#include "board.h"
#include "board_runtime.h"

#include <ctype.h>
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi_default.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "esp_http_server.h"
#include "esp_ota_ops.h"
#include "esp_system.h"

static void net_start_server(void);

#define NVS_NS          "ga100vfi"
#define NVS_SSID        "ssid"
#define NVS_PASS        "pass"
#define QUERY_MAX       768

static portMUX_TYPE s_net_mux = portMUX_INITIALIZER_UNLOCKED;
static bool s_got_ip, s_connect_enabled, s_wifi_ready;
static char s_ip[16] = "-";
static int s_down_ctr;

static bool net_snapshot(char *ip)
{
    portENTER_CRITICAL(&s_net_mux);
    bool up = s_got_ip;
    if (ip) memcpy(ip, s_ip, sizeof s_ip);
    portEXIT_CRITICAL(&s_net_mux);
    return up;
}

static void ev_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        char ip[16] = { 0 };
        snprintf(ip, sizeof ip, IPSTR, IP2STR(&e->ip_info.ip));
        portENTER_CRITICAL(&s_net_mux);
        memcpy(s_ip, ip, sizeof s_ip);
        s_got_ip = true;
        s_down_ctr = 0;
        portEXIT_CRITICAL(&s_net_mux);
        console_printf("wifi: connected, http://%s/\n", ip);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        portENTER_CRITICAL(&s_net_mux);
        s_got_ip = false;
        strcpy(s_ip, "-");
        bool retry = s_connect_enabled;
        portEXIT_CRITICAL(&s_net_mux);
        if (retry) esp_wifi_connect();
    }
}

static bool load_creds(char *ssid, size_t sn, char *pass, size_t pn)
{
    ssid[0] = pass[0] = 0;
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return false;
    size_t n = sn;
    esp_err_t se = nvs_get_str(h, NVS_SSID, ssid, &n);
    n = pn;
    esp_err_t pe = nvs_get_str(h, NVS_PASS, pass, &n);
    nvs_close(h);
    if (se != ESP_OK || (pe != ESP_OK && pe != ESP_ERR_NVS_NOT_FOUND)) {
        ssid[0] = pass[0] = 0;
        return false;
    }
    return ssid[0] != 0;
}

static bool save_creds(const char *ssid, const char *pass)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return false;
    esp_err_t err = nvs_set_str(h, NVS_SSID, ssid);
    if (err == ESP_OK) err = nvs_set_str(h, NVS_PASS, pass);
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    return err == ESP_OK;
}

static void sta_config(wifi_config_t *wc, const char *ssid, const char *pass)
{
    memset(wc, 0, sizeof *wc);
    // ESP-IDF accepts full-width 32-byte SSIDs / 64-byte hexadecimal PSKs.
    // strlcpy would silently drop the last byte of either valid maximum.
    memcpy(wc->sta.ssid, ssid, strlen(ssid));
    memcpy(wc->sta.password, pass, strlen(pass));
    wc->sta.threshold.authmode = pass[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
}

static bool creds_valid(const char *ssid, const char *pass)
{
    size_t sn = strlen(ssid), pn = strlen(pass);
    if (sn > 32 || pn > 64) return false;
    if (!sn) return !pn;
    if (!pn || (pn >= 8 && pn <= 63)) return true;
    if (pn != 64) return false;
    for (size_t i = 0; i < pn; i++)
        if (!isxdigit((unsigned char)pass[i])) return false;
    return true;
}

// Caller owns the board's maintenance lock (HTTP) or command lock (UART).
static bool set_creds(const char *ssid, const char *pass)
{
    if (!s_wifi_ready || !creds_valid(ssid, pass)) return false;
    if (!save_creds(ssid, pass)) return false;
    portENTER_CRITICAL(&s_net_mux);
    s_connect_enabled = false;
    s_got_ip = false;
    s_down_ctr = 0;
    strcpy(s_ip, "-");
    portEXIT_CRITICAL(&s_net_mux);
    esp_wifi_disconnect();
    wifi_config_t wc;
    sta_config(&wc, ssid, pass);
    if (esp_wifi_set_config(WIFI_IF_STA, &wc) != ESP_OK) return false;
    portENTER_CRITICAL(&s_net_mux);
    s_connect_enabled = ssid[0] != 0;
    portEXIT_CRITICAL(&s_net_mux);
    if (ssid[0]) esp_wifi_connect();
    console_printf("wifi: %s%s\n", ssid[0] ? "saved, connecting to " : "cleared", ssid);
    return true;
}

static void mon_task(void *arg)
{
    (void)arg;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        portENTER_CRITICAL(&s_net_mux);
        bool restart = false;
        if (s_got_ip || !s_connect_enabled) s_down_ctr = 0;
        else if (++s_down_ctr >= 12) { s_down_ctr = 0; restart = true; }
        portEXIT_CRITICAL(&s_net_mux);
        if (restart) {
            console_printf("wifi: 60 s link-lost, restarting radio\n");
            esp_wifi_stop();
            esp_wifi_start();
            esp_wifi_connect();
        }
    }
}

void net_init(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        err = nvs_flash_erase();
        if (err == ESP_OK) err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        console_printf("ERR network NVS init: %s\n", esp_err_to_name(err));
        return;
    }
    ESP_ERROR_CHECK(esp_netif_init());
    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) ESP_ERROR_CHECK(err);
    esp_netif_create_default_wifi_sta();
    esp_netif_create_default_wifi_ap();

    wifi_config_t wc;
    char ssid[33], pass[65];
    bool have = load_creds(ssid, sizeof ssid, pass, sizeof pass);
    if (!creds_valid(ssid, pass)) { ssid[0] = pass[0] = 0; have = false; }
    sta_config(&wc, ssid, pass);
    wifi_config_t ac = { 0 };
    strlcpy((char *)ac.ap.ssid, GA_AP_SSID, sizeof ac.ap.ssid);
    strlcpy((char *)ac.ap.password, GA_AP_PASS, sizeof ac.ap.password);
    ac.ap.ssid_len = strlen(GA_AP_SSID);
    ac.ap.channel = 6;
    ac.ap.max_connection = 4;
    ac.ap.authmode = WIFI_AUTH_WPA2_PSK;

    wifi_init_config_t wi = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wi));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ac));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, ev_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, ev_handler, NULL, NULL));
    s_wifi_ready = true;
    s_connect_enabled = have;
    ESP_ERROR_CHECK(esp_wifi_start());
    if (have) esp_wifi_connect();
    if (xTaskCreate(mon_task, "ga_net", 3072, NULL, 2, NULL) != pdPASS)
        console_printf("ERR wifi monitor task creation\n");
    net_start_server();
}

bool net_sta_up(void) { return net_snapshot(NULL); }

void net_wifi_cmd(const char *args)
{
    if (!args || !args[0]) {
        char ssid[33], pass[65], ip[16];
        load_creds(ssid, sizeof ssid, pass, sizeof pass);
        bool up = net_snapshot(ip);
        console_printf("wifi: ssid='%s' %s ip=%s ap=%s\n", ssid,
                       ssid[0] ? (up ? "up" : "down") : "unconfigured", ip, GA_AP_SSID);
        return;
    }
    if (strlen(args) >= 128) { console_printf("ERR wifi arguments too long\n"); return; }
    char buf[128];
    strcpy(buf, args);
    // Legacy UART syntax remains '@W ssid password'; the web API passes
    // separate decoded fields and supports spaces/non-ASCII in the SSID.
    char *pass = strchr(buf, ' ');
    if (pass) *pass++ = 0;
    else pass = (char *)"";
    if (!set_creds(buf, pass)) console_printf("ERR wifi settings invalid or save failed\n");
}

// ------------------------------------------------------------- HTTP ------

static esp_err_t reply(httpd_req_t *r, const char *status, const char *message)
{
    httpd_resp_set_status(r, status);
    httpd_resp_set_type(r, "text/plain; charset=utf-8");
    httpd_resp_set_hdr(r, "Cache-Control", "no-store");
    return httpd_resp_sendstr(r, message);
}

static esp_err_t close_error(httpd_req_t *r, const char *status, const char *message)
{
    httpd_resp_set_hdr(r, "Connection", "close");
    reply(r, status, message);
    return ESP_FAIL;  // Never reuse a connection with an unread request body.
}

static bool read_query(httpd_req_t *r, char *qs, size_t n)
{
    size_t len = httpd_req_get_url_query_len(r);
    if (len == 0) { qs[0] = 0; return true; }
    return len < n && httpd_req_get_url_query_str(r, qs, n) == ESP_OK;
}

static int hex_digit(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

// 1=present, 0=absent, -1=malformed/too long. Decode before testing the
// destination size: encoded UTF-8 or '%' must not consume the SSID budget.
static int qget(const char *qs, const char *key, char *out, size_t size)
{
    char encoded[QUERY_MAX];
    out[0] = 0;
    esp_err_t err = httpd_query_key_value(qs, key, encoded, sizeof encoded);
    if (err == ESP_ERR_NOT_FOUND) return 0;
    if (err != ESP_OK) return -1;
    size_t used = 0;
    for (size_t i = 0; encoded[i]; i++) {
        unsigned char ch = (unsigned char)encoded[i];
        if (ch == '%') {
            if (!encoded[i + 1] || !encoded[i + 2]) return -1;
            int hi = hex_digit(encoded[i + 1]), lo = hex_digit(encoded[i + 2]);
            if (hi < 0 || lo < 0) return -1;
            ch = (unsigned char)((hi << 4) | lo);
            i += 2;
        } else if (ch == '+') ch = ' ';
        if (!ch || used + 1 >= size) return -1;
        out[used++] = (char)ch;
    }
    out[used] = 0;
    return 1;
}

static bool parse_int(const char *s, int *value)
{
    if (!s[0] || isspace((unsigned char)s[0])) return false;
    char *end;
    errno = 0;
    long x = strtol(s, &end, 10);
    if (errno || *end || x < INT_MIN || x > INT_MAX) return false;
    *value = (int)x;
    return true;
}

static bool qint(const char *qs, const char *key, int *value)
{
    char b[24];
    int found = qget(qs, key, b, sizeof b);
    return found == 0 || (found == 1 && parse_int(b, value));
}

static bool parse_u32(const char *s, uint32_t *value)
{
    if (!s[0]) return false;
    uint64_t n = 0;
    for (; *s; s++) {
        if (*s < '0' || *s > '9') return false;
        n = n * 10 + (unsigned)(*s - '0');
        if (n > UINT32_MAX) return false;
    }
    *value = (uint32_t)n;
    return true;
}

static bool parse_delays(char *s, campaign_cfg_t *cfg)
{
    int count = 0;
    for (char *p = s; p;) {
        if (count == 6) return false;
        char *next = strchr(p, ',');
        if (next) *next++ = 0;
        if (!parse_int(p, &cfg->delays[count++])) return false;
        p = next;
    }
    cfg->ndelays = count;
    return count > 0;
}

static esp_err_t uri_root(httpd_req_t *r)
{
    httpd_resp_set_type(r, "text/html; charset=utf-8");
    httpd_resp_set_hdr(r, "Cache-Control", "no-store");
    return httpd_resp_send(r, WEBUI_HTML, sizeof WEBUI_HTML - 1);
}

/* The page's start request (see peer.h). The board stores it for the judge host and
 * deliberately does not parse the campaign parameters inside: the agent owns those, and
 * a board that half-parsed them would be a second place to get one wrong. What it does
 * check is that the request can be handed over intact, that it can still be honoured
 * (a run already going means there is nothing to start), and how long it stays valid.
 */
static esp_err_t uri_req(httpd_req_t *r)
{
    char qs[QUERY_MAX], v[24], error[96];
    uint32_t seq;
    if (!read_query(r, qs, sizeof qs) || qget(qs, "op", v, sizeof v) != 1)
        return reply(r, "400 Bad Request", "op=start or op=cancel required");
    if (!strcmp(v, "cancel")) {
        bool had = peer_request_cancel();
        return reply(r, had ? "200 OK" : "409 Conflict",
                     had ? "request withdrawn" : "no request pending");
    }
    if (strcmp(v, "start")) return reply(r, "400 Bad Request", "unknown request operation");
    if (qget(qs, "seq", v, sizeof v) != 1 || !parse_u32(v, &seq) || !seq)
        return reply(r, "400 Bad Request", "seq must be a non-zero decimal sequence");
    if (campaign_is_running() || board_is_busy())
        return reply(r, "409 Conflict", "a campaign is already running");
    if (!peer_request_post(qs, seq, error, sizeof error))
        return reply(r, "400 Bad Request", error);
    return reply(r, "200 OK",
                 "request stored for the judge host; valid 10 s unless claimed");
}

static char s_status_buf[3072];
static esp_err_t uri_status(httpd_req_t *r)
{
    char board[768], camp[768], peer[512], ip[16];
    board_status_json(board, sizeof board);
    campaign_status_json(camp, sizeof camp);
    peer_render(peer, sizeof peer);
    bool up = net_snapshot(ip);
    wifi_ap_record_t ap;
    int rssi = esp_wifi_sta_get_ap_info(&ap) == ESP_OK ? ap.rssi : 0;
    int n = snprintf(s_status_buf, sizeof s_status_buf,
             "{\"net\":{\"sta\":%s,\"ip\":\"%s\",\"rssi\":%d,\"ap\":\"%s\","
             "\"build\":\"" GA_BUILD "\"},"
             "\"board\":%s,\"campaign\":%s,\"peer\":%s}",
             up ? "true" : "false", ip, rssi, GA_AP_SSID, board, camp, peer);
    if (n < 0 || n >= (int)sizeof s_status_buf)
        return reply(r, "500 Internal Server Error", "status exceeds response buffer");
    httpd_resp_set_type(r, "application/json");
    httpd_resp_set_hdr(r, "Cache-Control", "no-store");
    return httpd_resp_send(r, s_status_buf, n);
}

static char logbuf[2048];
static esp_err_t uri_log(httpd_req_t *r)
{
    char qs[64], v[16];
    uint32_t pos = 0;
    if (!read_query(r, qs, sizeof qs)) return reply(r, "400 Bad Request", "bad query");
    int found = qget(qs, "pos", v, sizeof v);
    if (found < 0 || (found == 1 && !parse_u32(v, &pos)))
        return reply(r, "400 Bad Request", "invalid log position");
    int n = console_log_read(&pos, logbuf, sizeof logbuf);
    char hdr[16];
    snprintf(hdr, sizeof hdr, "%" PRIu32, pos);
    httpd_resp_set_hdr(r, "X-Log-Pos", hdr);
    httpd_resp_set_hdr(r, "Cache-Control", "no-store");
    httpd_resp_set_type(r, "text/plain; charset=utf-8");
    return httpd_resp_send(r, logbuf, n);
}

static esp_err_t uri_cmd(httpd_req_t *r)
{
    char b[GA_COMMAND_MAX];
    if (r->content_len == 0)
        return reply(r, "400 Bad Request", "empty command");
    if (r->content_len >= sizeof b)
        return close_error(r, "413 Payload Too Large", "one command of 1..255 bytes required");
    size_t used = 0;
    while (used < r->content_len) {
        int n = httpd_req_recv(r, b + used, r->content_len - used);
        if (n <= 0) return close_error(r, "400 Bad Request", "incomplete command");
        used += (size_t)n;
    }
    if (memchr(b, 0, used)) return reply(r, "400 Bad Request", "NUL in command");
    b[used] = 0;
    if (!console_input_push(b, (int)used))
        return reply(r, "409 Conflict", "command queue full or invalid command framing");
    return reply(r, "200 OK", "queued; check console for result");
}

static esp_err_t uri_campaign(httpd_req_t *r)
{
    char qs[QUERY_MAX], v[96];
    if (!read_query(r, qs, sizeof qs) || qget(qs, "op", v, sizeof v) != 1)
        return reply(r, "400 Bad Request", "op=start or op=stop required");
    if (!strcmp(v, "stop")) {
        campaign_stop();
        return reply(r, "200 OK", "stopping");
    }
    if (strcmp(v, "start")) return reply(r, "400 Bad Request", "unknown campaign operation");
    // 0.4.1 deleted the on-board SMBPBI oracle, so there is no verdict mode to
    // choose. `mode=1` is still accepted (callers and pages built before 0.4.1
    // send it); `mode=0` is refused instead of being silently ignored, because a
    // caller that asked for the board to judge itself must learn that nobody
    // will answer it and every shot will time out. Checked before the busy test:
    // the answer must depend on the request, not on what the board is doing.
    if (qget(qs, "mode", v, sizeof v) == 1 && strcmp(v, "1"))
        return reply(r, "400 Bad Request",
                     "mode removed: the board has no on-board oracle, "
                     "verdicts arrive via /api/verdict");
    if (campaign_is_running() || board_is_busy()) return reply(r, "409 Conflict", "board busy");
    campaign_cfg_t cfg = campaign_defaults;
    if (!qint(qs, "vd", &cfg.vd) || !qint(qs, "td", &cfg.td) ||
        !qint(qs, "vr", &cfg.vr) || !qint(qs, "tr", &cfg.tr) ||
        !qint(qs, "batch", &cfg.batch) || !qint(qs, "max", &cfg.max_attempts) ||
        !qint(qs, "minv", &cfg.min_v) || !qint(qs, "maxv", &cfg.max_v) ||
        !qint(qs, "step", &cfg.step) || !qint(qs, "pw", &cfg.pw) ||
        !qint(qs, "gap", &cfg.gap) || !qint(qs, "pulses", &cfg.pulses) ||
        !qint(qs, "wait", &cfg.restore_wait_ms))
        return reply(r, "400 Bad Request", "invalid numeric campaign parameter");
    int found = qget(qs, "delays", v, sizeof v);
    if (found < 0 || (found == 1 && !parse_delays(v, &cfg)))
        return reply(r, "400 Bad Request", "delays must contain 1..6 integers");
    char validation[160];
    if (!campaign_cfg_validate(&cfg, validation, sizeof validation))
        return reply(r, "400 Bad Request", validation);
    // Reserve the same owner as OTA and serial commands across the state
    // transition, rather than relying on a check made before parsing.
    if (!board_begin_maintenance()) return reply(r, "409 Conflict", "board busy");
    bool started = campaign_start(&cfg);
    board_end_maintenance();
    if (!started) return reply(r, "409 Conflict", "campaign start rejected: busy");
    return reply(r, "200 OK", "started");
}

static esp_err_t uri_verdict(httpd_req_t *r)
{
    char qs[160], v[20];
    uint32_t session, attempt;
    if (!read_query(r, qs, sizeof qs) ||
        qget(qs, "session", v, sizeof v) != 1 || !parse_u32(v, &session) || !session ||
        qget(qs, "attempt", v, sizeof v) != 1 || !parse_u32(v, &attempt) || !attempt ||
        qget(qs, "r", v, sizeof v) != 1)
        return reply(r, "400 Bad Request", "session, attempt and r required");
    oracle_t o;
    if (!strcmp(v, "goldilocks")) o = ORACLE_GOLDILOCKS;
    else if (!strcmp(v, "hit")) o = ORACLE_HIT;
    else if (!strcmp(v, "under")) o = ORACLE_UNDER;
    else if (!strcmp(v, "over")) o = ORACLE_OVER;
    else if (!strcmp(v, "lost")) o = ORACLE_LOST;
    else return reply(r, "400 Bad Request", "invalid verdict");
    if (!campaign_submit_verdict(session, attempt, o))
        return reply(r, "409 Conflict", "stale verdict or campaign not awaiting this attempt");
    return reply(r, "200 OK", "ok");
}

static esp_err_t uri_wifi(httpd_req_t *r)
{
    char qs[QUERY_MAX], ssid[33], pass[65];
    if (!read_query(r, qs, sizeof qs) || qget(qs, "ssid", ssid, sizeof ssid) != 1)
        return reply(r, "400 Bad Request", "valid SSID (0..32 bytes) required");
    int found = qget(qs, "pass", pass, sizeof pass);
    if (found < 0 || !creds_valid(ssid, pass))
        return reply(r, "400 Bad Request", "invalid password (empty, 8..63 bytes, or 64 hex digits)");
    if (!board_begin_maintenance()) return reply(r, "409 Conflict", "stop campaign and wait for board idle");
    bool ok = set_creds(ssid, pass);
    board_end_maintenance();
    return reply(r, ok ? "200 OK" : "500 Internal Server Error",
                 ok ? (ssid[0] ? "saved, connecting" : "cleared") : "Wi-Fi configuration/save failed");
}

static esp_err_t uri_ota(httpd_req_t *r)
{
    if (!board_begin_maintenance())
        return close_error(r, "409 Conflict", "stop campaign and wait for board idle before OTA");
    const char *failure = NULL;
    const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
    if (!part || r->content_len < 65536 || r->content_len > part->size) {
        board_end_maintenance();
        return close_error(r, "400 Bad Request", "no spare OTA partition or invalid image size");
    }
    esp_ota_handle_t oh;
    if (esp_ota_begin(part, r->content_len, &oh) != ESP_OK) {
        board_end_maintenance();
        return close_error(r, "500 Internal Server Error", "ota_begin failed");
    }
    console_printf("ota: %" PRIu64 " bytes -> %s\n", (uint64_t)r->content_len, part->label);
    static char buf[4096];
    size_t left = r->content_len;
    while (left > 0) {
        int n = httpd_req_recv(r, buf, left < sizeof buf ? left : sizeof buf);
        if (n <= 0) { failure = "incomplete OTA upload"; break; }
        if (esp_ota_write(oh, buf, (size_t)n) != ESP_OK) {
            failure = "flash write failed";
            break;
        }
        left -= (size_t)n;
    }
    if (failure) {
        esp_ota_abort(oh);
        board_end_maintenance();
        return close_error(r, "500 Internal Server Error", failure);
    }
    if (esp_ota_end(oh) != ESP_OK) {
        board_end_maintenance();
        return reply(r, "400 Bad Request", "invalid firmware image");
    }
    if (esp_ota_set_boot_partition(part) != ESP_OK) {
        board_end_maintenance();
        return reply(r, "500 Internal Server Error", "cannot select new boot partition");
    }
    console_printf("ota: staged %s, rebooting...\n", part->label);
    reply(r, "200 OK", "flashed, rebooting");
    vTaskDelay(pdMS_TO_TICKS(500));
    esp_restart();  // Keep the exclusive reservation until restart.
    return ESP_OK;
}

static httpd_handle_t s_httpd;
static void net_start_server(void)
{
    httpd_config_t c = HTTPD_DEFAULT_CONFIG();
    c.task_priority = 5;
    c.stack_size = 8192;
    c.core_id = 0;
    c.lru_purge_enable = true;
    // The default handler table is 8 entries, and 0.5.0 added a ninth route: the
    // registration failed, ESP_ERROR_CHECK aborted, and the board reboot-looped with the
    // HTTP server never up -- the one failure mode a bench that hangs off a card must not
    // have, because the only symptom at the rack is "the box stopped answering". 16 gives
    // room for routes without counting them again at every release.
    c.max_uri_handlers = 16;
    if (httpd_start(&s_httpd, &c) != ESP_OK) {
        console_printf("ERR http start failed\n");
        return;
    }
    const httpd_uri_t uris[] = {
        { .uri = "/",             .method = HTTP_GET,  .handler = uri_root },
        { .uri = "/api/status",   .method = HTTP_GET,  .handler = uri_status },
        { .uri = "/api/log",      .method = HTTP_GET,  .handler = uri_log },
        { .uri = "/api/cmd",      .method = HTTP_POST, .handler = uri_cmd },
        { .uri = "/api/campaign", .method = HTTP_GET,  .handler = uri_campaign },
        { .uri = "/api/req",      .method = HTTP_GET,  .handler = uri_req },
        { .uri = "/api/verdict",  .method = HTTP_GET,  .handler = uri_verdict },
        { .uri = "/api/wifi",     .method = HTTP_GET,  .handler = uri_wifi },
        { .uri = "/api/ota",      .method = HTTP_POST, .handler = uri_ota },
    };
    // A route that cannot register is named and survivable, not fatal: the page will show
    // that one endpoint as an error, and the console says which one at boot. What must not
    // happen is the whole board going quiet because of one missing table slot.
    for (size_t i = 0; i < sizeof uris / sizeof uris[0]; i++)
        if (httpd_register_uri_handler(s_httpd, &uris[i]) != ESP_OK)
            console_printf("ERR http route %s not registered (handler table full?)\n",
                           uris[i].uri);
}
