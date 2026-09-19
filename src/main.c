#include <arpa/inet.h>
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/eventfd.h>
#include <unistd.h>

#include "backend/backend_manager.h"
#include "backend/health_checker.h"
#include "backend/table_generator.h"
#include "config/config.h"
#include "conntrack/conntrack.h"
#include "forward/datapath.h"
#include "maglev/maglev_table.h"
#include "net/arp_resolve.h"
#include "net/eth.h"
#include "net/raw_socket.h"
#include "stats/stats_registry.h"
#include "ui/metrics_http.h"
#include "ui/tui.h"

#define ARP_TIMEOUT_MS 2000
#define REAPER_INTERVAL_MS 500
#define TCP_IDLE_TIMEOUT_NS (120ULL * 1000000000ULL)
#define UDP_IDLE_TIMEOUT_NS (45ULL * 1000000000ULL)
#define TUI_REFRESH_MS 1000

static void print_ip(const char *label, uint32_t addr_network_order) {
    struct in_addr a;
    a.s_addr = addr_network_order;
    fprintf(stderr, "%s%s", label, inet_ntoa(a));
}

struct datapath_thread_ctx {
    struct datapath *dp;
    int rc;
};

static void *datapath_thread_main(void *arg) {
    struct datapath_thread_ctx *ctx = arg;
    ctx->rc = datapath_run(ctx->dp);
    return NULL;
}

struct joiner_thread_ctx {
    pthread_t dp_thread;
    int stop_fd; /* written to once dp_thread has been joined */
};

static void *joiner_thread_main(void *arg) {
    struct joiner_thread_ctx *ctx = arg;
    pthread_join(ctx->dp_thread, NULL);
    uint64_t one = 1;
    ssize_t w = write(ctx->stop_fd, &one, sizeof(one));
    (void)w;
    return NULL;
}

/* Runs the datapath on its own thread while the TUI (which wants the main
 * thread for terminal I/O) runs here. Returns datapath_run()'s exit code. */
static int run_with_tui(struct datapath *dp, struct backend_manager *bm,
                         struct table_generator *tg, struct lb_stats *stats, uint32_t vip) {
    /* SIGINT/SIGTERM are already blocked for this thread - see main(), which
     * blocks them before spawning *any* thread (including health_checker's),
     * specifically so every thread in the process inherits the block and
     * only the datapath thread's signalfd ever consumes the signal. Blocking
     * it only here, right before spawning the datapath thread, was tried
     * first and found insufficient: health_checker_start() (called earlier
     * in main(), for both --tui and headless runs) had already spawned its
     * thread with an *unblocked* mask by that point, so a process-directed
     * SIGTERM could still be delivered to it and terminate the process via
     * that thread's default disposition instead of the clean shutdown path -
     * intermittently, depending on which thread the kernel happened to pick. */
    struct datapath_thread_ctx ctx = {.dp = dp, .rc = 1};
    pthread_t dp_thread;
    if (pthread_create(&dp_thread, NULL, datapath_thread_main, &ctx) != 0) {
        fprintf(stderr, "main: failed to start the datapath thread\n");
        return 1;
    }

    /* A dedicated joiner thread blocks on pthread_join(dp_thread) and signals
     * stop_fd the instant it returns, so tui_run() can wait on that via
     * poll() alongside stdin instead of relying solely on a periodic
     * datapath_is_running() poll (which still works as a fallback - see
     * tui.c - but reacts only once per refresh tick). */
    int stop_fd = eventfd(0, EFD_NONBLOCK);
    struct joiner_thread_ctx jctx = {.dp_thread = dp_thread, .stop_fd = stop_fd};
    pthread_t joiner_thread;
    bool have_joiner =
        stop_fd >= 0 && pthread_create(&joiner_thread, NULL, joiner_thread_main, &jctx) == 0;

    struct tui_config tui_cfg = {
        .bm = bm,
        .tg = tg,
        .stats = stats,
        .dp = dp,
        .vip = vip,
        .refresh_ms = TUI_REFRESH_MS,
        .stop_fd = have_joiner ? stop_fd : -1,
    };
    struct tui *tui = tui_create(&tui_cfg);
    if (tui != NULL) {
        tui_run(tui);
        tui_destroy(tui);
    } else {
        fprintf(stderr,
                "main: tui_create failed; waiting for the datapath to stop on its own\n");
    }

    /* The TUI returns either because the user pressed q, or because the
     * datapath already stopped by itself - only ask it to stop in the
     * former case. pthread_kill on a thread that has already exited but
     * not yet been joined is still valid (it just hasn't been reaped), so
     * this is safe either way even with the inherent check-then-act race
     * against the datapath stopping on its own right after the check. */
    if (datapath_is_running(dp)) {
        pthread_kill(dp_thread, SIGTERM);
    }
    if (have_joiner) {
        pthread_join(joiner_thread, NULL); /* this itself joins dp_thread */
    } else {
        pthread_join(dp_thread, NULL);
    }
    if (stop_fd >= 0) {
        close(stop_fd);
    }
    return ctx.rc;
}

