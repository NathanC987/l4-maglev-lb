#ifndef L4MLB_CONNTRACK_REAPER_H
#define L4MLB_CONNTRACK_REAPER_H

#include <stddef.h>
#include <stdint.h>

#include "conntrack.h"

/* Creates a timerfd (CLOCK_MONOTONIC, non-blocking) that fires every
 * interval_ms, suitable for registering directly in an epoll set. Returns
 * the fd, or -1 on error. */
int reaper_timerfd_create(uint32_t interval_ms);

/* Call when timerfd becomes readable: drains its expiration count and runs
 * one bounded sweep tick over ct. buckets_per_tick bounds the work done per
 * call so a full-table sweep is spread across many ticks instead of causing
 * a latency spike. Returns the number of entries reaped this tick, so the
 * caller can fold it into stats.conntrack_evictions. */
size_t reaper_on_timer_fired(int timerfd, struct conntrack_table *ct, uint64_t now_ns,
                              uint64_t tcp_timeout_ns, uint64_t udp_timeout_ns,
                              size_t buckets_per_tick);

#endif /* L4MLB_CONNTRACK_REAPER_H */
