#include <assert.h>
#include <stdio.h>

#include "stats/stats_registry.h"

int main(void) {
    struct lb_stats s;
    assert(stats_registry_init(&s, 4) == 0);

    atomic_fetch_add_explicit(&s.rx_packets, 5, memory_order_relaxed);
    atomic_fetch_add_explicit(&s.tx_packets, 3, memory_order_relaxed);
    stats_record_backend_packet(&s, 1, 100);
    stats_record_backend_packet(&s, 1, 50);
    stats_record_backend_packet(&s, 99, 10); /* out of range: must be a no-op, not a crash */

    struct lb_stats_snapshot snap;
    stats_registry_snapshot(&s, &snap);
    assert(snap.rx_packets == 5);
    assert(snap.tx_packets == 3);

    struct per_backend_stats_snapshot b1;
    stats_registry_snapshot_backend(&s, 1, &b1);
    assert(b1.packets == 2);
    assert(b1.bytes == 150);

    struct per_backend_stats_snapshot b_oob;
    stats_registry_snapshot_backend(&s, 99, &b_oob);
    assert(b_oob.packets == 0);

    stats_registry_destroy(&s);
    printf("test_stats_registry: all tests passed\n");
    return 0;
}
