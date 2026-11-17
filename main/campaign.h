// Cooperative campaign controller. Only campaign_poll(), called by the
// hardware-owner task, may execute an attempt or read the SMBPBI oracle.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "glitch_plan.h"
#include "oracle.h"

typedef struct {
    int vd, td, vr, tr;                       // base attempt (mV/us)
    int pulses, pw, gap;
    int delays[6];
    int ndelays;                              // 1..6
    int batch;                                // 0 = fixed voltage
    int min_v, max_v, step;
    int max_attempts;                         // 0 = unlimited; otherwise global cap
    int restore_wait_ms;                      // settle after the attempt's reset
    int oracle_mode;                          // -1 = current, 0 = SMBPBI, 1 = manual
} campaign_cfg_t;

typedef bool (*campaign_execute_fn)(const glitch_args_t *args,
                                   int *writes_failed, uint32_t *sequence);
// Restore the VRM control path without another PEX_RST pulse.
typedef bool (*campaign_restore_fn)(void);

void campaign_init(campaign_execute_fn execute, campaign_restore_fn restore);
void campaign_poll(void);                     // hardware-owner task only
bool campaign_is_running(void);
bool campaign_cfg_validate(const campaign_cfg_t *cfg, char *error, size_t n);
bool campaign_start(const campaign_cfg_t *cfg); // validates; false if busy/invalid
void campaign_stop(void);                     // cancels before the next attempt

// Only the currently awaiting (session, attempt) may accept a new verdict.
// A retry of the last accepted identical verdict is acknowledged without
// applying it twice. A new campaign invalidates all older tokens.
bool campaign_submit_verdict(uint32_t session, uint32_t attempt, oracle_t verdict);
void campaign_status_json(char *out, int n);
extern const campaign_cfg_t campaign_defaults;
