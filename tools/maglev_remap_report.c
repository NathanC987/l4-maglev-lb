/* Standalone analysis tool: quantifies Maglev's minimal-disruption property -
 * what fraction of flow-to-backend assignments change when the backend set
 * changes - and contrasts it against naive `hash % N` modulo hashing, which
 * has no such guarantee. Reuses maglev_table_generate() from the real,
 * already-tested implementation directly (src/maglev/maglev_table.c) rather
 * than reimplementing the algorithm, so this can never silently drift from
 * what maglev-lb actually does. Needs no root, no network namespaces, no
 * running maglev-lb - pure in-process computation. See docs/benchmarking.md.
 *
 * Usage:
 *   maglev_remap_report --before N1 --after N2 [--m M]
 *   maglev_remap_report --sweep MIN MAX [--m M]
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../src/maglev/maglev_table.h"

/* Synthesizes N distinct backends (id 0..N-1, fake but distinct IP:port
 * identities) purely so maglev_table_generate()'s per-backend offset/skip
 * hashes differ - the tool only cares about relative placement, not real
 * addresses. Caller frees the returned array. */
static struct backend_view *make_backends(size_t n) {
    struct backend_view *b = calloc(n, sizeof(*b));
    if (b == NULL) {
        return NULL;
    }
    for (size_t i = 0; i < n; i++) {
        b[i].id = (uint32_t)i;
        /* 10.0.(i/256).(i%256) - plenty of distinct addresses for any
         * realistic backend count. */
        uint8_t o3 = (uint8_t)((i / 256) % 256);
        uint8_t o4 = (uint8_t)(i % 256);
        uint32_t addr = (uint32_t)(10 | (0 << 8) | (o3 << 16) | (o4 << 24));
        b[i].addr = addr;
        b[i].port = 9000;
    }
    return b;
}

struct remap_result {
    size_t considered;
    size_t maglev_unchanged;
    size_t naive_unchanged;
};

/* Builds Maglev tables for backend sets [0,n_before) and [0,n_after) and
 * diffs them slot-by-slot (exact, over all M slots - not a sample), plus the
 * equivalent computation for naive `id % n` modulo hashing using each slot
 * index itself as a uniformly-distributed stand-in flow-hash sample (valid
 * because slot indices already range uniformly over [0,M), exactly like
 * flow_hash(key) % M does for real flows). Slots whose "before" owner no
 * longer exists in the "after" set (only possible when n_after < n_before)
 * are excluded from both counts - any algorithm is forced to move those,
 * so counting them would penalize Maglev for something inherent to removal
 * itself rather than to how well it minimizes disruption, matching
 * test/unit/test_maglev_table.c's test_minimal_disruption(). */
static int compute_remap(uint32_t m, size_t n_before, size_t n_after, struct remap_result *out) {
    struct backend_view *before = make_backends(n_before);
    struct backend_view *after = make_backends(n_after);
    if (before == NULL || after == NULL) {
        free(before);
        free(after);
        return -1;
    }

    struct maglev_table *t_before = maglev_table_generate(before, n_before, m);
    struct maglev_table *t_after = maglev_table_generate(after, n_after, m);
    free(before);
    free(after);
    if (t_before == NULL || t_after == NULL) {
        maglev_table_free(t_before);
        maglev_table_free(t_after);
        return -1;
    }

    memset(out, 0, sizeof(*out));
    for (uint32_t i = 0; i < m; i++) {
        int32_t mag_before = t_before->lookup[i];
        int32_t naive_before = (int32_t)(i % n_before);

        if ((size_t)mag_before < n_after) {
            out->considered++; /* same exclusion rule applies to both columns */
            if (t_after->lookup[i] == mag_before) {
                out->maglev_unchanged++;
            }
            int32_t naive_after = (int32_t)(i % n_after);
            if (naive_after == naive_before) {
                out->naive_unchanged++;
            }
        }
    }

    maglev_table_free(t_before);
    maglev_table_free(t_after);
    return 0;
}

static void print_row(size_t n_before, size_t n_after, const struct remap_result *r) {
    double maglev_pct = 100.0 * (double)(r->considered - r->maglev_unchanged) /
                         (double)r->considered;
    double naive_pct = 100.0 * (double)(r->considered - r->naive_unchanged) /
                        (double)r->considered;
    printf("%6zu -> %-6zu  considered=%-8zu  maglev_remapped=%6.2f%%   naive_remapped=%6.2f%%\n",
           n_before, n_after, r->considered, maglev_pct, naive_pct);
}

static void usage(const char *prog) {
    fprintf(stderr,
            "usage: %s --before N1 --after N2 [--m M]\n"
            "       %s --sweep MIN MAX [--m M]\n"
            "\n"
            "  --before/--after  Backend counts before/after the change (ids 0..N-1;\n"
            "                    N2<N1 removes the highest-id backends, N2>N1 adds new ones)\n"
            "  --sweep MIN MAX   For each N in [MIN,MAX], reports removing exactly one\n"
            "                    backend (N -> N-1) - shows Maglev's ~1/N relationship\n"
            "  --m M             Maglev table size (default %u, must be prime)\n",
            prog, prog, MAGLEV_DEFAULT_M);
}

int main(int argc, char **argv) {
    uint32_t m = MAGLEV_DEFAULT_M;
    long before = -1, after = -1;
    long sweep_min = -1, sweep_max = -1;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--before") == 0 && i + 1 < argc) {
            before = strtol(argv[++i], NULL, 10);
        } else if (strcmp(argv[i], "--after") == 0 && i + 1 < argc) {
            after = strtol(argv[++i], NULL, 10);
        } else if (strcmp(argv[i], "--sweep") == 0 && i + 2 < argc) {
            sweep_min = strtol(argv[++i], NULL, 10);
            sweep_max = strtol(argv[++i], NULL, 10);
        } else if (strcmp(argv[i], "--m") == 0 && i + 1 < argc) {
            m = (uint32_t)strtoul(argv[++i], NULL, 10);
        } else {
            usage(argv[0]);
            return 1;
        }
    }

    if (!maglev_is_prime(m)) {
        fprintf(stderr, "warning: --m %u is not prime; results may not reflect real maglev-lb "
                         "behavior (see maglev_is_prime() in src/maglev/maglev_table.c)\n",
                m);
    }

    if (before > 1 && after > 0) {
        struct remap_result r;
        if (compute_remap(m, (size_t)before, (size_t)after, &r) != 0) {
            fprintf(stderr, "compute_remap failed (allocation failure)\n");
            return 1;
        }
        printf("m=%u\n", m);
        print_row((size_t)before, (size_t)after, &r);
        return 0;
    }

    if (sweep_min >= 2 && sweep_max >= sweep_min) {
        printf("m=%u  (each row: N backends, remove exactly one)\n", m);
        for (long n = sweep_min; n <= sweep_max; n++) {
            struct remap_result r;
            if (compute_remap(m, (size_t)n, (size_t)(n - 1), &r) != 0) {
                fprintf(stderr, "compute_remap failed at N=%ld (allocation failure)\n", n);
                return 1;
            }
            print_row((size_t)n, (size_t)(n - 1), &r);
        }
        return 0;
    }

    usage(argv[0]);
    return 1;
}
