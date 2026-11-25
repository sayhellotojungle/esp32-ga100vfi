#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "campaign.h"
#include "oracle.h"
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
/* `@V <session> <attempt> <word>`: a verdict over the serial line, same five words as
 * /api/verdict. */
bool ga_verdict_line(const char *args, uint32_t *session, uint32_t *attempt,
                     oracle_t *verdict, char *error, size_t error_size);
/* `@A [seq=N] [adopted=N] {json}`: the judge host's heartbeat. On success `json` points
 * into `args` and is only valid while the command buffer lives. */
bool ga_agent_frame(const char *args, uint32_t *seq, uint32_t *adopted,
                    const char **json, char *error, size_t error_size);