int main(int argc, char **argv) {
    struct config cfg;
    int rc = config_parse_args(argc, argv, &cfg);
    if (rc == 1) {
        return 0;
    }
    if (rc != 0) {
        return 1;
    }

    if (!maglev_is_prime(cfg.table_size)) {
        fprintf(stderr, "main: --table-size %u is not prime; refusing to start "
                         "(primality guarantees every backend's permutation is a full "
                         "cycle over the table)\n",
                cfg.table_size);
        return 1;
    }

    /* Block SIGINT/SIGTERM for the main thread now, before any other thread
     * is spawned (health_checker_start() below, and - in --tui mode - the
     * datapath/joiner threads created later) - every thread this process
     * ever creates inherits its creating thread's signal mask, so blocking
     * here guarantees none of them can be handed the signal via its default
     * (process-terminating) disposition. Only the datapath thread's own
     * signalfd (set up inside datapath_run()) ever actually consumes it,
     * giving one single, deterministic clean-shutdown path regardless of
     * how many other threads exist. Blocking this later - e.g. only
     * immediately before spawning the datapath thread - is NOT equivalent:
     * a signal sent after health_checker_start() but before that point could
     * still land on its thread and kill the process via default disposition
     * instead, intermittently, depending on which thread the kernel picks. */
    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGINT);
    sigaddset(&mask, SIGTERM);
    if (pthread_sigmask(SIG_BLOCK, &mask, NULL) != 0) {
        fprintf(stderr, "main: pthread_sigmask failed: %s\n", strerror(errno));
        return 1;
    }

    uint8_t iface_mac[ETH_ADDR_LEN];
    if (raw_socket_get_mac(cfg.iface, iface_mac) != 0) {
        fprintf(stderr, "main: failed to read MAC address of %s\n", cfg.iface);
        return 1;
    }

    struct backend_manager *bm = NULL;
    struct table_generator *tg = NULL;
    struct conntrack_table *ct = NULL;
    struct lb_stats stats;
    bool stats_initialized = false;
    struct health_checker *hc = NULL;
    struct metrics_http *mh = NULL;
    struct datapath *dp = NULL;
    int run_rc = 1;

    bm = backend_manager_create();
    if (bm == NULL) {
        fprintf(stderr, "main: backend_manager_create failed\n");
        goto cleanup;
    }

    fprintf(stderr, "maglev-lb: iface=%s table_size=%u max_flows=%zu ttl=%u\n", cfg.iface,
            cfg.table_size, cfg.max_flows, cfg.ttl);
    print_ip("maglev-lb: vip=", cfg.vip);
    print_ip(" director_ip=", cfg.director_ip);
    fprintf(stderr, "\n");

    for (size_t i = 0; i < cfg.n_backends; i++) {
        uint32_t id = backend_manager_add(bm, cfg.backends[i].addr, cfg.backends[i].port);
        if (id == UINT32_MAX) {
            fprintf(stderr, "main: backend table full, cannot add backend %zu\n", i);
            goto cleanup;
        }

        uint8_t mac[ETH_ADDR_LEN];
        if (arp_resolve(cfg.iface, cfg.backends[i].addr, iface_mac, cfg.director_ip, mac,
                         ARP_TIMEOUT_MS) != 0) {
            print_ip("main: ARP resolution failed for backend ", cfg.backends[i].addr);
            fprintf(stderr, "\n");
            goto cleanup;
        }
        backend_manager_set_mac(bm, id, mac);

        print_ip("maglev-lb: backend ", cfg.backends[i].addr);
        fprintf(stderr, " resolved to %02x:%02x:%02x:%02x:%02x:%02x (id=%u)\n", mac[0], mac[1],
                mac[2], mac[3], mac[4], mac[5], id);
    }

    tg = table_generator_create(bm, cfg.table_size);
    if (tg == NULL) {
        fprintf(stderr, "main: table_generator_create failed\n");
        goto cleanup;
    }
    table_generator_install_as_subscriber(tg);
    if (table_generator_rebuild_and_install(tg) != 0) {
        fprintf(stderr, "main: initial Maglev table build failed\n");
        goto cleanup;
    }

    ct = conntrack_create(cfg.max_flows);
    if (ct == NULL) {
        fprintf(stderr, "main: conntrack_create failed\n");
        goto cleanup;
    }

    if (stats_registry_init(&stats, cfg.n_backends) != 0) {
        fprintf(stderr, "main: stats_registry_init failed\n");
        goto cleanup;
    }
    stats_initialized = true;

    {
        struct health_check_cfg hc_cfg = {
            .interval_ms = cfg.hc_interval_ms,
            .timeout_ms = cfg.hc_timeout_ms,
            .rise = cfg.hc_rise,
            .fall = cfg.hc_fall,
        };
        strncpy(hc_cfg.admin_fifo, cfg.admin_fifo, sizeof(hc_cfg.admin_fifo) - 1);
        hc = health_checker_create(bm, hc_cfg, &stats);
        if (hc == NULL) {
            fprintf(stderr, "main: health_checker_create failed; continuing without health "
                             "checking (backends keep their last known health state)\n");
        } else if (health_checker_start(hc) != 0) {
            fprintf(stderr, "main: health_checker_start failed; continuing without health "
                             "checking (backends keep their last known health state)\n");
        }
    }

    if (cfg.metrics_port != 0) {
        struct metrics_http_config mh_cfg = {
            .port = cfg.metrics_port,
            .bm = bm,
            .tg = tg,
            .ct = ct,
            .stats = &stats,
            .table_size = cfg.table_size,
            .configured_backends = cfg.n_backends,
        };
        mh = metrics_http_create(&mh_cfg);
        if (mh == NULL) {
            fprintf(stderr,
                    "main: metrics_http_create failed; continuing without the Prometheus "
                    "exporter\n");
        } else if (metrics_http_start(mh) != 0) {
            fprintf(stderr,
                    "main: metrics_http_start failed (port %u in use?); continuing without "
                    "the Prometheus exporter\n",
                    cfg.metrics_port);
            metrics_http_destroy(mh);
            mh = NULL;
        } else {
            fprintf(stderr, "maglev-lb: Prometheus metrics on :%u/metrics\n", cfg.metrics_port);
        }
    }

    {
        struct datapath_config dp_cfg = {
            .ifname = cfg.iface,
            .vip = cfg.vip,
            .director_ip = cfg.director_ip,
            .ttl = cfg.ttl,
            .tg = tg,
            .ct = ct,
            .stats = &stats,
            .reaper_interval_ms = REAPER_INTERVAL_MS,
            .tcp_timeout_ns = TCP_IDLE_TIMEOUT_NS,
            .udp_timeout_ns = UDP_IDLE_TIMEOUT_NS,
        };
        dp = datapath_create(&dp_cfg);
    }
    if (dp == NULL) {
        fprintf(stderr, "main: datapath_create failed\n");
        goto cleanup;
    }

    if (cfg.tui_enabled) {
        run_rc = run_with_tui(dp, bm, tg, &stats, cfg.vip);
    } else {
        fprintf(stderr, "maglev-lb: starting datapath, press Ctrl-C to stop\n");
        run_rc = datapath_run(dp);
    }

cleanup:
    datapath_destroy(dp);
    /* Stop the metrics thread before touching anything it might still be
     * reading mid-request (stats/ct/tg/bm) - see main.c's ordering note on
     * table_generator_reclaim for the same principle applied to the
     * datapath thread. */
    metrics_http_stop(mh);
    metrics_http_destroy(mh);
    if (stats_initialized) {
        stats_registry_destroy(&stats);
    }
    conntrack_destroy(ct);
    health_checker_stop(hc);
    health_checker_destroy(hc);
    table_generator_destroy(tg);
    backend_manager_destroy(bm);

    return run_rc == 0 ? 0 : 1;
}
