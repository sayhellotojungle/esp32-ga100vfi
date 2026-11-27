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
/* How long a heartbeat counts as "the judge is here". This number lives **here** and
 * nowhere else: the 0.5.6 page keyed its own `age_s <= 5` test, and a second copy of a
 * window on the client is exactly how the two sides drift apart. The agent sends at
 * 1 Hz (`VerdictAgent.PEER_EVERY_S`), which is five times inside this.
 *
 * Why the field needs it at all: the store flag in `peer.c` (`s_present`) latches on the
 * first `@A` and never
 * falls, so before 0.5.7 a board that had lost its judge kept answering
 * `{"present":true,"age_s":6}` -- and the page, reading `present`, reported an agent
 * that had been dead for minutes as 在线. Measured on the bench 2026-11-26 right after
 * the heartbeat landed: kill the process, and six seconds later presence was still
 * true. Presence is a claim about elapsed time, so it has to be computed from the age,
 * not latched. The snapshot itself (`seq`/`info`) stays latched: a judge that died is
 * precisely the moment its `halt` reason matters. */
#define PEER_STALE_US   INT64_C(5000000)    /* 5 s without `@A`: the judge is gone     */

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
