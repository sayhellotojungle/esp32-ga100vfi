/* Host-only pin/I2C model. It cannot access physical hardware. */
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "board.h"
#ifdef TEST_PEX_HIGH_PIN
#undef PIN_PEX_RST
#define PIN_PEX_RST 38
#endif
#include "../main/pmbus.c"
#undef printf

static int checks, failures;
#define CHECK(c) do { checks++; if (!(c)) { failures++; \
    printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); } } while (0)
static int direction[64], latch[64], pull[64];
static bool external_pulls = true, track_bb, transaction, nack_address;
static unsigned clocks, starts, stops, nacks, stretch_at;
static int old_sda = 1, old_scl = 1;
static unsigned bus_inits, tx_count, last_speed, last_reg;
static uint32_t last_reg_value;
struct native_bus { int unused; };
struct native_dev { uint32_t speed; uint8_t addr; };
static struct native_bus bus;
static struct native_dev devices[64];
static unsigned device_count;
static unsigned log_calls, log_fragments;
static char last_line[512];

static int master_level(int pin)
{
    if (direction[pin] == GPIO_MODE_OUTPUT_OD && !latch[pin]) return 0;
    return external_pulls || pull[pin] == GPIO_PULLUP_ONLY;
}
static void update_bus(void)
{
    if (!track_bb) return;
    int sda = master_level(BB_SDA), scl = master_level(BB_SCL);
    if (scl && old_scl && old_sda && !sda) {
        transaction = true; clocks = 0; starts++;
    }
    if (transaction && !old_scl && scl) {
        clocks++;
        if (clocks == 18 && direction[BB_SDA] == GPIO_MODE_INPUT) nacks++;
    }
    if (transaction && scl && old_scl && !old_sda && sda) {
        transaction = false; stops++;
    }
    old_sda = sda; old_scl = scl;
}
esp_err_t gpio_set_direction(int pin, int mode)
{ direction[pin] = mode; update_bus(); return ESP_OK; }
esp_err_t gpio_set_level(int pin, int level)
{ latch[pin] = level; update_bus(); return ESP_OK; }
esp_err_t gpio_set_pull_mode(int pin, int mode)
{ pull[pin] = mode; return ESP_OK; }
int gpio_get_level(int pin)
{
    if (track_bb && transaction) {
        if (pin == BB_SCL && stretch_at && clocks >= stretch_at) return 0;
        if (pin == BB_SDA && clocks == 9) return nack_address ? 1 : 0;
    }
    return master_level(pin);
}
void esp_rom_delay_us(uint32_t us) { (void)us; }
void native_reg_write(unsigned reg, uint32_t value)
{ last_reg = reg; last_reg_value = value; }
int console_printf(const char *fmt, ...)
{
    va_list ap; va_start(ap, fmt);
    int n = vsnprintf(last_line, sizeof last_line, fmt, ap);
    va_end(ap);
    log_calls++;
    size_t len = strlen(last_line);
    if (!len || last_line[len - 1] != '\n') log_fragments++;
    return n;
}
esp_err_t i2c_new_master_bus(const i2c_master_bus_config_t *cfg, i2c_master_bus_handle_t *h)
{
    (void)cfg; *h = &bus; bus_inits++;
    direction[PIN_I2C_SCL] = GPIO_MODE_OUTPUT_OD; latch[PIN_I2C_SCL] = 1;
    return ESP_OK;
}
esp_err_t i2c_del_master_bus(i2c_master_bus_handle_t h) { (void)h; return ESP_OK; }
esp_err_t i2c_master_bus_add_device(i2c_master_bus_handle_t h,
    const i2c_device_config_t *cfg, i2c_master_dev_handle_t *dev)
{
    (void)h;
    if (device_count >= sizeof devices / sizeof devices[0]) return ESP_FAIL;
    devices[device_count] = (struct native_dev){ cfg->scl_speed_hz, cfg->device_address };
    *dev = &devices[device_count++]; last_speed = cfg->scl_speed_hz;
    return ESP_OK;
}
esp_err_t i2c_master_bus_rm_device(i2c_master_dev_handle_t dev) { (void)dev; return ESP_OK; }
esp_err_t i2c_master_transmit(i2c_master_dev_handle_t dev, const uint8_t *p, size_t n, int timeout)
{ (void)p; (void)n; (void)timeout; tx_count++; last_speed = dev->speed; return ESP_OK; }
esp_err_t i2c_master_receive(i2c_master_dev_handle_t dev, uint8_t *p, size_t n, int timeout)
{ (void)timeout; memset(p, 0, n); last_speed = dev->speed; return ESP_OK; }
esp_err_t i2c_master_transmit_receive(i2c_master_dev_handle_t dev,
    const uint8_t *a, size_t na, uint8_t *b, size_t nb, int timeout)
{ (void)a; (void)na; return i2c_master_receive(dev, b, nb, timeout); }
esp_err_t i2c_master_probe(i2c_master_bus_handle_t h, uint16_t addr, int timeout)
{ (void)h; (void)addr; (void)timeout; return ESP_FAIL; }

