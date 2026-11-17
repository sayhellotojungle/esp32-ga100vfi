#include "oracle.h"
#include "pmbus.h"
#include "console.h"

#include <string.h>
#include <stdatomic.h>

// GPU BAR0 offsets from host/ga100vfi/classify.py
#define R_DSOV       0x00820084u
#define R_FPF_ROW    0x00824314u
#define R_DSOV_SET   0x00820040u
#define R_PRIV_MASK  0x008200fcu
#define R_CRC_STATUS 0x00820070u
#define CRC_GOOD     0x55u
#define UNREADABLE   0xFFFFFFFFu

// SMBPBI: SMBus BlockData, fixed 16-byte payload, GPU endpoint @ 0x3A.
// Wire (write): addr+W, 0x10, cmd, payload[0..14].  (read): addr+R, count,
// payload[0..15] where payload[0] echoes the cmd.
#define GPU_SMB_ADDR   0x3A
#define SMBPBI_WR_CMD  0x31
#define SMBPBI_RD_CMD  0x32
#define SMBPBI_FRAME   16            // payload bytes, spec-fixed

static atomic_int s_mode;

static bool reg_op(uint8_t cmd, uint32_t addr, uint32_t *val /* in+out */)
{
    uint8_t f[1 + SMBPBI_FRAME];
    if (!pmbus_up()) return false;
    memset(f, 0, sizeof f);
    f[0] = SMBPBI_FRAME;
    f[1] = cmd;
    f[2] = 1;                                   // one register word
    f[3] = (uint8_t)addr;  f[4] = (uint8_t)(addr >> 8);
    f[5] = (uint8_t)(addr >> 16); f[6] = (uint8_t)(addr >> 24);
    if (cmd == SMBPBI_WR_CMD && val) {
        uint32_t v = *val;
        f[7] = (uint8_t)v;  f[8] = (uint8_t)(v >> 8);
        f[9] = (uint8_t)(v >> 16); f[10] = (uint8_t)(v >> 24);
    }
    if (!pmbus_raw_write(GPU_SMB_ADDR, f, sizeof f)) return false;
    if (cmd == SMBPBI_WR_CMD) return true;
    if (!pmbus_raw_read(GPU_SMB_ADDR, f, sizeof f)) return false;
    if (f[0] != SMBPBI_FRAME && f[0] != cmd) return false;    // framing off
    if (val)
        *val = (uint32_t)f[2] | ((uint32_t)f[3] << 8) |
               ((uint32_t)f[4] << 16) | ((uint32_t)f[5] << 24);
    return true;
}

static bool rd32(uint32_t a, uint32_t *v) { return reg_op(SMBPBI_RD_CMD, a, v); }
static bool wr32(uint32_t a, uint32_t v)  { return reg_op(SMBPBI_WR_CMD, a, &v); }

oracle_t oracle_run_smbpbi(bool *lost)
{
    uint32_t dsov = UNREADABLE, fpf = UNREADABLE, wb = 0, priv = 0, crc = 0;
    if (lost) *lost = true;
    if (!rd32(R_DSOV, &dsov) || dsov == UNREADABLE ||
        !rd32(R_FPF_ROW, &fpf) || !wr32(R_DSOV_SET, 1) || !rd32(R_DSOV_SET, &wb) ||
        !rd32(R_PRIV_MASK, &priv) || !rd32(R_CRC_STATUS, &crc))
        return ORACLE_LOST;
    if (lost) *lost = false;

    bool ok_dsov0  = (dsov == 0);
    bool ok_fpf    = (fpf != UNREADABLE);
    bool ok_set    = (wb == 1);
    bool ok_priv   = (priv == UNREADABLE);
    bool ok_crc    = (crc == CRC_GOOD);
    if (ok_dsov0 && ok_fpf && ok_set && ok_priv && ok_crc) return ORACLE_GOLDILOCKS;
    if (!ok_dsov0) return ORACLE_UNDER;         // dsov stuck: dip too shallow
    if (ok_crc) return ORACLE_HIT;
    return ORACLE_OVER;
}

void oracle_init(void)
{
    atomic_store_explicit(&s_mode, 0, memory_order_relaxed);
}

void oracle_probe_print(void)
{
    uint32_t v;
    console_printf("smbpbi probe @0x%02x:", GPU_SMB_ADDR);
    if (!pmbus_up()) { console_printf(" bus down\n"); return; }
    // Chip identity: cmd 0x01, empty payload, response bytes 2..
    uint8_t f[1 + SMBPBI_FRAME];
    memset(f, 0, sizeof f);
    f[0] = SMBPBI_FRAME; f[1] = 0x01;
    bool alive = pmbus_raw_write(GPU_SMB_ADDR, f, sizeof f) &&
                 pmbus_raw_read(GPU_SMB_ADDR, f, sizeof f);
    if (!alive) { console_printf(" no ack\n"); }
    else {
        console_printf(" id:");
        for (int i = 2; i < 10; i++) console_printf(" %02x", f[i]);
    }
    if (rd32(R_DSOV, &v)) console_printf("  DSOV=0x%08x", (unsigned)v);
    else console_printf("  reg-read FAILED");
    console_printf("\n");
}

void oracle_set_mode(int mode)
{
    if (mode == 0 || mode == 1)
        atomic_store_explicit(&s_mode, mode, memory_order_relaxed);
}

int oracle_get_mode(void)
{ return atomic_load_explicit(&s_mode, memory_order_relaxed); }

const char *oracle_name(oracle_t v)
{
    switch (v) {
    case ORACLE_GOLDILOCKS: return "goldilocks";
    case ORACLE_HIT:        return "hit";
    case ORACLE_UNDER:      return "under";
    case ORACLE_OVER:       return "over";
    default:                return "lost";
    }
}
