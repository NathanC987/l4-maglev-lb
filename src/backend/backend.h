#ifndef L4MLB_BACKEND_BACKEND_H
#define L4MLB_BACKEND_BACKEND_H

#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>

#include "../net/eth.h"

enum backend_admin_state {
    BACKEND_ENABLED,
    BACKEND_DRAINING,
    BACKEND_DISABLED,
};

struct backend {
    uint32_t id;
    uint32_t addr;              /* opaque network-byte-order IPv4 */
    uint16_t port;              /* host byte order; service/health-check port */
    uint8_t mac[ETH_ADDR_LEN];  /* valid only once mac_resolved is true */
    _Atomic bool mac_resolved;
    _Atomic bool healthy;       /* written by health_checker (M2); read by table_generator */
    enum backend_admin_state admin_state;
};

#endif /* L4MLB_BACKEND_BACKEND_H */
