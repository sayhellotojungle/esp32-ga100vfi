#pragma once
#include <stdbool.h>
#include <stdint.h>

/* One page of paper between the board and the machine that judges its shots.
 *
 * The agent is the only thing allowed to fire a shot (it is the only side that can
 * read BAR0), and it is a process a human starts when needed rather than a service,
 * so neither side may ever wait on the other: the agent pushes a snapshot with `@A`
 * over the serial line, the web page leaves a start request with `GET /api/req`, and
 * each side reads the other's slot whenever it next talks to the board. Two fixed RAM
 * buffers under a spinlock: no flash, no queue, no allocation, and nothing here can
 * hold the executor up while a shot is in flight.
 *
 * An unclaimed request expires (PEER_REQ_TTL_US). That rule exists because the agent
 * is on-demand: a start clicked while no agent runs must not fire ten minutes later
 * when somebody finally plugs the cable in.
 */

#define PEER_INFO_MAX   256                 /* agent -> page: one ASCII JSON object */
#define PEER_REQ_MAX    192                 /* page -> agent: the request verbatim  */
#define PEER_REQ_TTL_US INT64_C(10000000)   /* 10 s: after that the click is void   */

void peer_init(void);

/* Agent -> board (serial `@A`). The blob must be one balanced ASCII JSON object: a
 * truncated line would otherwise render as a page that lies about the bench. `error`
 * receives the reason when this returns false; it may be NULL. */
bool peer_store(const char *json, uint32_t seq, char *error, int n);

/* The `peer` block of /api/status: presence, snapshot age, and the state of the
 * page's start request (pending / adopted / expired). */
void peer_render(char *out, int n);

/* Page -> board (`GET /api/req`). The body is stored verbatim for the agent to parse;
 * the board does not read campaign parameters, so it must not mis-type them either.
 * A second request replaces the first: the newest click is what the human meant. */
bool peer_request_post(const char *body, uint32_t seq, char *error, int n);

/* Page -> board: withdraw a request that has not been claimed yet. */
bool peer_request_cancel(void);

/* The pending request for the `@A` reply, if one is still in force. */
bool peer_pending_request(char *out, int n, uint32_t *seq);

/* The agent says it started this request: the slot clears, and the page learns that
 * its click became a run rather than a timeout. */
void peer_adopted(uint32_t seq);
