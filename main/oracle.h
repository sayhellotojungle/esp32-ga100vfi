// Attempt verdict oracle. Primary path: SMBPBI (NVIDIA's "GPU registers over
// SMBus" protocol, block transfers to addr 0x3A on the same two wires as the
// VRM). classify.py's five checks are reproduced bit-for-bit; the campaign
// classifies exactly like the host tools did.
//
// Verdicts follow cli.py: GOLDILOCKS (all checks), HIT (dsov=0, crc=0x55),
// UNDER (dsov still set), OVER (dsov cleared but crc != 0x55), LOST (a
// transaction failed or DSOV is unreadable). Do not reset again to classify.
#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef enum {
    ORACLE_GOLDILOCKS = 0,
    ORACLE_HIT,
    ORACLE_UNDER,
    ORACLE_OVER,
    ORACLE_LOST,
} oracle_t;

// Mode: 0 = SMBPBI, 1 = manual (web button feeds verdicts in).
void oracle_init(void);
void oracle_set_mode(int mode);
int  oracle_get_mode(void);

// Run the five checks after the attempt's settle interval. Every required
// transaction must succeed; *lost is true for any transport failure.
oracle_t oracle_run_smbpbi(bool *lost);

// Pretty name for logs/JSON.
const char *oracle_name(oracle_t v);

// @P: diagnostic read with the existing experimental framing (not a proof
// that this undocumented endpoint implements the assumed register protocol).
void oracle_probe_print(void);
