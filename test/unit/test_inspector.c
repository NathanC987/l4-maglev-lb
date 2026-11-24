#include <arpa/inet.h>
#include <assert.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "conntrack/conntrack.h"
#include "maglev/hash.h"
#include "maglev/maglev_table.h"
#include "ui/inspector.h"

#define M 1009u
#define SEC 1000000000ULL

static struct routing_snapshot *make_snapshot(size_t n, uint64_t generation) {
    struct backend_view views[3];
    memset(views, 0, sizeof(views));
    for (size_t i = 0; i < n; i++) {
        views[i].id = (uint32_t)i;
        views[i].addr = htonl(0x0a000001u + (uint32_t)i); /* 10.0.0.1, .2, .3 */
        views[i].port = 8080;
    }
    struct routing_snapshot *s = routing_snapshot_create(views, n, views, n, M, generation);
    assert(s != NULL);
    return s;
}

static struct flow_key make_key(uint16_t sport) {
    struct flow_key k;
    memset(&k, 0, sizeof(k));
    k.src_ip = htonl(0x0a630a0au); /* 10.99.10.10 */
    k.dst_ip = htonl(0x0ac8000au);
    k.src_port = htons(sport);
    k.dst_port = htons(8080);
    k.proto = IPPROTO_TCP;
    return k;
}

static char *render(struct inspector *in, int have_gen, uint64_t gen, uint64_t now) {
    char *json = NULL;
    size_t len = inspector_render_json(in, have_gen != 0, gen, now, &json);
    assert(json != NULL && len == strlen(json));
    return json;
}

static int b64val(char c) {
    const char *alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    const char *p = strchr(alphabet, c);
    return p ? (int)(p - alphabet) : -1;
}

/* Decodes the "table" field, checking it against the real lookup table. */
static void check_table(const char *json, const struct maglev_table *t) {
    const char *p = strstr(json, "\"table\":\"");
    assert(p != NULL);
    p += strlen("\"table\":\"");
    uint8_t *out = malloc(t->m + 3);
    assert(out != NULL);
    size_t n = 0;
    while (*p != '"') {
        int v[4];
        for (int i = 0; i < 4; i++) {
            v[i] = p[i] == '=' ? 0 : b64val(p[i]);
            assert(v[i] >= 0);
        }
        uint32_t w = (uint32_t)(v[0] << 18 | v[1] << 12 | v[2] << 6 | v[3]);
        out[n++] = (uint8_t)(w >> 16);
        if (p[2] != '=') out[n++] = (uint8_t)(w >> 8);
        if (p[3] != '=') out[n++] = (uint8_t)w;
        p += 4;
    }
    assert(n == t->m);
    for (uint32_t i = 0; i < t->m; i++) {
        assert(out[i] == (uint8_t)(t->lookup[i] + 1)); /* id + 1; 0 would be an empty slot */
    }
    free(out);
}

static void test_copies_only_while_watched(void) {
    struct inspector *in = inspector_create();
    struct conntrack_table *ct = conntrack_create(64);
    struct routing_snapshot *snap = make_snapshot(2, 1);
    assert(in && ct);

    inspector_on_tick(in, snap, ct, 10 * SEC); /* nobody asked: no copy */
    char *json = render(in, 0, 0, 10 * SEC);
    assert(strcmp(json, "{\"pending\":true}") == 0);
    free(json);

    inspector_want(in, 10 * SEC);
    inspector_on_tick(in, snap, ct, 11 * SEC);
    json = render(in, 0, 0, 11 * SEC);
    assert(strstr(json, "\"generation\":1,") != NULL);
    free(json);

    /* The watch lapses INSPECTOR_WATCH_MS after the last ask. */
    struct flow_key k = make_key(1000);
    assert(conntrack_insert(ct, &k, 0, 20 * SEC) == 0);
    inspector_on_tick(in, snap, ct, 20 * SEC);
    json = render(in, 0, 0, 20 * SEC);
    assert(strstr(json, "\"active\":0") != NULL); /* still the copy from 11 s */
    free(json);

    routing_snapshot_free(snap);
    conntrack_destroy(ct);
    inspector_destroy(in);
}

