#pragma once
#include <stdint.h>
typedef uint32_t TickType_t;
#define portMAX_DELAY UINT32_MAX

/* peer.c keeps its two slots under a spinlock; the offline model has one thread, so the
 * lock is a no-op that still has to type-check. */
typedef int portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(mux) ((void)(mux))
#define portEXIT_CRITICAL(mux) ((void)(mux))
