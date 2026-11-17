/* Compile the actual network handlers against SDK-shaped offline doubles.
 * cc -std=c11 -Wall -Wextra -Werror -Itest/stubs_net -Imain test/test_net.c -o test_net
 * No radio, socket, flash, task or hardware operation is performed.
 */
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "stubs_net/mock_sdk.h"
#include "../main/net.c"

static bool t_busy, t_held, t_reserve_fail, t_running, t_start_ok, t_config_ok;
static bool t_queue_ok, t_verdict_ok, t_nvs_ok, t_part_exists;
static int t_begin, t_end, t_start, t_queued, t_verdict, t_registered;
static int t_nvs_init, t_nvs_erase, t_nvs_first, t_ota_fail;
static int t_ota_abort, t_ota_end, t_boot_select, t_restart;
static size_t t_written;
static uint32_t t_session, t_attempt;
static oracle_t t_result;
static char t_command[GA_COMMAND_MAX], t_ssid[33], t_password[65];
static wifi_config_t t_wifi;
static campaign_cfg_t t_campaign;
static const esp_partition_t t_partition = { .size = 1u << 20, .label = "ota_1" };
static char t_image[65536];

static void reset_fakes(void)
{
    t_busy = t_held = t_reserve_fail = t_running = false;
    t_start_ok = t_config_ok = t_queue_ok = t_verdict_ok = t_nvs_ok = t_part_exists = true;
    t_begin = t_end = t_start = t_queued = t_verdict = t_registered = 0;
    t_nvs_init = t_nvs_erase = t_nvs_first = t_ota_fail = 0;
    t_ota_abort = t_ota_end = t_boot_select = t_restart = 0;
    t_written = 0;
    t_session = t_attempt = 0;
    t_command[0] = t_ssid[0] = t_password[0] = 0;
    memset(&t_wifi, 0, sizeof t_wifi);
    s_wifi_ready = true;
    s_got_ip = s_connect_enabled = false;
    s_down_ctr = 0;
    strcpy(s_ip, "-");
}

static httpd_req_t request(const char *query, const char *body, size_t len)
{
    httpd_req_t r = { .query = query, .body = body, .content_len = len,
                      .chunk_size = 3, .fail_after = SIZE_MAX };
    strcpy(r.response_status, "200 OK");
    return r;
}

static void status_is(const httpd_req_t *r, const char *code)
{
    assert(strncmp(r->response_status, code, 3) == 0);
}

/* Platform and service doubles. */
const char *esp_err_to_name(esp_err_t error) { (void)error; return "mock error"; }
size_t strlcpy(char *out, const char *src, size_t n)
{
    size_t len = strlen(src);
    if (n) { size_t copied = len < n - 1 ? len : n - 1; memcpy(out, src, copied); out[copied] = 0; }
    return len;
}
int xTaskCreate(void (*entry)(void *), const char *name, uint32_t stack,
                void *arg, unsigned priority, void *handle)
{ (void)entry; (void)name; (void)stack; (void)arg; (void)priority; (void)handle; return pdPASS; }
void vTaskDelay(unsigned ticks) { (void)ticks; }
int esp_event_loop_create_default(void) { return ESP_OK; }
int esp_event_handler_instance_register(esp_event_base_t base, int id,
    void (*handler)(void *, esp_event_base_t, int32_t, void *), void *arg, void *instance)
{ (void)base; (void)id; (void)handler; (void)arg; (void)instance; return ESP_OK; }
int esp_netif_init(void) { return ESP_OK; }
void *esp_netif_create_default_wifi_sta(void) { return (void *)1; }
void *esp_netif_create_default_wifi_ap(void) { return (void *)1; }
int esp_wifi_init(const wifi_init_config_t *c) { (void)c; return ESP_OK; }
int esp_wifi_set_storage(int storage) { (void)storage; return ESP_OK; }
int esp_wifi_set_mode(int mode) { (void)mode; return ESP_OK; }
int esp_wifi_set_config(int interface, const wifi_config_t *config)
{ if (interface == WIFI_IF_STA) t_wifi = *config; return ESP_OK; }
int esp_wifi_start(void) { return ESP_OK; }
int esp_wifi_stop(void) { return ESP_OK; }
int esp_wifi_connect(void) { return ESP_OK; }
int esp_wifi_disconnect(void) { return ESP_OK; }
int esp_wifi_sta_get_ap_info(wifi_ap_record_t *info) { info->rssi = -50; return ESP_OK; }
int nvs_open(const char *name, int mode, nvs_handle_t *handle)
{ (void)name; (void)mode; *handle = 1; return ESP_OK; }
int nvs_get_str(nvs_handle_t handle, const char *key, char *value, size_t *size)
{
    (void)handle;
    const char *source = !strcmp(key, "ssid") ? t_ssid : t_password;
    size_t needed = strlen(source) + 1;
    if (*size < needed) return ESP_FAIL;
    memcpy(value, source, needed); *size = needed; return ESP_OK;
}
int nvs_set_str(nvs_handle_t handle, const char *key, const char *value)
{
    (void)handle;
    if (!t_nvs_ok) return ESP_FAIL;
    if (!strcmp(key, "ssid")) strlcpy(t_ssid, value, sizeof t_ssid);
    else strlcpy(t_password, value, sizeof t_password);
    return ESP_OK;
}
int nvs_commit(nvs_handle_t handle) { (void)handle; return t_nvs_ok ? ESP_OK : ESP_FAIL; }
void nvs_close(nvs_handle_t handle) { (void)handle; }
int nvs_flash_init(void) { return ++t_nvs_init == 1 ? t_nvs_first : ESP_OK; }
int nvs_flash_erase(void) { t_nvs_erase++; return ESP_OK; }

