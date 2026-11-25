#include "oracle.h"

// Only the verdict vocabulary lives here since 0.4.1: the board has no judge of
// its own (see oracle.h for why the SMBPBI path was removed), so this module has
// no transport, no mode and nothing to probe. It stays a module of its own
// because the enum is shared by the campaign counters, the HTTP verdict endpoint
// and the console.

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
