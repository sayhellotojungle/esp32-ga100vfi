// PMBus / I2C bridge for the MP2888B @ 0x25.
//
// IDF v5 i2c_master (DMA) driver; bus speed runtime-switchable (@F), default
// from Kconfig (400 kHz: the card's SMBus pull-ups are Fm-spec, Fm+ rise
// times are not guaranteed). Pull-up verification and the unpowered-card
// ESD-clamp discriminator follow detect.c.
#include "pmbus.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>

#include "console.h"

// Route command-path diagnostics into the web log ring too (see console.c).
// The IRAM bit-bang path never printfs, so this costs nothing there.
#define printf(...) console_printf(__VA_ARGS__)

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_attr.h"
#include "esp_rom_sys.h"
#include "soc/gpio_reg.h"
#include "soc/soc.h"

#include "board.h"
#include "glitch_plan.h"

#define I2C_PORT I2C_NUM_0

static i2c_master_bus_handle_t s_bus;
static i2c_master_dev_handle_t s_dev;
static uint32_t s_baud = GA_BAUD_HZ;
static uint32_t s_effective_baud = GA_BAUD_HZ;
static bool s_up;
static bool s_scl_held;

uint32_t pmbus_baud_hz(void) { return s_effective_baud; }
uint32_t pmbus_requested_baud_hz(void) { return s_baud; }
uint32_t pmbus_write_us(void) { return glitch_pmbus_w_us(s_effective_baud); }

// ------------------------------------------------------------------ pins
static void pin_input_no_pull(int pin)
{
    gpio_set_direction(pin, GPIO_MODE_INPUT);
    gpio_set_pull_mode(pin, GPIO_FLOATING);
}

// Discharge the line, release it against the ~45k internal pull-down and
// read back: a real external pull-up (a few kOhm) wins and reads high; a
// bare line stays low. Same trick as detect.c.
static bool external_pullup(int pin)
{
    gpio_set_direction(pin, GPIO_MODE_OUTPUT_OD);
    gpio_set_level(pin, 0);
    esp_rom_delay_us(10);
    pin_input_no_pull(pin);
    gpio_set_pull_mode(pin, GPIO_PULLDOWN_ONLY);
    esp_rom_delay_us(100);
    bool high = gpio_get_level(pin) != 0;
    gpio_set_pull_mode(pin, GPIO_FLOATING);
    return high;
}

// ------------------------------------------------------------------- bus
bool pmbus_up(void)
{
    if (s_scl_held) return false;
    if (s_up) return true;

    pin_input_no_pull(PIN_I2C_SDA);
    pin_input_no_pull(PIN_I2C_SCL);

    bool ext_sda = external_pullup(PIN_I2C_SDA);
    bool ext_scl = external_pullup(PIN_I2C_SCL);
    bool use_pulls = !(ext_sda && ext_scl);
    uint32_t eff_baud = s_baud;

    if (use_pulls) {
        printf("no external pull-up on%s%s: using ~45k internal pull-ups\n",
               ext_sda ? "" : " SDA", ext_scl ? "" : " SCL");
        gpio_set_pull_mode(PIN_I2C_SDA, GPIO_PULLUP_ONLY);
        gpio_set_pull_mode(PIN_I2C_SCL, GPIO_PULLUP_ONLY);
        esp_rom_delay_us(2000);
        if (!gpio_get_level(PIN_I2C_SDA) || !gpio_get_level(PIN_I2C_SCL)) {
            printf("still low with pull-ups: SDA=%d SCL=%d -- chip unpowered "
                   "(ESD diodes clamp the bus) or a line shorted to ground\n",
                   gpio_get_level(PIN_I2C_SDA), gpio_get_level(PIN_I2C_SCL));
            pin_input_no_pull(PIN_I2C_SDA);
            pin_input_no_pull(PIN_I2C_SCL);
            return false;
        }
        if (eff_baud > 100000) eff_baud = 100000;   // slow edges on 45k pulls
    }

    i2c_master_bus_config_t bcfg = {
        .i2c_port = I2C_PORT,
        .sda_io_num = PIN_I2C_SDA,
        .scl_io_num = PIN_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = use_pulls,
    };
    if (i2c_new_master_bus(&bcfg, &s_bus) != ESP_OK) {
        printf("I2C ERR bus init\n");
        pin_input_no_pull(PIN_I2C_SDA);
        pin_input_no_pull(PIN_I2C_SCL);
        return false;
    }
    i2c_device_config_t dcfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = PMBUS_ADDR,
        .scl_speed_hz = eff_baud,
        .scl_wait_us = 10000,          // tolerate clock stretching
    };
    if (i2c_master_bus_add_device(s_bus, &dcfg, &s_dev) != ESP_OK) {
        printf("I2C ERR dev init\n");
        i2c_del_master_bus(s_bus);
        s_bus = NULL;
        pin_input_no_pull(PIN_I2C_SDA);
        pin_input_no_pull(PIN_I2C_SCL);
        return false;
    }
    s_effective_baud = eff_baud;
    s_up = true;
    return true;
}