size_t httpd_req_get_url_query_len(httpd_req_t *r) { return r->query ? strlen(r->query) : 0; }
int httpd_req_get_url_query_str(httpd_req_t *r, char *out, size_t size)
{
    if (!r->query) return ESP_ERR_NOT_FOUND;
    if (strlcpy(out, r->query, size) >= size) return ESP_ERR_HTTPD_RESULT_TRUNC;
    return ESP_OK;
}
int httpd_query_key_value(const char *query, const char *key, char *out, size_t size)
{
    size_t wanted = strlen(key);
    for (const char *p = query; p && *p;) {
        const char *amp = strchr(p, '&');
        const char *end = amp ? amp : p + strlen(p);
        const char *eq = memchr(p, '=', (size_t)(end - p));
        if (eq && (size_t)(eq - p) == wanted && !memcmp(p, key, wanted)) {
            size_t n = (size_t)(end - eq - 1);
            if (n >= size) return ESP_ERR_HTTPD_RESULT_TRUNC;
            memcpy(out, eq + 1, n); out[n] = 0; return ESP_OK;
        }
        p = amp ? amp + 1 : NULL;
    }
    return ESP_ERR_NOT_FOUND;
}
int httpd_req_recv(httpd_req_t *r, char *out, size_t size)
{
    r->recv_calls++;
    if (r->read_pos >= r->fail_after) return ESP_FAIL;
    size_t n = r->content_len - r->read_pos;
    if (n > size) n = size;
    if (r->chunk_size && n > r->chunk_size) n = r->chunk_size;
    if (n > r->fail_after - r->read_pos) n = r->fail_after - r->read_pos;
    memcpy(out, r->body + r->read_pos, n); r->read_pos += n;
    return (int)n;
}
int httpd_resp_set_status(httpd_req_t *r, const char *s)
{ strlcpy(r->response_status, s, sizeof r->response_status); return ESP_OK; }
int httpd_resp_set_type(httpd_req_t *r, const char *s) { (void)r; (void)s; return ESP_OK; }
int httpd_resp_set_hdr(httpd_req_t *r, const char *key, const char *value)
{ if (!strcmp(key, "Connection") && !strcmp(value, "close")) r->connection_close = true; return ESP_OK; }
int httpd_resp_send(httpd_req_t *r, const char *body, ssize_t length)
{
    size_t n = length < 0 ? strlen(body) : (size_t)length;
    if (n >= sizeof r->response_body) n = sizeof r->response_body - 1;
    memcpy(r->response_body, body, n); r->response_body[n] = 0; return ESP_OK;
}
int httpd_resp_sendstr(httpd_req_t *r, const char *body) { return httpd_resp_send(r, body, -1); }
int httpd_start(httpd_handle_t *handle, const httpd_config_t *config)
{ (void)config; *handle = (void *)1; return ESP_OK; }
int httpd_register_uri_handler(httpd_handle_t handle, const httpd_uri_t *uri)
{ (void)handle; assert(uri->handler); t_registered++; return ESP_OK; }