static void test_effective_baud_and_holds(void)
{
    external_pulls = false;
    CHECK(pmbus_set_baud(1000000));
    CHECK(pmbus_up());
    CHECK(pmbus_requested_baud_hz() == 1000000);
    CHECK(pmbus_baud_hz() == 100000 && pmbus_write_us() == 360);
    CHECK(last_speed == 100000);
    uint8_t b[2] = { 0, 0 };
    CHECK(pmbus_raw_write(0x3a, b, sizeof b) && last_speed == 100000);
    CHECK(pmbus_raw_read(0x3a, b, sizeof b) && last_speed == 100000);
    char rd[] = "r 3a 31 02", wr[] = "w 3a 31 00";
    pmbus_cmd(rd); CHECK(last_speed == 100000);
    pmbus_cmd(wr); CHECK(last_speed == 100000);
    pmbus_scl_hold(true);
    unsigned inits = bus_inits, writes = tx_count;
    CHECK(s_scl_held && !s_up);
    CHECK(!pmbus_w16(0x21, 100) && !pmbus_raw_write(0x3a, b, 2));
    CHECK(!pmbus_raw_read(0x3a, b, 2));
    CHECK(bus_inits == inits && tx_count == writes);
    CHECK(direction[PIN_I2C_SCL] == GPIO_MODE_OUTPUT_OD && !latch[PIN_I2C_SCL]);
    pmbus_scl_hold(false); CHECK(!s_scl_held);
    CHECK(pmbus_up());
    int mode = direction[PIN_I2C_SCL];
    pmbus_scl_hold(false); CHECK(direction[PIN_I2C_SCL] == mode && s_up);
    pmbus_down(); external_pulls = true;
    CHECK(pmbus_set_baud(400000) && pmbus_up());
    CHECK(pmbus_baud_hz() == 400000 && pmbus_write_us() == 90);
    unsigned calls = log_calls, fragments = log_fragments;
    char read_max[] = "r 25 21 20";
    pmbus_cmd(read_max);
    CHECK(log_calls == calls + 1 && log_fragments == fragments);
    CHECK(strlen(last_line) == 103 && !strncmp(last_line, "I2C OK ", 7) && last_line[102] == '\n');
    calls = log_calls;
    char scan[] = "s";
    pmbus_cmd(scan);
    CHECK(log_calls == calls + 1 && !strcmp(last_line, "I2C scan:\n"));
}

static void test_scan_completes_each_read_and_stop(void)
{
    pmbus_down();
    memset(direction, 0, sizeof direction); memset(latch, 0, sizeof latch);
    old_sda = old_scl = 1; track_bb = true;
    starts = stops = nacks = 0; transaction = false;
    CHECK(bb_probe(0x25));
    CHECK(!transaction && starts == 1 && stops == 1 && nacks == 1 && clocks == 19);
    CHECK(direction[BB_SDA] == GPIO_MODE_INPUT && direction[BB_SCL] == GPIO_MODE_INPUT);
    CHECK(bb_probe(0x3a));
    CHECK(starts == 2 && stops == 2 && nacks == 2);
    nack_address = true;
    CHECK(!bb_probe(0x40) && !transaction);
    CHECK(stops == 3 && nacks == 2);
    nack_address = false; stretch_at = 5;
    CHECK(!bb_probe(0x25));
    CHECK(direction[BB_SDA] == GPIO_MODE_INPUT && direction[BB_SCL] == GPIO_MODE_INPUT);
    stretch_at = 0; transaction = false; old_sda = old_scl = 1;
    starts = stops = nacks = 0;
    unsigned calls = log_calls;
    bb_scan();
    CHECK(starts == 0x78 - 0x08 && stops == starts && nacks == starts);
    CHECK(log_calls == calls + 1 && !log_fragments);
    CHECK(!transaction && direction[BB_SDA] == GPIO_MODE_INPUT && direction[BB_SCL] == GPIO_MODE_INPUT);
    track_bb = false;
}

static void test_rejected_commands_do_not_write(void)
{
    unsigned writes = tx_count, inits = bus_inits;
    char *bad[] = { "w 25 21 100", "w 125 21 00", "w 25 21 bad!", "r 25 21 00",
                    "r 25 21 21", "F400000x", "x garbage", "s garbage", "unknown" };
    for (unsigned i = 0; i < sizeof bad / sizeof bad[0]; i++) pmbus_cmd(bad[i]);
    CHECK(tx_count == writes && bus_inits == inits);
    CHECK(!pmbus_w(0x21, NULL, 1));
    CHECK(!pmbus_raw_write(0xff, (const uint8_t *)"a", 1));
    CHECK(!pmbus_r(0x21, NULL, 2));
}

static void test_reset_gpio_bank(void)
{
    pex_init();
    CHECK(direction[PIN_PEX_RST] == GPIO_MODE_OUTPUT_OD);
    pex_hold(true);
#if PIN_PEX_RST < 32
    CHECK(last_reg == GPIO_OUT_W1TC_REG && last_reg_value == (1u << PIN_PEX_RST));
#else
    CHECK(last_reg == GPIO_OUT1_W1TC_REG && last_reg_value == (1u << (PIN_PEX_RST - 32)));
#endif
    pex_hold(false);
#if PIN_PEX_RST < 32
    CHECK(last_reg == GPIO_OUT_W1TS_REG && last_reg_value == (1u << PIN_PEX_RST));
#else
    CHECK(last_reg == GPIO_OUT1_W1TS_REG && last_reg_value == (1u << (PIN_PEX_RST - 32)));
#endif
    CHECK(direction[PIN_PEX_RST] == GPIO_MODE_OUTPUT_OD);
}

int main(void)
{
    test_effective_baud_and_holds();
    test_scan_completes_each_read_and_stop();
    test_rejected_commands_do_not_write();
    test_reset_gpio_bank();
    printf("pmbus: %d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
