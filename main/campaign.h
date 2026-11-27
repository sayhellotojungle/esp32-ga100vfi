// Cooperative campaign controller. Only campaign_poll(), called by the
// hardware-owner task, may execute an attempt.
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
    // Both attempt knobs were pinned by campaign.c. They are caller-selectable
    // from 0.5.4: swin>0 is the only physical feedback this bench has (the 0x8b rail
    // samples are what proved the commanded 372 mV pit is really ~70 mV on the rail),
    // and revert=1 is proven to keep 0xE1 ours (e1=0xc2ea where revert=0 leaves
    // 0xc2fa). Its life-extension claim is NOT proven: on 2026-11-25 the same cycle
    // died at shot 8+3 with revert=1 as it had at 8+3 without it.
    int swin;                                 // telemetry sample window us, 0=off
    int revert;                               // 1 = in-timeline VMIN restore
    // No verdict source is configured per run: after the settle interval the
    // campaign always waits for the host (0.4.1 removed the on-board SMBPBI
    // oracle, which this bench cannot reach).
} campaign_cfg_t;

typedef bool (*campaign_execute_fn)(const glitch_args_t *args,
                                   int *writes_failed, uint32_t *sequence);
// Restore the VRM control path without another PEX_RST pulse.
typedef bool (*campaign_restore_fn)(void);

void campaign_init(campaign_execute_fn execute, campaign_restore_fn restore);
void campaign_poll(void);                     // hardware-owner task only
bool campaign_is_running(void);
// The shot a config actually produces. Public because "did my swin/revert reach the
// timeline?" is the only question a caller cannot ask the board once it is running, and
// a key that parses into a config nobody builds a plan from would pass every other test.
void campaign_args_for(const campaign_cfg_t *cfg, int vd, int delay,
                       glitch_args_t *args);

bool campaign_cfg_validate(const campaign_cfg_t *cfg, char *error, size_t n);
bool campaign_start(const campaign_cfg_t *cfg); // validates; false if busy/invalid
void campaign_stop(void);                     // cancels before the next attempt

// Only the currently awaiting (session, attempt) may accept a new verdict.
// A retry of the last accepted identical verdict is acknowledged without
// applying it twice. A new campaign invalidates all older tokens.
bool campaign_submit_verdict(uint32_t session, uint32_t attempt, oracle_t verdict);
void campaign_status_json(char *out, int n);
extern const campaign_cfg_t campaign_defaults;