bool board_is_busy(void) { return t_busy || t_held; }
bool board_begin_maintenance(void)
{
    t_begin++;
    if (t_busy || t_held || t_running || t_reserve_fail) return false;
    t_held = true; return true;
}
void board_end_maintenance(void) { assert(t_held); t_held = false; t_end++; }
void board_status_json(char *out, int n) { snprintf(out, (size_t)n, "{}"); }
int console_printf(const char *format, ...) { (void)format; return 0; }
bool console_input_push(const char *buf, int len)
{
    t_queued++; assert(len > 0 && len < GA_COMMAND_MAX);
    memcpy(t_command, buf, (size_t)len); t_command[len] = 0;
    return t_queue_ok;
}
int console_log_read(uint32_t *pos, char *out, int max)
{ (void)pos; (void)out; (void)max; return 0; }
const campaign_cfg_t campaign_defaults = {
    .vd = 354, .td = -60, .vr = 750, .tr = -20, .pulses = 3,
    .pw = 198, .gap = 150, .delays = {117}, .ndelays = 1,
    .batch = 5, .min_v = 300, .max_v = 380, .step = 6,
    .max_attempts = 200, .restore_wait_ms = 300, .oracle_mode = 1
};
bool campaign_is_running(void) { return t_running; }
bool campaign_cfg_validate(const campaign_cfg_t *cfg, char *error, size_t n)
{
    (void)cfg;
    if (!t_config_ok) snprintf(error, n, "invalid campaign config");
    return t_config_ok;
}
bool campaign_start(const campaign_cfg_t *cfg)
{
    assert(t_held); t_start++; t_campaign = *cfg;
    if (t_start_ok) t_running = true;
    return t_start_ok;
}
void campaign_stop(void) { t_running = false; }
void campaign_status_json(char *out, int n) { snprintf(out, (size_t)n, "{}"); }
bool campaign_submit_verdict(uint32_t session, uint32_t attempt, oracle_t result)
{ t_verdict++; t_session = session; t_attempt = attempt; t_result = result; return t_verdict_ok; }

const esp_partition_t *esp_ota_get_next_update_partition(const esp_partition_t *current)
{ (void)current; return t_part_exists ? &t_partition : NULL; }
int esp_ota_begin(const esp_partition_t *partition, size_t size, esp_ota_handle_t *handle)
{ assert(t_held); assert(partition && size == sizeof t_image); *handle = 1; return t_ota_fail == 1 ? ESP_FAIL : ESP_OK; }
int esp_ota_write(esp_ota_handle_t handle, const void *data, size_t size)
{ (void)handle; (void)data; assert(t_held); t_written += size; return t_ota_fail == 2 ? ESP_FAIL : ESP_OK; }
int esp_ota_abort(esp_ota_handle_t handle) { (void)handle; assert(t_held); t_ota_abort++; return ESP_OK; }
int esp_ota_end(esp_ota_handle_t handle)
{ (void)handle; assert(t_held); t_ota_end++; return t_ota_fail == 3 ? ESP_FAIL : ESP_OK; }
int esp_ota_set_boot_partition(const esp_partition_t *partition)
{ (void)partition; assert(t_held); t_boot_select++; return t_ota_fail == 4 ? ESP_FAIL : ESP_OK; }
void esp_restart(void) { assert(t_held); t_restart++; }