void pmbus_down(void)
{
    if (s_up) {
        i2c_master_bus_rm_device(s_dev);
        i2c_del_master_bus(s_bus);
        s_dev = NULL;
        s_bus = NULL;
        s_up = false;
    }
    pin_input_no_pull(PIN_I2C_SDA);
    pin_input_no_pull(PIN_I2C_SCL);
    s_scl_held = false;
}

bool pmbus_set_baud(unsigned long hz)
{
    if (hz != 100000 && hz != 400000 && hz != 1000000) return false;
    s_baud = hz;
    s_effective_baud = hz;
    if (s_up) pmbus_down();
    return true;
}

// ---------------------------------------------------------------- pmbus
// Hot path for the IRAM timeline: pmbus_up() returns immediately once the
// bus is established (flash cache is on during the sequence; only the
// no-cache-disabled invariant matters, and no flash op runs mid-sequence).
IRAM_ATTR bool pmbus_w(uint8_t reg, const uint8_t *d, int n)
{
    if (n < 0 || n > 3 || (n && !d)) return false;
    if (!s_up && !pmbus_up()) return false;
    uint8_t b[4];
    b[0] = reg;
    for (int i = 0; i < n; i++) b[1 + i] = d[i];
    return i2c_master_transmit(s_dev, b, n + 1, I2C_TO_MS) == ESP_OK;
}

IRAM_ATTR bool pmbus_w16(uint8_t reg, uint16_t v)
{
    uint8_t d[2] = { (uint8_t)v, (uint8_t)(v >> 8) };
    return pmbus_w(reg, d, 2);
}

IRAM_ATTR bool pmbus_r(uint8_t reg, uint8_t *d, int n)
{
    if (!d || n <= 0) return false;
    if (!pmbus_up()) return false;
    return i2c_master_transmit_receive(s_dev, &reg, 1, d, n, I2C_TO_MS) == ESP_OK;
}

bool pmbus_raw_write(uint8_t addr, const uint8_t *buf, int n)
{
    if (addr > 0x7f || !buf || n <= 0) return false;
    if (!pmbus_up()) return false;
    i2c_device_config_t dc = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = addr,
        .scl_speed_hz = s_effective_baud,
        .scl_wait_us = 10000,
    };
    i2c_master_dev_handle_t dev;
    if (i2c_master_bus_add_device(s_bus, &dc, &dev) != ESP_OK) return false;
    bool ok = i2c_master_transmit(dev, buf, n, I2C_TO_MS) == ESP_OK;
    i2c_master_bus_rm_device(dev);
    return ok;
}

