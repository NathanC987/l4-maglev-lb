#include <arpa/inet.h>
#include <assert.h>
#include <netinet/in.h>
#include <stdio.h>

#include "conntrack/conntrack.h"

static struct flow_key make_key(int variant) {
    struct flow_key k;
    struct in_addr a;
    inet_pton(AF_INET, "10.0.0.5", &a);
    k.src_ip = a.s_addr;
    inet_pton(AF_INET, "10.99.0.100", &a);
    k.dst_ip = a.s_addr;
    k.src_port = (uint16_t)(50000 + variant);
    k.dst_port = htons(80);
    k.proto = IPPROTO_TCP;
    return k;
}

static void test_insert_and_lookup(void) {
    struct conntrack_table *ct = conntrack_create(1024);
    assert(ct != NULL);

    struct flow_key k = make_key(1);
    int32_t out;
    assert(conntrack_lookup(ct, &k, 0, &out) == 0); /* miss on empty table */

    assert(conntrack_insert(ct, &k, 7, 1000) == 0);
    assert(conntrack_lookup(ct, &k, 2000, &out) == 1);
    assert(out == 7);
    assert(conntrack_active_flows(ct) == 1);

    conntrack_destroy(ct);
}

static void test_pool_exhaustion(void) {
    size_t max_flows = 16;
    struct conntrack_table *ct = conntrack_create(max_flows);
    assert(ct != NULL);

    for (size_t i = 0; i < max_flows; i++) {
        struct flow_key k = make_key((int)i);
        assert(conntrack_insert(ct, &k, (int32_t)i, 0) == 0);
    }
    struct flow_key overflow_key = make_key((int)max_flows);
    assert(conntrack_insert(ct, &overflow_key, 99, 0) == -1);

    conntrack_destroy(ct);
}

static void test_reap_expired(void) {
    struct conntrack_table *ct = conntrack_create(64);
    assert(ct != NULL);

    struct flow_key k = make_key(1);
    assert(conntrack_insert(ct, &k, 3, 0) == 0);
    assert(conntrack_active_flows(ct) == 1);

    uint64_t timeout_ns = 1000;
    size_t reaped = conntrack_reap_slice(ct, 10 * timeout_ns, timeout_ns, timeout_ns,
                                          conntrack_bucket_count(ct));
    assert(reaped == 1);
    assert(conntrack_active_flows(ct) == 0);

    int32_t out;
    assert(conntrack_lookup(ct, &k, 20 * timeout_ns, &out) == 0);

    conntrack_destroy(ct);
}

static void test_does_not_reap_fresh_entries(void) {
    struct conntrack_table *ct = conntrack_create(64);
    assert(ct != NULL);

    struct flow_key k = make_key(1);
    assert(conntrack_insert(ct, &k, 3, 1000) == 0);

    size_t reaped = conntrack_reap_slice(ct, 1500, 1000000, 1000000, conntrack_bucket_count(ct));
    assert(reaped == 0);
    assert(conntrack_active_flows(ct) == 1);

    conntrack_destroy(ct);
}

int main(void) {
    test_insert_and_lookup();
    test_pool_exhaustion();
    test_reap_expired();
    test_does_not_reap_fresh_entries();
    printf("test_conntrack: all tests passed\n");
    return 0;
}
