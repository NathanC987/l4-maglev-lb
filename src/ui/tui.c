#include "tui.h"

#include <arpa/inet.h>
#include <errno.h>
#include <ncurses.h>
#include <poll.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#define HEALTHY_PAIR 1
#define UNHEALTHY_PAIR 2

struct tui {
    struct tui_config cfg;
};

struct tui *tui_create(const struct tui_config *cfg) {
    struct tui *t = calloc(1, sizeof(*t));
    if (t == NULL) {
        return NULL;
    }
    t->cfg = *cfg;
    return t;
}

void tui_destroy(struct tui *t) {
    free(t);
}

static void format_ip(uint32_t addr, char *out, size_t out_cap) {
    struct in_addr a;
    a.s_addr = addr;
    snprintf(out, out_cap, "%s", inet_ntoa(a));
}

static const char *admin_state_str(enum backend_admin_state s) {
    switch (s) {
    case BACKEND_ENABLED:
        return "enabled";
    case BACKEND_DRAINING:
        return "draining";
    case BACKEND_DISABLED:
        return "disabled";
    }
    return "?";
}

static void draw(struct tui *t) {
    erase();
    int row = 0;

    char vip_str[32];
    format_ip(t->cfg.vip, vip_str, sizeof(vip_str));
    uint64_t generation = table_generator_generation(t->cfg.tg);
    uint64_t last_regen_us = table_generator_last_regen_us(t->cfg.tg);

    attron(A_BOLD);
    mvprintw(row++, 0, "maglev-lb   vip=%s   table generation=%llu   last regen=%llu us", vip_str,
              (unsigned long long)generation, (unsigned long long)last_regen_us);
    attroff(A_BOLD);
    row++;

    struct lb_stats_snapshot snap;
    stats_registry_snapshot(t->cfg.stats, &snap);

    mvprintw(row++, 0, "rx  %12llu pkts  %14llu bytes", (unsigned long long)snap.rx_packets,
              (unsigned long long)snap.rx_bytes);
    mvprintw(row++, 0, "tx  %12llu pkts  %14llu bytes", (unsigned long long)snap.tx_packets,
              (unsigned long long)snap.tx_bytes);
    mvprintw(row++, 0, "conntrack   hits=%llu  misses=%llu  evictions=%llu",
              (unsigned long long)snap.conntrack_hits, (unsigned long long)snap.conntrack_misses,
              (unsigned long long)snap.conntrack_evictions);
    mvprintw(row++, 0, "drops       parse=%llu  no_backend=%llu  unhealthy=%llu  conntrack_full=%llu",
              (unsigned long long)snap.drops_parse_error,
              (unsigned long long)snap.drops_no_backend,
              (unsigned long long)snap.drops_backend_unhealthy,
              (unsigned long long)snap.drops_conntrack_full);
    row++;

    attron(A_BOLD);
    mvprintw(row++, 0, "%-4s %-21s %-9s %-9s %12s %14s %8s %8s", "ID", "ADDRESS", "HEALTH",
              "ADMIN", "PACKETS", "BYTES", "FLOWS", "HC-FAIL");
    attroff(A_BOLD);

    struct backend backends[BACKEND_MAX];
    size_t n = backend_manager_snapshot_all(t->cfg.bm, backends, BACKEND_MAX);
    for (size_t i = 0; i < n; i++) {
        char addr_str[32];
        format_ip(backends[i].addr, addr_str, sizeof(addr_str));
        char addr_port[40];
        snprintf(addr_port, sizeof(addr_port), "%s:%u", addr_str, backends[i].port);

        struct per_backend_stats_snapshot bs;
        stats_registry_snapshot_backend(t->cfg.stats, backends[i].id, &bs);

        bool healthy = atomic_load_explicit(&backends[i].healthy, memory_order_relaxed);
        int pair = healthy ? HEALTHY_PAIR : UNHEALTHY_PAIR;

        attron(COLOR_PAIR(pair));
        mvprintw(row++, 0, "%-4u %-21s %-9s %-9s %12llu %14llu %8llu %8llu", backends[i].id,
                  addr_port, healthy ? "healthy" : "unhealthy",
                  admin_state_str(backends[i].admin_state), (unsigned long long)bs.packets,
                  (unsigned long long)bs.bytes, (unsigned long long)bs.active_flows,
                  (unsigned long long)bs.health_check_failures);
        attroff(COLOR_PAIR(pair));
    }

    row++;
    mvprintw(row++, 0, "press q to quit");

    refresh();
}

int tui_run(struct tui *t) {
    initscr();
    if (has_colors()) {
        start_color();
        init_pair(HEALTHY_PAIR, COLOR_GREEN, COLOR_BLACK);
        init_pair(UNHEALTHY_PAIR, COLOR_RED, COLOR_BLACK);
    }
    noecho();
    curs_set(0);
    cbreak();
    keypad(stdscr, TRUE);
    nodelay(stdscr, TRUE); /* getch() below is only ever called once poll() confirms input */

    int running = 1;
    while (running) {
        draw(t);

        struct pollfd pfds[2];
        pfds[0].fd = STDIN_FILENO;
        pfds[0].events = POLLIN;
        nfds_t n_fds = 1;
        if (t->cfg.stop_fd >= 0) {
            pfds[1].fd = t->cfg.stop_fd;
            pfds[1].events = POLLIN;
            n_fds = 2;
        }

        int pr;
        do {
            pr = poll(pfds, n_fds, (int)t->cfg.refresh_ms);
        } while (pr < 0 && errno == EINTR);

        if (pr > 0 && n_fds == 2 && (pfds[1].revents & POLLIN)) {
            running = 0; /* the datapath thread has stopped and been joined */
            continue;
        }
        if (pr > 0 && (pfds[0].revents & POLLIN)) {
            int ch = getch();
            if (ch == 'q' || ch == 'Q') {
                running = 0;
            }
        }

        /* Belt-and-suspenders even without stop_fd (or if it's somehow
         * never signaled): also fall back to polling the flag directly. */
        if (running && t->cfg.dp != NULL && !datapath_is_running(t->cfg.dp)) {
            running = 0;
        }
    }

    endwin();
    return 0;
}