bool pmbus_raw_read(uint8_t addr, uint8_t *buf, int n)
{
    if (addr > 0x7f || !buf || n <= 0) return false;
    if (!pmbus_up()) return false;
    i2c_device_config_t dc = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = addr,
        .scl_speed_hz = s_effective_baud,
        .scl_wait_us = 10000,
    };
    i2c_master_dev_handle_t dev;
    if (i2c_master_bus_add_device(s_bus, &dc, &dev) != ESP_OK) return false;
    bool ok = i2c_master_receive(dev, buf, n, I2C_TO_MS) == ESP_OK;
    i2c_master_bus_rm_device(dev);
    return ok;
}

// ---------------------------------------------------------------- pex_rst
void pex_init(void)
{
    // Open-drain output, idle level 1 == hi-Z: can only ever sink current.
    // Preset the output latch (resets to 0) BEFORE switching to OD so the
    // direction flip cannot briefly drive the 1V8 net low; then OD mode:
    // level 0 sinks, level 1 is hi-Z, push-pull high is unreachable.
    gpio_set_direction(PIN_PEX_RST, GPIO_MODE_INPUT);
    gpio_set_pull_mode(PIN_PEX_RST, GPIO_FLOATING);
    pex_hold(false);
    gpio_set_direction(PIN_PEX_RST, GPIO_MODE_OUTPUT_OD);
    gpio_set_level(PIN_PEX_RST, 1);
}

// Used from the hardware-timer ISR as well as the command task. Direct latch
// writes stay IRAM-safe and preserve the pad's permanently open-drain mode.
void IRAM_ATTR pex_hold(bool low)
{
#if PIN_PEX_RST < 32
    if (low) REG_WRITE(GPIO_OUT_W1TC_REG, 1u << PIN_PEX_RST);
    else REG_WRITE(GPIO_OUT_W1TS_REG, 1u << PIN_PEX_RST);
#else
    if (low) REG_WRITE(GPIO_OUT1_W1TC_REG, 1u << (PIN_PEX_RST - 32));
    else REG_WRITE(GPIO_OUT1_W1TS_REG, 1u << (PIN_PEX_RST - 32));
#endif
}

void pmbus_scl_hold(bool low)
{
    if (low) {
        if (s_scl_held) return;
        if (s_up) pmbus_down();
        gpio_set_direction(PIN_I2C_SCL, GPIO_MODE_OUTPUT_OD);
        gpio_set_pull_mode(PIN_I2C_SCL, GPIO_FLOATING);
        gpio_set_level(PIN_I2C_SCL, 0);
        s_scl_held = true;
    } else {
        if (!s_scl_held) return;  // do not disable SCL on a live hardware bus
        pin_input_no_pull(PIN_I2C_SCL);
        s_scl_held = false;
    }
}

// -------------------------------------------------------------- serial @"
// Bit-bang probe with SDA/SCL roles swapped (detect.c discriminator for a
// crossed pair, which hardware I2C cannot see). ~25 kHz.
#define BB_SDA     PIN_I2C_SCL   // swapped
#define BB_SCL     PIN_I2C_SDA
#define BB_HALF_US 20

static void bb_low(int pin)  { gpio_set_direction(pin, GPIO_MODE_OUTPUT_OD); gpio_set_level(pin, 0); }
static void bb_rel(int pin)  { pin_input_no_pull(pin); }

static bool bb_scl_high(void)
{
    for (int t = 0; t < 20000; t++) {
        if (gpio_get_level(BB_SCL)) return true;
        esp_rom_delay_us(1);
    }
    return false;
}

