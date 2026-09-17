#include "config.h"

#include <arpa/inet.h>
#include <getopt.h>
#include <stdlib.h>
#include <string.h>

static int parse_ip(const char *s, uint32_t *out) {
    struct in_addr a;
    if (inet_pton(AF_INET, s, &a) != 1) {
        return -1;
    }
    *out = a.s_addr;
    return 0;
}

static int parse_backend_spec(const char *spec, struct config_backend *out) {
    const char *colon = strrchr(spec, ':');
    if (colon == NULL) {
        return -1;
    }

    char ipbuf[64];
    size_t ip_len = (size_t)(colon - spec);
    if (ip_len == 0 || ip_len >= sizeof(ipbuf)) {
        return -1;
    }
    memcpy(ipbuf, spec, ip_len);
    ipbuf[ip_len] = '\0';

    uint32_t ip;
    if (parse_ip(ipbuf, &ip) != 0) {
        return -1;
    }

    char *end = NULL;
    long port = strtol(colon + 1, &end, 10);
    if (end == colon + 1 || *end != '\0' || port <= 0 || port > 65535) {
        return -1;
    }

    out->addr = ip;
    out->port = (uint16_t)port;
    return 0;
}

void config_print_usage(const char *prog, FILE *stream) {
    fprintf(stream,
            "Usage: %s --iface IFACE --vip VIP_IP --director-ip DIRECTOR_IP\n"
            "          --backend IP:PORT [--backend IP:PORT ...]\n"
            "          [--ttl TTL] [--table-size M] [--max-flows N]\n"
            "          [--hc-interval-ms MS] [--hc-timeout-ms MS]\n"
            "          [--hc-rise N] [--hc-fall N] [--tui] [--metrics-port PORT]\n"
            "\n"
            "  --iface        Interface the LB listens/sends on (single shared L2\n"
            "                 segment in v1's netns topology: client- and backend-facing)\n"
            "  --vip          Virtual IP clients connect to\n"
            "  --director-ip  This host's own IP, used as the GRE outer source\n"
            "  --backend      Backend real IP:port; repeat for multiple backends\n"
            "                 (port is the health-check TCP-connect target, not used\n"
            "                 for datapath forwarding, which is pure DSR passthrough)\n"
            "  --ttl          Outer IP TTL for GRE-encapsulated packets (default 64)\n"
            "  --table-size   Maglev lookup table size M, must be prime (default 65537)\n"
            "  --max-flows    Conntrack table capacity (default 1000000)\n"
            "  --hc-interval-ms  Health-check probe interval (default 1000)\n"
            "  --hc-timeout-ms   Health-check per-round timeout (default 500)\n"
            "  --hc-rise         Consecutive successes to mark a backend healthy "
            "(default 2)\n"
            "  --hc-fall         Consecutive failures to mark a backend unhealthy "
            "(default 3)\n"
            "  --tui          Run the ncurses dashboard instead of plain log output\n"
            "  --metrics-port Port for the Prometheus /metrics HTTP endpoint (default 9105;\n"
            "                 0 disables it)\n"
            "  --help         Show this message\n",
            prog);
}

/* Long-only options (no short-flag equivalent) use values outside the ASCII
 * range so they can't collide with any future short option. */
enum {
    OPT_HC_INTERVAL_MS = 1001,
    OPT_HC_TIMEOUT_MS = 1002,
    OPT_HC_RISE = 1003,
    OPT_HC_FALL = 1004,
    OPT_TUI = 1005,
    OPT_METRICS_PORT = 1006,
};

