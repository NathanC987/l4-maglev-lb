#ifndef L4MLB_UI_TUI_H
#define L4MLB_UI_TUI_H

#include <stdint.h>

#include "../backend/backend_manager.h"
#include "../backend/table_generator.h"
#include "../forward/datapath.h"
#include "../stats/stats_registry.h"

struct tui_config {
    struct backend_manager *bm;
    struct table_generator *tg;
    struct lb_stats *stats;
    struct datapath *dp; /* for datapath_is_running(), belt-and-suspenders - see tui_run() */
    uint32_t vip;         /* opaque network-byte-order IPv4, display only */
    uint32_t refresh_ms;  /* redraw interval */

    /* Becomes readable the instant the datapath thread has been joined -
     * see stop_fd. Pass -1 to skip this (tui_run() then relies solely on
     * the refresh_ms poll + datapath_is_running() checks, which still
     * work, just with up to one refresh interval of latency). Not owned by
     * the tui: the caller creates and closes it. */
    int stop_fd;
};

struct tui;

struct tui *tui_create(const struct tui_config *cfg);
void tui_destroy(struct tui *t);

/* Blocks running the ncurses dashboard, redrawing every cfg.refresh_ms from
 * stats_registry_snapshot(), backend_manager_snapshot_all(), and
 * table_generator's atomic generation/regen-time accessors - never touches
 * table_generator_get_active() itself, since that snapshot may only safely
 * be read by the single datapath thread (see table_generator.h).
 *
 * Waits for input via poll() on stdin (plus cfg.stop_fd, if given) rather
 * than ncurses' own getch()-with-timeout() mechanism: a blocking getch()
 * call was observed to occasionally not return promptly around the
 * datapath thread handling a shutdown signal (some interaction between
 * ncurses' internal read() and signal delivery to another thread in the
 * same process - not fully root-caused, but poll()'s own EINTR-retry loop
 * here sidesteps it entirely, and getch() is then only ever called
 * non-blocking, after poll() has already confirmed input is waiting).
 *
 * Returns when the user presses 'q'/'Q', or when cfg.stop_fd becomes
 * readable / datapath_is_running(dp) reports false - i.e. the datapath
 * already stopped on its own (e.g. a Ctrl-C consumed by its signalfd).
 * Without either of those noticing, this would otherwise hang forever
 * after a Ctrl-C that this (blocked-signal) thread never sees directly.
 * Always returns 0. */
int tui_run(struct tui *t);

#endif /* L4MLB_UI_TUI_H */
