#include <arpa/inet.h>
#include <assert.h>
#include <netinet/in.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "backend/backend_manager.h"
#include "backend/table_generator.h"
#include "conntrack/conntrack.h"
#include "stats/stats_registry.h"
#include "ui/metrics_http.h"

#define TEST_PORT 19105

static size_t http_get(uint16_t port, char *out, size_t out_cap) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    assert(fd >= 0);

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
    assert(connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0);

    const char *req = "GET /metrics HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n";
    ssize_t sent = send(fd, req, strlen(req), 0);
    assert(sent == (ssize_t)strlen(req));

    size_t total = 0;
    for (;;) {
        ssize_t n = recv(fd, out + total, out_cap - total - 1, 0);
        if (n <= 0) {
            break;
        }
        total += (size_t)n;
        assert(total < out_cap - 1);
    }
    out[total] = '\0';
    close(fd);
    return total;
}

int main(void) {
    struct backend_manager *bm = backend_manager_create();
    assert(bm != NULL);

    struct in_addr a;
    inet_pton(AF_INET, "10.0.0.11", &a);
    uint32_t addr1 = a.s_addr;
    inet_pton(AF_INET, "10.0.0.12", &a);
    uint32_t addr2 = a.s_addr;

    uint32_t id1 = backend_manager_add(bm, addr1, 9000);
    uint32_t id2 = backend_manager_add(bm, addr2, 9000);
    uint8_t mac[6] = {1, 2, 3, 4, 5, 6};
    backend_manager_set_mac(bm, id1, mac);
    backend_manager_set_mac(bm, id2, mac);
    backend_manager_set_health(bm, id2, false); /* leave backend id2 unhealthy for the test */

    struct table_generator *tg = table_generator_create(bm, 1009);
    assert(tg != NULL);
    table_generator_install_as_subscriber(tg);
    assert(table_generator_rebuild_and_install(tg) == 0);

    struct conntrack_table *ct = conntrack_create(1024);
    assert(ct != NULL);
    struct flow_key fk;
    memset(&fk, 0, sizeof(fk));
    fk.src_port = 1;
    assert(conntrack_insert(ct, &fk, (int32_t)id1, 0) == 0);

    struct lb_stats stats;
    assert(stats_registry_init(&stats, 2) == 0);
    atomic_fetch_add_explicit(&stats.rx_packets, 42, memory_order_relaxed);
    atomic_fetch_add_explicit(&stats.drops_no_backend, 3, memory_order_relaxed);
    stats_record_backend_packet(&stats, id1, 100);
    stats_backend_health_check_failed(&stats, id2);

    struct metrics_http_config cfg = {
        .port = TEST_PORT,
        .bm = bm,
        .tg = tg,
        .ct = ct,
        .stats = &stats,
        .table_size = 1009,
        .configured_backends = 2,
    };
    struct metrics_http *m = metrics_http_create(&cfg);
    assert(m != NULL);
    assert(metrics_http_start(m) == 0);

    static char resp[262144];
    size_t len = http_get(TEST_PORT, resp, sizeof(resp));
    assert(len > 0);

    assert(strstr(resp, "HTTP/1.1 200 OK") != NULL);
    assert(strstr(resp, "Content-Type: text/plain; version=0.0.4") != NULL);

    assert(strstr(resp, "maglev_lb_rx_packets_total 42\n") != NULL);
    assert(strstr(resp, "maglev_lb_drops_total{reason=\"no_backend\"} 3\n") != NULL);
    assert(strstr(resp, "maglev_lb_table_size 1009\n") != NULL);
    assert(strstr(resp, "maglev_lb_backends_configured 2\n") != NULL);
    assert(strstr(resp, "maglev_lb_backends_eligible 1\n") != NULL); /* id2 is unhealthy */
    assert(strstr(resp, "maglev_lb_conntrack_active_flows 1\n") != NULL);

    char expect_up1[128];
    snprintf(expect_up1, sizeof(expect_up1),
             "maglev_lb_backend_up{backend_id=\"%u\",backend_addr=\"10.0.0.11:9000\"} 1\n", id1);
    assert(strstr(resp, expect_up1) != NULL);

    char expect_up2[128];
    snprintf(expect_up2, sizeof(expect_up2),
             "maglev_lb_backend_up{backend_id=\"%u\",backend_addr=\"10.0.0.12:9000\"} 0\n", id2);
    assert(strstr(resp, expect_up2) != NULL);

    char expect_pkts1[160];
    snprintf(expect_pkts1, sizeof(expect_pkts1),
             "maglev_lb_backend_packets_total{backend_id=\"%u\",backend_addr=\"10.0.0.11:9000\"} 1\n",
             id1);
    assert(strstr(resp, expect_pkts1) != NULL);

    char expect_hcfail2[200];
    snprintf(expect_hcfail2, sizeof(expect_hcfail2),
             "maglev_lb_backend_health_check_failures_total{backend_id=\"%u\","
             "backend_addr=\"10.0.0.12:9000\"} 1\n",
             id2);
    assert(strstr(resp, expect_hcfail2) != NULL);

    metrics_http_stop(m);
    metrics_http_destroy(m);
    stats_registry_destroy(&stats);
    conntrack_destroy(ct);
    table_generator_destroy(tg);
    backend_manager_destroy(bm);

    printf("test_metrics_http: all tests passed\n");
    return 0;
}
