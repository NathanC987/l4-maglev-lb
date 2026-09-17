#include "health_checker.h"

#include <errno.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

/* Sentinel epoll_event.data.u64 value identifying the wake_fd (vs. a probe
 * socket, which uses its index into the round's backend array - always
 * < BACKEND_MAX, so never collides with this). */
#define HC_WAKE_SENTINEL UINT64_MAX
#define HC_MAX_EVENTS (BACKEND_MAX + 1)

struct probe_state {
    uint32_t backend_id;
    uint32_t consecutive_ok;
    uint32_t consecutive_fail;
    bool in_use;
};

struct health_checker {
    struct backend_manager *bm;
    struct health_check_cfg cfg;
    struct lb_stats *stats; /* may be NULL - see header */
    pthread_t thread;
    int wake_fd;
    _Atomic bool running; /* true once the thread has actually started */
    _Atomic bool stop_requested;
    /* Persists rise/fall counters across rounds; linear-scanned, fine for
     * BACKEND_MAX (64) entries at one probe round per interval. */
    struct probe_state states[BACKEND_MAX];
};

struct health_checker *health_checker_create(struct backend_manager *bm,
                                              struct health_check_cfg cfg,
                                              struct lb_stats *stats) {
    struct health_checker *hc = calloc(1, sizeof(*hc));
    if (hc == NULL) {
        return NULL;
    }
    hc->bm = bm;
    hc->cfg = cfg;
    hc->stats = stats;
    hc->wake_fd = eventfd(0, EFD_NONBLOCK);
    if (hc->wake_fd < 0) {
        free(hc);
        return NULL;
    }
    atomic_init(&hc->running, false);
    atomic_init(&hc->stop_requested, false);
    return hc;
}

void health_checker_destroy(struct health_checker *hc) {
    if (hc == NULL) {
        return;
    }
    if (hc->wake_fd >= 0) {
        close(hc->wake_fd);
    }
    free(hc);
}

static uint64_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000ULL + (uint64_t)ts.tv_nsec / 1000000ULL;
}

static struct probe_state *find_or_create_state(struct health_checker *hc, uint32_t backend_id) {
    struct probe_state *free_slot = NULL;
    for (size_t i = 0; i < BACKEND_MAX; i++) {
        if (hc->states[i].in_use && hc->states[i].backend_id == backend_id) {
            return &hc->states[i];
        }
        if (!hc->states[i].in_use && free_slot == NULL) {
            free_slot = &hc->states[i];
        }
    }
    if (free_slot != NULL) {
        free_slot->in_use = true;
        free_slot->backend_id = backend_id;
        free_slot->consecutive_ok = 0;
        free_slot->consecutive_fail = 0;
    }
    return free_slot;
}

/* Flips backend_id's health exactly once, at the moment its consecutive
 * ok/fail count first reaches the configured rise/fall threshold - not on
 * every probe past it - so backend_manager_set_health() (and the table
 * regeneration it triggers) isn't invoked redundantly every single round a
 * backend stays in the same state. */
static void record_result(struct health_checker *hc, uint32_t backend_id, bool ok) {
    struct probe_state *st = find_or_create_state(hc, backend_id);
    if (st == NULL) {
        return; /* more distinct backend ids probed than BACKEND_MAX; can't happen in practice */
    }

    if (ok) {
        st->consecutive_fail = 0;
        if (st->consecutive_ok < hc->cfg.rise) {
            st->consecutive_ok++;
            if (st->consecutive_ok == hc->cfg.rise &&
                backend_manager_set_health(hc->bm, backend_id, true)) {
                fprintf(stderr,
                        "health: backend id=%u is now HEALTHY (%u consecutive successful "
                        "probes)\n",
                        backend_id, hc->cfg.rise);
            }
        }
    } else {
        if (hc->stats != NULL) {
            stats_backend_health_check_failed(hc->stats, backend_id);
        }
        st->consecutive_ok = 0;
        if (st->consecutive_fail < hc->cfg.fall) {
            st->consecutive_fail++;
            if (st->consecutive_fail == hc->cfg.fall &&
                backend_manager_set_health(hc->bm, backend_id, false)) {
                fprintf(stderr,
                        "health: backend id=%u is now UNHEALTHY (%u consecutive failed "
                        "probes)\n",
                        backend_id, hc->cfg.fall);
            }
        }
    }
}

static int start_connect(uint32_t addr, uint16_t port) {
    int fd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, 0);
    if (fd < 0) {
        return -1;
    }

    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port = htons(port);
    memcpy(&sa.sin_addr, &addr, sizeof(addr));

    if (connect(fd, (struct sockaddr *)&sa, sizeof(sa)) == 0 || errno == EINPROGRESS) {
        return fd;
    }
    close(fd);
    return -1;
}