int config_parse_args(int argc, char **argv, struct config *out) {
    memset(out, 0, sizeof(*out));
    out->ttl = 64;
    out->table_size = 65537;
    out->max_flows = 1000000;
    out->hc_interval_ms = 1000;
    out->hc_timeout_ms = 500;
    out->hc_rise = 2;
    out->hc_fall = 3;
    out->metrics_port = 9105;

    static struct option long_opts[] = {
        {"iface", required_argument, NULL, 'i'},
        {"vip", required_argument, NULL, 'v'},
        {"director-ip", required_argument, NULL, 'd'},
        {"backend", required_argument, NULL, 'b'},
        {"ttl", required_argument, NULL, 't'},
        {"table-size", required_argument, NULL, 'm'},
        {"max-flows", required_argument, NULL, 'f'},
        {"hc-interval-ms", required_argument, NULL, OPT_HC_INTERVAL_MS},
        {"hc-timeout-ms", required_argument, NULL, OPT_HC_TIMEOUT_MS},
        {"hc-rise", required_argument, NULL, OPT_HC_RISE},
        {"hc-fall", required_argument, NULL, OPT_HC_FALL},
        {"tui", no_argument, NULL, OPT_TUI},
        {"metrics-port", required_argument, NULL, OPT_METRICS_PORT},
        {"help", no_argument, NULL, 'h'},
        {NULL, 0, NULL, 0},
    };

    int have_iface = 0, have_vip = 0, have_director = 0;

    int c;
    while ((c = getopt_long(argc, argv, "i:v:d:b:t:m:f:h", long_opts, NULL)) != -1) {
        switch (c) {
        case 'i':
            strncpy(out->iface, optarg, CONFIG_IFACE_LEN - 1);
            have_iface = 1;
            break;
        case 'v':
            if (parse_ip(optarg, &out->vip) != 0) {
                fprintf(stderr, "invalid --vip: %s\n", optarg);
                return -1;
            }
            have_vip = 1;
            break;
        case 'd':
            if (parse_ip(optarg, &out->director_ip) != 0) {
                fprintf(stderr, "invalid --director-ip: %s\n", optarg);
                return -1;
            }
            have_director = 1;
            break;
        case 'b':
            if (out->n_backends >= CONFIG_MAX_BACKENDS) {
                fprintf(stderr, "too many --backend entries (max %u)\n", CONFIG_MAX_BACKENDS);
                return -1;
            }
            if (parse_backend_spec(optarg, &out->backends[out->n_backends]) != 0) {
                fprintf(stderr, "invalid --backend (expected IP:PORT): %s\n", optarg);
                return -1;
            }
            out->n_backends++;
            break;
        case 't': {
            long ttl = strtol(optarg, NULL, 10);
            if (ttl <= 0 || ttl > 255) {
                fprintf(stderr, "invalid --ttl: %s\n", optarg);
                return -1;
            }
            out->ttl = (uint8_t)ttl;
            break;
        }
        case 'm': {
            long m = strtol(optarg, NULL, 10);
            if (m <= 2 || m > (long)UINT32_MAX) {
                fprintf(stderr, "invalid --table-size: %s\n", optarg);
                return -1;
            }
            out->table_size = (uint32_t)m;
            break;
        }
        case 'f': {
            long f = strtol(optarg, NULL, 10);
            if (f <= 0) {
                fprintf(stderr, "invalid --max-flows: %s\n", optarg);
                return -1;
            }
            out->max_flows = (size_t)f;
            break;
        }
        case OPT_HC_INTERVAL_MS: {
            long v = strtol(optarg, NULL, 10);
            if (v <= 0) {
                fprintf(stderr, "invalid --hc-interval-ms: %s\n", optarg);
                return -1;
            }
            out->hc_interval_ms = (uint32_t)v;
            break;
        }
        case OPT_HC_TIMEOUT_MS: {
            long v = strtol(optarg, NULL, 10);
            if (v <= 0) {
                fprintf(stderr, "invalid --hc-timeout-ms: %s\n", optarg);
                return -1;
            }
            out->hc_timeout_ms = (uint32_t)v;
            break;
        }
        case OPT_HC_RISE: {
            long v = strtol(optarg, NULL, 10);
            if (v <= 0) {
                fprintf(stderr, "invalid --hc-rise: %s\n", optarg);
                return -1;
            }
            out->hc_rise = (uint32_t)v;
            break;
        }
        case OPT_HC_FALL: {
            long v = strtol(optarg, NULL, 10);
            if (v <= 0) {
                fprintf(stderr, "invalid --hc-fall: %s\n", optarg);
                return -1;
            }
            out->hc_fall = (uint32_t)v;
            break;
        }
        case OPT_TUI:
            out->tui_enabled = true;
            break;
        case OPT_METRICS_PORT: {
            long v = strtol(optarg, NULL, 10);
            if (v < 0 || v > 65535) {
                fprintf(stderr, "invalid --metrics-port: %s\n", optarg);
                return -1;
            }
            out->metrics_port = (uint16_t)v;
            break;
        }
        case 'h':
            config_print_usage(argv[0], stdout);
            return 1;
        default:
            config_print_usage(argv[0], stderr);
            return -1;
        }
    }

    if (!have_iface || !have_vip || !have_director || out->n_backends == 0) {
        fprintf(stderr, "missing required arguments\n\n");
        config_print_usage(argv[0], stderr);
        return -1;
    }

    return 0;
}
