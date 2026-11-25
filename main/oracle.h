// Verdict vocabulary. The board cannot judge its own shot: the only verdict
// source is the host, either `host/ga100vfi_agent.py` or a human pressing the
// button on the web page. Both hand the result in through GET /api/verdict with
// the (session, attempt) token of the shot that is currently waiting.
//
// 0.4.1 removed the on-board SMBPBI oracle (the old "mode 0"), which read GPU
// registers as SMBus block transfers to 0x3A on the same two wires as the VRM
// and reproduced classify.py's five checks. That endpoint never answered on
// this bench: `@P` and the direct DSOV read gave no reply four times out of four
// (docs/UNLOCK_AND_POISON.md §2.4.1), so the board cannot see this GPU at all.
// A campaign started in that mode judged every shot LOST through a transport
// that does not exist here, which is worse than not having the mode.
#pragma once

typedef enum {
    ORACLE_GOLDILOCKS = 0,
    ORACLE_HIT,
    ORACLE_UNDER,
    ORACLE_OVER,
    ORACLE_LOST,
} oracle_t;

// Pretty name for logs/JSON.
const char *oracle_name(oracle_t v);