static bool bb_probe(uint8_t addr)
{
    if (!gpio_get_level(BB_SDA) || !gpio_get_level(BB_SCL)) return false;   // bus not idle
    bool ok = false;
    bb_rel(BB_SDA); bb_rel(BB_SCL);
    esp_rom_delay_us(BB_HALF_US);
    bb_low(BB_SDA);                       // START
    esp_rom_delay_us(BB_HALF_US);
    bb_low(BB_SCL);
    esp_rom_delay_us(BB_HALF_US);
    uint8_t byte = (uint8_t)((addr << 1) | 1);
    for (int i = 7; i >= 0; i--) {
        if (byte & (1 << i)) bb_rel(BB_SDA); else bb_low(BB_SDA);
        esp_rom_delay_us(BB_HALF_US);
        bb_rel(BB_SCL);
        if (!bb_scl_high()) goto stop;
        esp_rom_delay_us(BB_HALF_US);
        bb_low(BB_SCL);
        esp_rom_delay_us(BB_HALF_US);
    }
    // Address ACK, followed by a complete data byte and a master NACK.
    bb_rel(BB_SDA);
    esp_rom_delay_us(BB_HALF_US);
    bb_rel(BB_SCL);
    if (!bb_scl_high()) goto stop;
    esp_rom_delay_us(BB_HALF_US);
    bool ack = !gpio_get_level(BB_SDA);
    bb_low(BB_SCL);
    esp_rom_delay_us(BB_HALF_US);
    if (!ack) goto stop;
    for (int i = 0; i < 8; i++) {
        bb_rel(BB_SDA);
        esp_rom_delay_us(BB_HALF_US);
        bb_rel(BB_SCL);
        if (!bb_scl_high()) goto stop;
        esp_rom_delay_us(BB_HALF_US);
        bb_low(BB_SCL);
        esp_rom_delay_us(BB_HALF_US);
    }
    bb_rel(BB_SDA);                       // NACK the one-byte read
    esp_rom_delay_us(BB_HALF_US);
    bb_rel(BB_SCL);
    if (!bb_scl_high()) goto stop;
    esp_rom_delay_us(BB_HALF_US);
    bb_low(BB_SCL);
    esp_rom_delay_us(BB_HALF_US);
    ok = true;
stop:
    bb_low(BB_SCL);
    bb_low(BB_SDA);
    esp_rom_delay_us(BB_HALF_US);
    bb_rel(BB_SCL);
    if (!bb_scl_high()) ok = false;
    esp_rom_delay_us(BB_HALF_US);
    bb_rel(BB_SDA);
    esp_rom_delay_us(BB_HALF_US);
    bb_rel(BB_SCL);                       // release both pins on every exit
    return ok;
}

static void bb_scan(void)
{
    pmbus_down();
    bb_rel(BB_SDA); bb_rel(BB_SCL);
    char reply[400];  // prefix + all 112 scanned addresses + terminator
    size_t used = (size_t)snprintf(reply, sizeof reply,
                                 "bit-bang scan, SDA=%d SCL=%d (swapped):", BB_SDA, BB_SCL);
    bool found = false;
    for (uint8_t a = 0x08; a < 0x78; a++) {
        if (bb_probe(a)) {
            used += (size_t)snprintf(reply + used, sizeof reply - used, " %02x", a);
            found = true;
        }
    }
    bb_rel(BB_SDA); bb_rel(BB_SCL);
    snprintf(reply + used, sizeof reply - used, found ? "\n" : " none\n");
    printf("%s", reply);
}

static const char *skip_space(const char *p)
{
    while (isspace((unsigned char)*p)) p++;
    return p;
}

