#pragma once
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include "protocol.h"
void console_init(void);
int console_printf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
/* Atomic whole command submission; false means rejected or queue full. */
bool console_input_push(const char *buf, int len);
/* Positive length, zero on timeout, -1 for invalid output buffer. */
int console_read_command(char *out, size_t capacity, int timeout_ms);
uint32_t console_log_head(void);
/* Resync stale/future cursors; returns actual bytes copied. */
int console_log_read(uint32_t *pos, char *out, int max);
