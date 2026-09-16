#include "health_checker.h"

#include <stdlib.h>

struct health_checker {
    struct backend_manager *bm;
    struct health_check_cfg cfg;
};

struct health_checker *health_checker_create(struct backend_manager *bm,
                                              struct health_check_cfg cfg) {
    struct health_checker *hc = calloc(1, sizeof(*hc));
    if (hc == NULL) {
        return NULL;
    }
    hc->bm = bm;
    hc->cfg = cfg;
    return hc;
}

void health_checker_destroy(struct health_checker *hc) {
    free(hc);
}

int health_checker_start(struct health_checker *hc) {
    (void)hc;
    return 0;
}

void health_checker_stop(struct health_checker *hc) {
    (void)hc;
}