void pmbus_cmd(char *line)
{
    if (!line || !line[0]) { printf("I2C ERR usage\n"); return; }
    char op = line[0];
    if ((op == 'q' || op == 'L' || op == 'U' || op == 'x' || op == 's') &&
        *skip_space(line + 1)) { printf("I2C ERR usage\n"); return; }
    if (op == 'q') { pmbus_down(); printf("I2C released\n"); return; }
    if (op == 'L') { pmbus_scl_hold(true);  printf("I2C SCL held low\n"); return; }
    if (op == 'U') { pmbus_scl_hold(false); printf("I2C SCL released\n"); return; }
    if (op == 'F') {
        char *end;
        errno = 0;
        unsigned long hz = strtoul(line + 1, &end, 10);
        if (end != line + 1 && !errno && !*skip_space(end) && pmbus_set_baud(hz))
            printf("I2C baud %lu\n", hz);
        else printf("I2C ERR baud\n");
        return;
    }
    if (op == 'x') { bb_scan(); return; }
    if (op != 's' && op != 'r' && op != 'w') { printf("I2C ERR usage\n"); return; }

    uint8_t b[32];
    int nb = 0;
    char *p = line + 1, *end;
    while (nb < (int)sizeof b) {
        p = (char *)skip_space(p);
        if (!*p) break;
        errno = 0;
        unsigned long v = strtoul(p, &end, 16);
        if (end == p || errno || v > 255 || (*end && !isspace((unsigned char)*end))) {
            printf("I2C ERR usage\n");
            return;
        }
        b[nb++] = (uint8_t)v;
        p = end;
    }
    if (*skip_space(p) || (nb && b[0] > 0x7f) ||
        (op == 'r' && (nb != 3 || b[2] == 0 || b[2] > 32)) ||
        (op == 'w' && nb < 2)) { printf("I2C ERR usage\n"); return; }
    if (!pmbus_up()) { printf("I2C ERR bus unavailable or SCL held\n"); return; }

    if (op == 's') {
        char reply[352];  // prefix + 112 three-character addresses + newline
        size_t used = (size_t)snprintf(reply, sizeof reply, "I2C scan:");
        for (uint16_t a = 0x08; a < 0x78; a++) {
            if (i2c_master_probe(s_bus, a, I2C_TO_MS) == ESP_OK)
                used += (size_t)snprintf(reply + used, sizeof reply - used, " %02x", a);
        }
        snprintf(reply + used, sizeof reply - used, "\n");
        printf("%s", reply);
    } else if (op == 'r' && nb == 3 && b[2] > 0 && b[2] <= 32) {
        // @r AA CC N: read N bytes from register CC of address AA
        uint8_t d[32];
        bool ok = false;
        if (b[0] == PMBUS_ADDR) {
            ok = pmbus_r(b[1], d, b[2]);
        } else {
            i2c_device_config_t dc = {
                .dev_addr_length = I2C_ADDR_BIT_LEN_7,
                .device_address = b[0],
                .scl_speed_hz = s_effective_baud,
                .scl_wait_us = 10000,
            };
            i2c_master_dev_handle_t dev;
            if (i2c_master_bus_add_device(s_bus, &dc, &dev) == ESP_OK) {
                ok = i2c_master_transmit_receive(dev, &b[1], 1, d, b[2],
                                                 I2C_TO_MS) == ESP_OK;
                i2c_master_bus_rm_device(dev);
            }
        }
        if (!ok) { printf("I2C ERR\n"); return; }
        char reply[104];  // "I2C OK" + 32 hex bytes + newline + NUL
        size_t used = (size_t)snprintf(reply, sizeof reply, "I2C OK");
        for (int i = 0; i < b[2]; i++)
            used += (size_t)snprintf(reply + used, sizeof reply - used, " %02x", d[i]);
        snprintf(reply + used, sizeof reply - used, "\n");
        printf("%s", reply);
    } else if (op == 'w' && nb >= 2) {
        bool ok = false;
        if (b[0] == PMBUS_ADDR) {
            ok = i2c_master_transmit(s_dev, &b[1], nb - 1, I2C_TO_MS) == ESP_OK;
        } else {
            i2c_device_config_t dc = {
                .dev_addr_length = I2C_ADDR_BIT_LEN_7,
                .device_address = b[0],
                .scl_speed_hz = s_effective_baud,
                .scl_wait_us = 10000,
            };
            i2c_master_dev_handle_t dev;
            if (i2c_master_bus_add_device(s_bus, &dc, &dev) == ESP_OK) {
                ok = i2c_master_transmit(dev, &b[1], nb - 1, I2C_TO_MS) == ESP_OK;
                i2c_master_bus_rm_device(dev);
            }
        }
        printf(ok ? "I2C OK\n" : "I2C ERR\n");
    } else {
        printf("I2C ERR usage\n");
    }
}