static void test_table_and_flows(void) {
    struct inspector *in = inspector_create();
    struct conntrack_table *ct = conntrack_create(1024);
    struct routing_snapshot *snap = make_snapshot(3, 4);
    assert(in && ct);

    /* 40 flows on backend 1, seen at 1..40 s; 1 on backend 2. */
    for (uint16_t i = 1; i <= 40; i++) {
        struct flow_key k = make_key((uint16_t)(40000 + i));
        assert(conntrack_insert(ct, &k, 1, i * SEC) == 0);
    }
    struct flow_key lone = make_key(50000);
    assert(conntrack_insert(ct, &lone, 2, 5 * SEC) == 0);

    inspector_want(in, 41 * SEC);
    inspector_on_tick(in, snap, ct, 41 * SEC);
    char *json = render(in, 0, 0, 41 * SEC);

    check_table(json, snap->table);
    assert(strstr(json, "\"m\":1009,") != NULL);
    assert(strstr(json, "{\"id\":0,\"addr\":\"10.0.0.1\",\"port\":8080,\"slots\":") != NULL);
    assert(strstr(json, "\"active\":41,") != NULL);
    assert(strstr(json, "{\"id\":1,\"n\":40}") != NULL);
    assert(strstr(json, "{\"id\":2,\"n\":1}") != NULL);

    /* Slot counts add up to M. */
    unsigned total = 0;
    for (const char *p = json; (p = strstr(p, "\"slots\":")) != NULL; p++) {
        total += (unsigned)strtoul(p + strlen("\"slots\":"), NULL, 10);
    }
    assert(total == M);

    /* Backend 1 keeps only its 16 most recently seen flows: ports 40025..40040. */
    assert(strstr(json, "\"sport\":40040,") != NULL);
    assert(strstr(json, "\"sport\":40025,") != NULL);
    assert(strstr(json, "\"sport\":40024,") == NULL);
    assert(strstr(json, "\"sport\":50000,") != NULL);

    /* Each row names the Maglev slot its 5-tuple hashes to, and its idle time. */
    char expect[96];
    struct flow_key k40 = make_key(40040);
    snprintf(expect, sizeof(expect), "\"id\":1,\"slot\":%u,\"idleMs\":1000}",
             (unsigned)(flow_hash(&k40) % M));
    assert(strstr(json, expect) != NULL);
    free(json);

    /* A reader holding generation 4 gets everything but the table. */
    json = render(in, 1, 4, 41 * SEC);
    assert(strstr(json, "\"table\"") == NULL);
    assert(strstr(json, "\"active\":41,") != NULL);
    free(json);

    routing_snapshot_free(snap);
    conntrack_destroy(ct);
    inspector_destroy(in);
}

static void test_follows_a_new_generation(void) {
    struct inspector *in = inspector_create();
    struct conntrack_table *ct = conntrack_create(16);
    struct routing_snapshot *three = make_snapshot(3, 1);
    struct routing_snapshot *two = make_snapshot(2, 2);
    assert(in && ct);

    inspector_want(in, 0);
    inspector_on_tick(in, three, ct, 1 * SEC);
    inspector_want(in, 2 * SEC);
    inspector_on_tick(in, two, ct, 2 * SEC);
    inspector_want(in, 3 * SEC);
    inspector_on_tick(in, two, ct, 3 * SEC); /* reuses the copy two captures back: must recopy */
    char *json = render(in, 0, 0, 3 * SEC);
    assert(strstr(json, "\"generation\":2,") != NULL);
    assert(strstr(json, "10.0.0.3") == NULL);
    check_table(json, two->table);
    free(json);

    /* Captures are at least INSPECTOR_CAPTURE_MS apart. */
    struct flow_key k = make_key(7);
    assert(conntrack_insert(ct, &k, 0, 3 * SEC) == 0);
    inspector_on_tick(in, two, ct, 3 * SEC + 500000000ULL);
    json = render(in, 0, 0, 3 * SEC);
    assert(strstr(json, "\"active\":0,") != NULL);
    free(json);

    routing_snapshot_free(three);
    routing_snapshot_free(two);
    conntrack_destroy(ct);
    inspector_destroy(in);
}

struct reader_arg {
    struct inspector *in;
    _Atomic int stop;
    int renders;
};

static void *reader(void *p) {
    struct reader_arg *a = p;
    while (!atomic_load(&a->stop)) {
        char *json = NULL;
        inspector_render_json(a->in, 0, 0, 0, &json);
        assert(json != NULL && json[0] == '{');
        free(json);
        a->renders++;
    }
    return NULL;
}

/* The datapath publishes while a reader renders: under TSan this checks the
 * swap, and that a publish skipped for a busy reader loses nothing. */
static void test_concurrent_reader(void) {
    struct inspector *in = inspector_create();
    struct conntrack_table *ct = conntrack_create(4096);
    struct routing_snapshot *snaps[2] = {make_snapshot(3, 1), make_snapshot(2, 2)};
    assert(in && ct);
    struct reader_arg a = {.in = in, .renders = 0};
    atomic_init(&a.stop, 0);
    pthread_t t;
    assert(pthread_create(&t, NULL, reader, &a) == 0);
    for (uint64_t i = 1; i <= 300; i++) {
        struct flow_key k = make_key((uint16_t)i);
        assert(conntrack_insert(ct, &k, (int32_t)(i % 2), i * SEC) == 0);
        inspector_want(in, i * SEC);
        inspector_on_tick(in, snaps[(i / 7) % 2], ct, i * SEC);
    }
    atomic_store(&a.stop, 1);
    pthread_join(t, NULL);
    assert(a.renders > 0);
    routing_snapshot_free(snaps[0]);
    routing_snapshot_free(snaps[1]);
    conntrack_destroy(ct);
    inspector_destroy(in);
}

int main(void) {
    test_copies_only_while_watched();
    test_table_and_flows();
    test_follows_a_new_generation();
    test_concurrent_reader();
    printf("test_inspector: all tests passed\n");
    return 0;
}
