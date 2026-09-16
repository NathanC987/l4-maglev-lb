#include <arpa/inet.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "maglev/maglev_table.h"

static uint32_t ip(const char *s) {
    struct in_addr a;
    int rc = inet_pton(AF_INET, s, &a);
    assert(rc == 1);
    return a.s_addr;
}

static void test_is_prime(void) {
    assert(maglev_is_prime(2));
    assert(maglev_is_prime(3));
    assert(maglev_is_prime(5));
    assert(maglev_is_prime(65537));
    assert(!maglev_is_prime(0));
    assert(!maglev_is_prime(1));
    assert(!maglev_is_prime(4));
    assert(!maglev_is_prime(65536));
    assert(!maglev_is_prime(9));
}

static void test_empty_backend_set(void) {
    struct maglev_table *t = maglev_table_generate(NULL, 0, 1009);
    assert(t != NULL);
    for (uint32_t i = 0; i < t->m; i++) {
        assert(t->lookup[i] == -1);
    }
    struct flow_key k;
    memset(&k, 0, sizeof(k));
    assert(maglev_table_lookup(t, &k) == -1);
    maglev_table_free(t);
}

static void test_full_coverage_and_balance(void) {
    struct backend_view backends[2] = {
        {.id = 0, .addr = ip("10.99.0.11"), .port = 9000, .mac = {0}},
        {.id = 1, .addr = ip("10.99.0.12"), .port = 9000, .mac = {0}},
    };
    uint32_t m = 65537;
    struct maglev_table *t = maglev_table_generate(backends, 2, m);
    assert(t != NULL);

    uint32_t counts[2] = {0, 0};
    for (uint32_t i = 0; i < m; i++) {
        int32_t b = t->lookup[i];
        assert(b == 0 || b == 1);
        counts[b]++;
    }
    double ratio = (double)counts[0] / (double)m;
    assert(ratio > 0.45 && ratio < 0.55);

    maglev_table_free(t);
}

static void test_deterministic_and_order_independent(void) {
    struct backend_view a_first[2] = {
        {.id = 5, .addr = ip("10.99.0.11"), .port = 9000, .mac = {0}},
        {.id = 7, .addr = ip("10.99.0.12"), .port = 9000, .mac = {0}},
    };
    struct backend_view b_first[2] = {
        {.id = 7, .addr = ip("10.99.0.12"), .port = 9000, .mac = {0}},
        {.id = 5, .addr = ip("10.99.0.11"), .port = 9000, .mac = {0}},
    };
    uint32_t m = 1009;
    struct maglev_table *t1 = maglev_table_generate(a_first, 2, m);
    struct maglev_table *t2 = maglev_table_generate(b_first, 2, m);
    assert(t1 != NULL && t2 != NULL);
    /* Each backend's permutation depends only on its own identity string
     * (ip:port), not its position in the input array, so the resulting
     * table must be identical regardless of input order. */
    assert(memcmp(t1->lookup, t2->lookup, sizeof(int32_t) * m) == 0);
    maglev_table_free(t1);
    maglev_table_free(t2);
}

static void test_minimal_disruption(void) {
    struct backend_view three[3] = {
        {.id = 0, .addr = ip("10.99.0.11"), .port = 9000, .mac = {0}},
        {.id = 1, .addr = ip("10.99.0.12"), .port = 9000, .mac = {0}},
        {.id = 2, .addr = ip("10.99.0.13"), .port = 9000, .mac = {0}},
    };
    struct backend_view two[2] = {three[0], three[2]};
    uint32_t m = 65537;
    struct maglev_table *before = maglev_table_generate(three, 3, m);
    struct maglev_table *after = maglev_table_generate(two, 2, m);
    assert(before != NULL && after != NULL);

    size_t considered = 0;
    size_t unchanged = 0;
    for (uint32_t i = 0; i < m; i++) {
        if (before->lookup[i] == 1) {
            continue; /* backend 1 was removed; its old slots necessarily move */
        }
        considered++;
        if (before->lookup[i] == after->lookup[i]) {
            unchanged++;
        }
    }
    /* Maglev's whole point: removing one backend should barely disturb the
     * slots that already belonged to the surviving backends. */
    double stability = (double)unchanged / (double)considered;
    assert(stability > 0.95);

    maglev_table_free(before);
    maglev_table_free(after);
}

static void test_lookup_determinism(void) {
    struct backend_view backends[2] = {
        {.id = 0, .addr = ip("10.99.0.11"), .port = 9000, .mac = {0}},
        {.id = 1, .addr = ip("10.99.0.12"), .port = 9000, .mac = {0}},
    };
    struct maglev_table *t = maglev_table_generate(backends, 2, 65537);
    assert(t != NULL);

    struct flow_key k;
    k.src_ip = ip("10.0.0.5");
    k.dst_ip = ip("10.99.0.100");
    k.src_port = htons(51234);
    k.dst_port = htons(80);
    k.proto = 6;

    int32_t first = maglev_table_lookup(t, &k);
    assert(first == 0 || first == 1);
    for (int i = 0; i < 100; i++) {
        assert(maglev_table_lookup(t, &k) == first);
    }

    maglev_table_free(t);
}

int main(void) {
    test_is_prime();
    test_empty_backend_set();
    test_full_coverage_and_balance();
    test_deterministic_and_order_independent();
    test_minimal_disruption();
    test_lookup_determinism();
    printf("test_maglev_table: all tests passed\n");
    return 0;
}
