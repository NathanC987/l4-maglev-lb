#include <assert.h>
#include <arpa/inet.h>
#include <getopt.h>
#include <stdio.h>
#include <string.h>

#include "config/config.h"

/* Silences the usage text a rejected argv prints. */
static int parse_backend(const char *spec, struct config *cfg) {
    char *argv[] = {"maglev-lb", "--iface", "eth0", "--vip", "10.0.0.1", "--director-ip", "10.0.0.2",
                    "--backend", (char *)spec, NULL};
    optind = 1;
    FILE *saved = stderr;
    stderr = fopen("/dev/null", "w");
    int rc = config_parse_args(9, argv, cfg);
    fclose(stderr);
    stderr = saved;
    return rc;
}

static void test_plain_backend_has_no_mac(void) {
    struct config cfg;
    assert(parse_backend("10.0.0.5:9000", &cfg) == 0);
    assert(cfg.n_backends == 1);
    assert(cfg.backends[0].addr == inet_addr("10.0.0.5"));
    assert(cfg.backends[0].port == 9000);
    assert(!cfg.backends[0].has_mac);
}

static void test_backend_with_mac(void) {
    struct config cfg;
    const uint8_t want[CONFIG_MAC_LEN] = {0x0a, 0x1b, 0x2c, 0x3d, 0x4e, 0xff};
    assert(parse_backend("10.0.0.5:9000@0a:1b:2c:3d:4e:ff", &cfg) == 0);
    assert(cfg.backends[0].port == 9000);
    assert(cfg.backends[0].has_mac);
    assert(memcmp(cfg.backends[0].mac, want, CONFIG_MAC_LEN) == 0);
}

static void test_bad_macs_are_rejected(void) {
    struct config cfg;
    assert(parse_backend("10.0.0.5:9000@", &cfg) != 0);
    assert(parse_backend("10.0.0.5:9000@0a:1b:2c:3d:4e", &cfg) != 0);
    assert(parse_backend("10.0.0.5:9000@0a:1b:2c:3d:4e:ff:00", &cfg) != 0);
    assert(parse_backend("10.0.0.5:9000@0a:1b:2c:3d:4e:zz", &cfg) != 0);
    assert(parse_backend("10.0.0.5@0a:1b:2c:3d:4e:ff", &cfg) != 0);
}

int main(void) {
    test_plain_backend_has_no_mac();
    test_backend_with_mac();
    test_bad_macs_are_rejected();
    printf("test_config: all tests passed\n");
    return 0;
}