/* Behavioral cases exercise the included production functions. */
static void test_query_decodes_and_checks_decoded_size(void)
{
    char value[33];
    assert(qget("ssid=Lab+WiFi%2B%E4%B8%AD", "ssid", value, sizeof value) == 1);
    assert(!strcmp(value, "Lab WiFi+中"));
    char exact[3];
    assert(qget("v=%41%42", "v", exact, sizeof exact) == 1 && !strcmp(exact, "AB"));
    assert(qget("v=%41%42%43", "v", exact, sizeof exact) == -1);
    assert(qget("v=%", "v", exact, sizeof exact) == -1);
    assert(qget("v=%GG", "v", exact, sizeof exact) == -1);
    assert(qget("v=%00", "v", exact, sizeof exact) == -1);
}
static void test_command_waits_for_complete_body(void)
{
    const char *body = "g 354 -60 750 -20";
    httpd_req_t r = request(NULL, body, strlen(body));
    assert(uri_cmd(&r) == ESP_OK); status_is(&r, "200");
    assert(r.recv_calls > 1 && t_queued == 1 && !strcmp(t_command, body));
}
static void test_partial_command_never_queued(void)
{
    httpd_req_t r = request(NULL, "g 354 -60", 9); r.fail_after = 3;
    assert(uri_cmd(&r) == ESP_FAIL); status_is(&r, "400");
    assert(t_queued == 0 && r.connection_close);
}
static void test_oversize_and_nul_commands_rejected(void)
{
    httpd_req_t r = request(NULL, "x", GA_COMMAND_MAX);
    assert(uri_cmd(&r) == ESP_FAIL); status_is(&r, "413");
    assert(r.recv_calls == 0 && t_queued == 0);
    const char bytes[] = {'u', 0, 'x'};
    r = request(NULL, bytes, sizeof bytes);
    uri_cmd(&r); status_is(&r, "400"); assert(t_queued == 0);
}
static void test_queue_full_is_reported(void)
{
    t_queue_ok = false;
    httpd_req_t r = request(NULL, "v", 1);
    uri_cmd(&r); status_is(&r, "409"); assert(t_queued == 1);
}
static void test_wifi_spaces_and_literal_plus_survive(void)
{
    httpd_req_t r = request("ssid=Lab%20WiFi&pass=abc%2B1234", NULL, 0);
    uri_wifi(&r); status_is(&r, "200");
    assert(!strcmp(t_ssid, "Lab WiFi") && !strcmp(t_password, "abc+1234"));
    assert(!memcmp(t_wifi.sta.ssid, "Lab WiFi", 8));
    assert(t_begin == 1 && t_end == 1 && !t_held);
}
static void test_wifi_full_width_credentials_are_not_truncated(void)
{
    char query[160], psk[65]; memset(psk, 'a', 64); psk[64] = 0;
    const char *ssid = "12345678901234567890123456789012";
    snprintf(query, sizeof query, "ssid=%s&pass=%s", ssid, psk);
    httpd_req_t r = request(query, NULL, 0);
    uri_wifi(&r); status_is(&r, "200");
    assert(!memcmp(t_wifi.sta.ssid, ssid, 32));
    assert(!memcmp(t_wifi.sta.password, psk, 64));
}
static void test_wifi_rejects_invalid_password_and_releases_on_save_error(void)
{
    httpd_req_t r = request("ssid=Lab&pass=short", NULL, 0);
    uri_wifi(&r); status_is(&r, "400"); assert(t_begin == 0);
    t_nvs_ok = false;
    r = request("ssid=Lab&pass=12345678", NULL, 0);
    uri_wifi(&r); status_is(&r, "500");
    assert(t_begin == 1 && t_end == 1 && !t_held);
}
static void test_nvs_reinitializes_after_erasing(void)
{
    t_nvs_first = ESP_ERR_NVS_NO_FREE_PAGES;
    net_init();
    assert(t_nvs_init == 2 && t_nvs_erase == 1 && t_registered == 8);
}
static void test_campaign_start_reserved_and_parameters_decoded(void)
{
    httpd_req_t r = request("op=start&mode=1&delays=117%2C125&wait=500", NULL, 0);
    uri_campaign(&r); status_is(&r, "200");
    assert(t_start == 1 && t_begin == 1 && t_end == 1 && !t_held);
    assert(t_campaign.oracle_mode == 1 && t_campaign.restore_wait_ms == 500);
    assert(t_campaign.ndelays == 2 && t_campaign.delays[0] == 117 && t_campaign.delays[1] == 125);
}
static void test_campaign_failed_start_releases_reservation(void)
{
    t_start_ok = false;
    httpd_req_t r = request("op=start", NULL, 0);
    uri_campaign(&r); status_is(&r, "409");
    assert(t_start == 1 && t_begin == 1 && t_end == 1 && !t_held);
}
static void test_campaign_racing_maintenance_prevents_start(void)
{
    // Initial busy check passes; another owner wins before reservation.
    t_reserve_fail = true;
    httpd_req_t r = request("op=start", NULL, 0);
    uri_campaign(&r); status_is(&r, "409");
    assert(t_start == 0 && t_begin == 1 && t_end == 0);
}
static void test_bad_campaign_parameter_cannot_default_into_start(void)
{
    httpd_req_t r = request("op=start&batch=garbage", NULL, 0);
    uri_campaign(&r); status_is(&r, "400"); assert(t_start == 0 && t_begin == 0);
    r = request("op=start&delays=1%2C", NULL, 0);
    uri_campaign(&r); status_is(&r, "400"); assert(t_start == 0);
}
static void test_verdict_ids_and_names_are_strict(void)
{
    httpd_req_t r = request("session=12&attempt=3&r=hit", NULL, 0);
    uri_verdict(&r); status_is(&r, "200");
    assert(t_session == 12 && t_attempt == 3 && t_result == ORACLE_HIT);
    t_verdict_ok = false;
    uri_verdict(&r); status_is(&r, "409");
    r = request("session=4294967296&attempt=3&r=hit", NULL, 0);
    uri_verdict(&r); status_is(&r, "400");
    r = request("session=12&attempt=3&r=typo", NULL, 0);
    uri_verdict(&r); status_is(&r, "400"); assert(t_verdict == 2);
}
static void test_ota_busy_does_not_read_or_write(void)
{
    t_busy = true;
    httpd_req_t r = request(NULL, t_image, sizeof t_image);
    assert(uri_ota(&r) == ESP_FAIL); status_is(&r, "409");
    assert(t_begin == 1 && t_end == 0 && r.recv_calls == 0 && t_written == 0);
}
static void test_ota_failures_always_release_reservation(void)
{
    for (int stage = 1; stage <= 5; stage++) {
        reset_fakes(); t_ota_fail = stage;
        httpd_req_t r = request(NULL, t_image, sizeof t_image); r.chunk_size = 4096;
        if (stage == 5) r.fail_after = 8192;
        uri_ota(&r);
        assert(strncmp(r.response_status, "200", 3));
        assert(t_begin == 1 && t_end == 1 && !t_held && !t_restart);
        assert(t_ota_abort == ((stage == 2 || stage == 5) ? 1 : 0));
    }
}
static void test_ota_success_holds_reservation_until_restart(void)
{
    httpd_req_t r = request(NULL, t_image, sizeof t_image); r.chunk_size = 4096;
    uri_ota(&r); status_is(&r, "200");
    assert(t_written == sizeof t_image && t_ota_end == 1 && t_boot_select == 1);
    assert(t_held && t_end == 0 && t_restart == 1);
}

int main(void)
{
    int count = 0;
#define RUN(fn) do { reset_fakes(); fn(); count++; } while (0)
    RUN(test_query_decodes_and_checks_decoded_size);
    RUN(test_command_waits_for_complete_body);
    RUN(test_partial_command_never_queued);
    RUN(test_oversize_and_nul_commands_rejected);
    RUN(test_queue_full_is_reported);
    RUN(test_wifi_spaces_and_literal_plus_survive);
    RUN(test_wifi_full_width_credentials_are_not_truncated);
    RUN(test_wifi_rejects_invalid_password_and_releases_on_save_error);
    RUN(test_nvs_reinitializes_after_erasing);
    RUN(test_campaign_start_reserved_and_parameters_decoded);
    RUN(test_campaign_failed_start_releases_reservation);
    RUN(test_campaign_racing_maintenance_prevents_start);
    RUN(test_bad_campaign_parameter_cannot_default_into_start);
    RUN(test_verdict_ids_and_names_are_strict);
    RUN(test_ota_busy_does_not_read_or_write);
    RUN(test_ota_failures_always_release_reservation);
    RUN(test_ota_success_holds_reservation_until_restart);
    printf("net: %d cases passed (including five OTA failure stages)\n", count);
    return 0;
}
