#pragma once
#include <stdbool.h>
#include <stddef.h>
#include "campaign.h"
#define GA_COMMAND_MAX 256
typedef struct { char line[GA_COMMAND_MAX]; size_t used; bool dropping; } ga_uart_parser_t;
/* 1: complete command; 0: more input/whitespace; -1: rejected line. */
int ga_uart_feed(ga_uart_parser_t *p, unsigned char ch, char out[GA_COMMAND_MAX]);
/* Exactly one complete command; no silent truncation. */
bool ga_command_normalize(const char *src, size_t len, char out[GA_COMMAND_MAX]);
bool ga_parse_int(const char *text, int *out);
bool ga_parse_delays(const char *text, int *out, int *count, int capacity);
/* cfg is changed only when all key=value options parse successfully. */
bool ga_campaign_options(const char *args, campaign_cfg_t *cfg, char *error, size_t error_size);