/* One full round: fires a non-blocking connect() at every currently known
 * backend concurrently (via epfd, shared with the thread's idle-wait so a
 * shutdown request can interrupt an in-flight round too), waits up to
 * cfg.timeout_ms total for them to resolve, and records a result for each -
 * success, connection failure, or timeout. */
static void run_probe_round(struct health_checker *hc, int epfd) {
    struct backend backends[BACKEND_MAX];
    size_t n = backend_manager_snapshot_all(hc->bm, backends, BACKEND_MAX);
    if (n == 0) {
        return;
    }

    int socks[BACKEND_MAX];
    bool done[BACKEND_MAX];
    size_t pending = 0;

    for (size_t i = 0; i < n; i++) {
        done[i] = false;
        socks[i] = start_connect(backends[i].addr, backends[i].port);
        if (socks[i] < 0) {
            record_result(hc, backends[i].id, false);
            done[i] = true;
            continue;
        }
        struct epoll_event ev;
        memset(&ev, 0, sizeof(ev));
        ev.events = EPOLLOUT;
        ev.data.u64 = (uint64_t)i;
        if (epoll_ctl(epfd, EPOLL_CTL_ADD, socks[i], &ev) != 0) {
            close(socks[i]);
            record_result(hc, backends[i].id, false);
            done[i] = true;
            continue;
        }
        pending++;
    }

    uint64_t deadline = now_ms() + hc->cfg.timeout_ms;
    bool aborted = false;
    while (pending > 0 && !aborted) {
        int64_t remaining = (int64_t)deadline - (int64_t)now_ms();
        if (remaining <= 0) {
            break;
        }
        struct epoll_event evs[HC_MAX_EVENTS];
        int r = epoll_wait(epfd, evs, HC_MAX_EVENTS, (int)remaining);
        if (r < 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        for (int k = 0; k < r; k++) {
            if (evs[k].data.u64 == HC_WAKE_SENTINEL) {
                aborted = true; /* shutdown requested mid-round; abandon in-flight probes */
                break;
            }
            size_t i = (size_t)evs[k].data.u64;
            int err = 0;
            socklen_t elen = sizeof(err);
            getsockopt(socks[i], SOL_SOCKET, SO_ERROR, &err, &elen);
            epoll_ctl(epfd, EPOLL_CTL_DEL, socks[i], NULL);
            close(socks[i]);
            done[i] = true;
            pending--;
            record_result(hc, backends[i].id, err == 0);
        }
    }

    for (size_t i = 0; i < n; i++) {
        if (!done[i]) {
            epoll_ctl(epfd, EPOLL_CTL_DEL, socks[i], NULL);
            close(socks[i]);
            if (!aborted) {
                record_result(hc, backends[i].id, false); /* timed out */
            }
        }
    }
}

static void wait_next_interval(struct health_checker *hc, int epfd) {
    struct epoll_event ev;
    int r = epoll_wait(epfd, &ev, 1, (int)hc->cfg.interval_ms);
    (void)r; /* either an ordinary timeout, or wake_fd fired for shutdown - either
              * way the top-of-loop stop_requested check decides what happens next */
}

static void *thread_main(void *arg) {
    struct health_checker *hc = arg;
    int epfd = epoll_create1(0);
    if (epfd < 0) {
        return NULL;
    }

    struct epoll_event ev;
    memset(&ev, 0, sizeof(ev));
    ev.events = EPOLLIN;
    ev.data.u64 = HC_WAKE_SENTINEL;
    epoll_ctl(epfd, EPOLL_CTL_ADD, hc->wake_fd, &ev);

    while (!atomic_load_explicit(&hc->stop_requested, memory_order_relaxed)) {
        run_probe_round(hc, epfd);
        if (atomic_load_explicit(&hc->stop_requested, memory_order_relaxed)) {
            break;
        }
        wait_next_interval(hc, epfd);
    }

    close(epfd);
    return NULL;
}

int health_checker_start(struct health_checker *hc) {
    if (pthread_create(&hc->thread, NULL, thread_main, hc) != 0) {
        return -1;
    }
    atomic_store(&hc->running, true);
    return 0;
}

void health_checker_stop(struct health_checker *hc) {
    if (hc == NULL || !atomic_load(&hc->running)) {
        return;
    }
    atomic_store_explicit(&hc->stop_requested, true, memory_order_relaxed);
    uint64_t one = 1;
    ssize_t w = write(hc->wake_fd, &one, sizeof(one));
    (void)w;
    pthread_join(hc->thread, NULL);
    atomic_store(&hc->running, false);
}
