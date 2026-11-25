#pragma once
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

/* SDK-shaped offline doubles. No socket, task, flash or GPIO operation runs. */
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL (-1)
#define ESP_ERR_INVALID_STATE 0x101
#define ESP_ERR_NOT_FOUND 0x102
#define ESP_ERR_HTTPD_RESULT_TRUNC 0x103
#define ESP_ERR_HTTPD_HANDLERS_FULL 0xb001
#define ESP_ERR_NVS_NOT_FOUND 0x104
#define ESP_ERR_NVS_NO_FREE_PAGES 0x105
#define ESP_ERR_NVS_NEW_VERSION_FOUND 0x106
#define ESP_ERROR_CHECK(expr) assert((expr) == ESP_OK)
const char *esp_err_to_name(esp_err_t error);
size_t strlcpy(char *dst, const char *src, size_t n);

typedef int portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(mux) ((void)(mux))
#define portEXIT_CRITICAL(mux) ((void)(mux))
#define pdMS_TO_TICKS(ms) (ms)
#define pdPASS 1
int xTaskCreate(void (*entry)(void *), const char *name, uint32_t stack,
                void *arg, unsigned priority, void *handle);
void vTaskDelay(unsigned ticks);

typedef const void *esp_event_base_t;
#define IP_EVENT ((esp_event_base_t)1)
#define WIFI_EVENT ((esp_event_base_t)2)
#define IP_EVENT_STA_GOT_IP 1
#define WIFI_EVENT_STA_DISCONNECTED 2
#define ESP_EVENT_ANY_ID (-1)
typedef struct { unsigned a, b, c, d; } mock_ip_t;
typedef struct { struct { mock_ip_t ip; } ip_info; } ip_event_got_ip_t;
#define IPSTR "%u.%u.%u.%u"
#define IP2STR(ip) (ip)->a, (ip)->b, (ip)->c, (ip)->d
int esp_event_loop_create_default(void);
int esp_event_handler_instance_register(esp_event_base_t base, int id,
    void (*handler)(void *, esp_event_base_t, int32_t, void *), void *arg, void *instance);
int esp_netif_init(void);
void *esp_netif_create_default_wifi_sta(void);
void *esp_netif_create_default_wifi_ap(void);

#define WIFI_AUTH_OPEN 0
#define WIFI_AUTH_WPA2_PSK 2
#define WIFI_STORAGE_RAM 0
#define WIFI_MODE_APSTA 3
#define WIFI_IF_STA 0
#define WIFI_IF_AP 1
typedef union {
    struct { uint8_t ssid[32], password[64]; struct { int authmode; } threshold; } sta;
    struct { uint8_t ssid[32], password[64]; unsigned ssid_len, channel, max_connection; int authmode; } ap;
} wifi_config_t;
typedef struct { int unused; } wifi_init_config_t;
#define WIFI_INIT_CONFIG_DEFAULT() ((wifi_init_config_t){0})
typedef struct { int rssi; } wifi_ap_record_t;
int esp_wifi_init(const wifi_init_config_t *config);
int esp_wifi_set_storage(int storage);
int esp_wifi_set_mode(int mode);
int esp_wifi_set_config(int interface, const wifi_config_t *config);
int esp_wifi_start(void);
int esp_wifi_stop(void);
int esp_wifi_connect(void);
int esp_wifi_disconnect(void);
int esp_wifi_sta_get_ap_info(wifi_ap_record_t *info);

typedef int nvs_handle_t;
#define NVS_READONLY 0
#define NVS_READWRITE 1
int nvs_open(const char *name, int mode, nvs_handle_t *handle);
int nvs_get_str(nvs_handle_t handle, const char *key, char *value, size_t *size);
int nvs_set_str(nvs_handle_t handle, const char *key, const char *value);
int nvs_commit(nvs_handle_t handle);
void nvs_close(nvs_handle_t handle);
int nvs_flash_init(void);
int nvs_flash_erase(void);

typedef struct httpd_req {
    size_t content_len;
    const char *query, *body;
    size_t read_pos, chunk_size, fail_after;
    int recv_calls;
    char response_status[64], response_body[2048];
    bool connection_close;
} httpd_req_t;
typedef void *httpd_handle_t;
typedef struct { int task_priority, stack_size, core_id, max_uri_handlers; bool lru_purge_enable; } httpd_config_t;
#define HTTPD_DEFAULT_CONFIG() ((httpd_config_t){0})
#define HTTP_GET 0
#define HTTP_POST 1
typedef struct { const char *uri; int method; esp_err_t (*handler)(httpd_req_t *); } httpd_uri_t;
int httpd_start(httpd_handle_t *handle, const httpd_config_t *config);
int httpd_register_uri_handler(httpd_handle_t handle, const httpd_uri_t *uri);
size_t httpd_req_get_url_query_len(httpd_req_t *request);
int httpd_req_get_url_query_str(httpd_req_t *request, char *out, size_t size);
int httpd_query_key_value(const char *query, const char *key, char *out, size_t size);
int httpd_req_recv(httpd_req_t *request, char *out, size_t size);
int httpd_resp_set_status(httpd_req_t *request, const char *status);
int httpd_resp_set_type(httpd_req_t *request, const char *type);
int httpd_resp_set_hdr(httpd_req_t *request, const char *key, const char *value);
int httpd_resp_send(httpd_req_t *request, const char *body, ssize_t length);
int httpd_resp_sendstr(httpd_req_t *request, const char *body);

typedef struct { size_t size; const char *label; } esp_partition_t;
typedef unsigned esp_ota_handle_t;
const esp_partition_t *esp_ota_get_next_update_partition(const esp_partition_t *current);
int esp_ota_begin(const esp_partition_t *partition, size_t length, esp_ota_handle_t *handle);
int esp_ota_write(esp_ota_handle_t handle, const void *data, size_t length);
int esp_ota_abort(esp_ota_handle_t handle);
int esp_ota_end(esp_ota_handle_t handle);
int esp_ota_set_boot_partition(const esp_partition_t *partition);
void esp_restart(void);
