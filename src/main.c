#include <arpa/inet.h>
#include <stdio.h>
#include <stdlib.h>

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

#define ARP_TIMEOUT_MS 2000
#define REAPER_INTERVAL_MS 500
#define TCP_IDLE_TIMEOUT_NS (120ULL * 1000000000ULL)
#define UDP_IDLE_TIMEOUT_NS (45ULL * 1000000000ULL)

static void print_ip(const char *label, uint32_t addr_network_order) {
    struct in_addr a;
    a.s_addr = addr_network_order;
    fprintf(stderr, "%s%s", label, inet_ntoa(a));
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

    uint8_t iface_mac[ETH_ADDR_LEN];
    if (raw_socket_get_mac(cfg.iface, iface_mac) != 0) {
        fprintf(stderr, "main: failed to read MAC address of %s\n", cfg.iface);
        return 1;
    }

    struct backend_manager *bm = backend_manager_create();
    if (bm == NULL) {
        fprintf(stderr, "main: backend_manager_create failed\n");
        return 1;
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
            backend_manager_destroy(bm);
            return 1;
        }

        uint8_t mac[ETH_ADDR_LEN];
        if (arp_resolve(cfg.iface, cfg.backends[i].addr, iface_mac, cfg.director_ip, mac,
                         ARP_TIMEOUT_MS) != 0) {
            print_ip("main: ARP resolution failed for backend ", cfg.backends[i].addr);
            fprintf(stderr, "\n");
            backend_manager_destroy(bm);
            return 1;
        }
        backend_manager_set_mac(bm, id, mac);

        print_ip("maglev-lb: backend ", cfg.backends[i].addr);
        fprintf(stderr, " resolved to %02x:%02x:%02x:%02x:%02x:%02x (id=%u)\n", mac[0], mac[1],
                mac[2], mac[3], mac[4], mac[5], id);
    }

    struct table_generator *tg = table_generator_create(bm, cfg.table_size);
    if (tg == NULL) {
        fprintf(stderr, "main: table_generator_create failed\n");
        backend_manager_destroy(bm);
        return 1;
    }
    table_generator_install_as_subscriber(tg);
    if (table_generator_rebuild_and_install(tg) != 0) {
        fprintf(stderr, "main: initial Maglev table build failed\n");
        table_generator_destroy(tg);
        backend_manager_destroy(bm);
        return 1;
    }

    /* M1: no active health checker. Interface exists and is exercised
     * (create/start/stop) so M2 can implement it behind this same shape
     * without touching main.c's wiring. */
    struct health_check_cfg hc_cfg = {
        .interval_ms = 1000, .timeout_ms = 500, .rise = 2, .fall = 3};
    struct health_checker *hc = health_checker_create(bm, hc_cfg);
    if (hc != NULL) {
        health_checker_start(hc);
    }

    int run_rc = 1; /* overwritten below only if datapath_run actually executes */

    struct conntrack_table *ct = conntrack_create(cfg.max_flows);
    if (ct == NULL) {
        fprintf(stderr, "main: conntrack_create failed\n");
        goto cleanup_hc;
    }

    struct lb_stats stats;
    if (stats_registry_init(&stats, cfg.n_backends) != 0) {
        fprintf(stderr, "main: stats_registry_init failed\n");
        goto cleanup_ct;
    }

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
    struct datapath *dp = datapath_create(&dp_cfg);
    if (dp == NULL) {
        fprintf(stderr, "main: datapath_create failed\n");
        stats_registry_destroy(&stats);
        goto cleanup_ct;
    }

    fprintf(stderr, "maglev-lb: starting datapath, press Ctrl-C to stop\n");
    run_rc = datapath_run(dp);

    datapath_destroy(dp);
    stats_registry_destroy(&stats);
cleanup_ct:
    conntrack_destroy(ct);
cleanup_hc:
    health_checker_stop(hc);
    health_checker_destroy(hc);
    table_generator_destroy(tg);
    backend_manager_destroy(bm);

    return run_rc == 0 ? 0 : 1;
}
