#include "console.h"
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define COMMAND_QUEUE 16
#define LOG_RING 16384
typedef struct { char text[GA_COMMAND_MAX]; } input_t;
static QueueHandle_t s_in;
static SemaphoreHandle_t s_log_mx;
static char s_log[LOG_RING];
static uint32_t s_seq;
static bool s_ready;

static void pump_task(void *arg)
{
    (void)arg;
    ga_uart_parser_t parser = { 0 };
    /* 一次抽 512 B、空转只睡 2 ms（原来是 64 B 与 10 ms）。Arduino 的预编译 sdkconfig 里没有
     * CONFIG_ESP_CONSOLE_UART_RX_BUFFER_SIZE，控制台 UART 因此只有硬件 FIFO 那点缓冲（S3 为
     * 128 B）。0.5.1 在本机实测的行长度台阶：47 B 与 71 B 十发十中、107 B 全中、185 B 中 2、
     * 257 B 全灭；溢出丢弃之后，尾部字节被当作新的一行起始解析。抽得干净、睡得短是固件侧能为
     * FIFO 腾出余量的全部手段，更长的行要靠发送方控制长度（见 PORT_NOTES 的 128 B 预算）。 */
    char out[GA_COMMAND_MAX], b[512];
    for (;;) {
        ssize_t n = read(0, b, sizeof b);
        if (n <= 0) { vTaskDelay(pdMS_TO_TICKS(n < 0 ? 50 : 2)); continue; }
        for (ssize_t i = 0; i < n; i++) {
            int ready = ga_uart_feed(&parser, (unsigned char)b[i], out);
            if (ready < 0) console_printf("ERR invalid or overlong command\n");
            else if (ready > 0 && !console_input_push(out, (int)strlen(out)))
                console_printf("ERR command queue full\n");
        }
    }
}

void console_init(void)
{
    s_log_mx = xSemaphoreCreateMutex();
    s_in = xQueueCreate(COMMAND_QUEUE, sizeof(input_t));
    ESP_ERROR_CHECK(s_log_mx && s_in ? ESP_OK : ESP_ERR_NO_MEM);
    s_ready = true;
    ESP_ERROR_CHECK(xTaskCreatePinnedToCore(pump_task, "ga_uart", 4096, NULL,
                                          3, NULL, 0) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
}

int console_printf(const char *fmt, ...)
{
    static char tmp[1024];
    va_list ap; va_start(ap, fmt);
    if (!s_ready) { int n = vprintf(fmt, ap); va_end(ap); return n; }
    xSemaphoreTake(s_log_mx, portMAX_DELAY);
    int n = vsnprintf(tmp, sizeof tmp, fmt, ap); va_end(ap);
    if (n > 0) {
        fputs(tmp, stdout); fflush(stdout);
        size_t len = (size_t)n < sizeof tmp ? (size_t)n : sizeof tmp - 1;
        for (size_t i = 0; i < len; i++) s_log[(s_seq + i) % LOG_RING] = tmp[i];
        s_seq += (uint32_t)len;
    }
    xSemaphoreGive(s_log_mx); return n;
}

bool console_input_push(const char *buf, int len)
{
    input_t item = { 0 };
    if (!s_ready || len <= 0 || !ga_command_normalize(buf, (size_t)len, item.text)) return false;
    return xQueueSend(s_in, &item, 0) == pdTRUE;
}

int console_read_command(char *out, size_t capacity, int timeout_ms)
{
    if (!out || capacity < GA_COMMAND_MAX) return -1;
    input_t item;
    TickType_t timeout = timeout_ms < 0 ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms);
    if (timeout_ms > 0 && !timeout) timeout = 1;
    if (!xQueueReceive(s_in, &item, timeout)) return 0;
    memcpy(out, item.text, sizeof item.text); return (int)strlen(out);
}

uint32_t console_log_head(void)
{
    xSemaphoreTake(s_log_mx, portMAX_DELAY); uint32_t head = s_seq;
    xSemaphoreGive(s_log_mx); return head;
}

int console_log_read(uint32_t *pos, char *out, int max)
{
    if (!pos || !out || max <= 0) return 0;
    xSemaphoreTake(s_log_mx, portMAX_DELAY);
    uint32_t head = s_seq;
    uint32_t oldest = head > LOG_RING ? head - LOG_RING : 0;
    /* A browser may retain its old cursor across firmware restart. */
    if (*pos > head || *pos < oldest) *pos = oldest;
    uint32_t n = head - *pos; if (n > (uint32_t)max) n = (uint32_t)max;
    for (uint32_t i = 0; i < n; i++) out[i] = s_log[(*pos + i) % LOG_RING];
    *pos += n; xSemaphoreGive(s_log_mx); return (int)n;
}
