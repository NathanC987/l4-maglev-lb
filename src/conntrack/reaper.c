#include "reaper.h"

#include <string.h>
#include <sys/timerfd.h>
#include <unistd.h>

int reaper_timerfd_create(uint32_t interval_ms) {
    int fd = timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK);
    if (fd < 0) {
        return -1;
    }

    struct itimerspec spec;
    memset(&spec, 0, sizeof(spec));
    spec.it_value.tv_sec = interval_ms / 1000;
    spec.it_value.tv_nsec = (long)(interval_ms % 1000) * 1000000L;
    spec.it_interval = spec.it_value;
    if (timerfd_settime(fd, 0, &spec, NULL) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

size_t reaper_on_timer_fired(int timerfd, struct conntrack_table *ct, uint64_t now_ns,
                              uint64_t tcp_timeout_ns, uint64_t udp_timeout_ns,
                              size_t buckets_per_tick) {
    uint64_t expirations;
    ssize_t n = read(timerfd, &expirations, sizeof(expirations));
    (void)n; /* EAGAIN/short read is fine, we still run one sweep tick below */
    return conntrack_reap_slice(ct, now_ns, tcp_timeout_ns, udp_timeout_ns, buckets_per_tick);
}
