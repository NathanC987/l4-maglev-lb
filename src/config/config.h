#ifndef L4MLB_CONFIG_CONFIG_H
#define L4MLB_CONFIG_CONFIG_H

#include <stdint.h>
#include <stdio.h>

#define CONFIG_MAX_BACKENDS 64u
#define CONFIG_IFACE_LEN 16u

struct config_backend {
    uint32_t addr; /* opaque network-byte-order IPv4 */
    uint16_t port; /* host byte order */
};

struct config {
    char iface[CONFIG_IFACE_LEN];
    uint32_t vip;         /* opaque network-byte-order IPv4; clients connect here */
    uint32_t director_ip; /* opaque network-byte-order IPv4; this host's IP, GRE outer src */
    uint8_t ttl;
    uint32_t table_size; /* Maglev M; must be prime, validated by the caller */
    size_t max_flows;
    struct config_backend backends[CONFIG_MAX_BACKENDS];
    size_t n_backends;

    uint32_t hc_interval_ms;
    uint32_t hc_timeout_ms;
    uint32_t hc_rise;
    uint32_t hc_fall;
};

/* Parses argv via getopt_long. Returns 0 if out is ready to use, 1 if --help
 * was given (usage already printed to stdout, caller should exit 0), or -1
 * on a parse/validation error (usage already printed to stderr, caller
 * should exit non-zero). */
int config_parse_args(int argc, char **argv, struct config *out);

void config_print_usage(const char *prog, FILE *stream);

#endif /* L4MLB_CONFIG_CONFIG_H */
