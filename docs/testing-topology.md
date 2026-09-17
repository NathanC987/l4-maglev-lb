# Test topology

Everything runs on one machine using network namespaces and veth pairs, all defined
in `scripts/topology.sh` and built by `scripts/setup-netns.sh`.

## Layout

```
                     br-lan (root netns), 10.99.0.0/24, MTU 1600
                     also has 10.99.0.254/24 itself (HOST_IP - see below)
        ┌───────────────┬───────────────────┬───────────────────┐
   veth-client-br    veth-lb-br         veth-be1-br          veth-be2-br
        │                │                   │                     │
  [l4mlb-client]    [l4mlb-lb]         [l4mlb-be1]           [l4mlb-be2]
   veth-client        veth-lb            veth-be1               veth-be2
   10.99.0.2/24      10.99.0.1/24       10.99.0.11/24          10.99.0.12/24
                  metrics on :9105  gre1: remote 10.99.0.1     gre1: remote 10.99.0.1
                                          local 10.99.0.11           local 10.99.0.12
                                    VIP 10.99.0.100/32 on lo   VIP 10.99.0.100/32 on lo
                                    echo server on 0.0.0.0:9000 (same on backend2)

  client route: 10.99.0.100/32 via 10.99.0.1 dev veth-client
```

All four namespaces sit on **one shared bridge**, not separate client-facing and
backend-facing segments. This isn't a shortcut - it mirrors how real anycast/ECMP L4
load balancers actually work: the load balancer is just another L3-adjacent node,
reachable only because the client has a specific host route pointing at it (not
because it's a default gateway or otherwise in every packet's path). Backend replies
go directly to the client over the same bridge; the load balancer never sees them,
enforced by the bridge only forwarding unicast frames to the port that owns the
destination MAC, not by anything in `maglev-lb` itself.

The root netns also gets its own address directly on `br-lan` (`HOST_IP` in
`topology.sh`) - purely for observability, so a process outside all four simulated
namespaces (a host-networked Prometheus container, a `curl` from a real terminal) can
reach the LB's metrics endpoint at `10.99.0.1:9105` without needing to be inside any
of them. It's just another L3-adjacent host on the same bridge, exactly like the
client and backends; nothing routes VIP traffic through it, so it has no effect on the
DSR bypass proof below. See `monitoring/README.md` for how this gets used.

## Request path

1. Client sends to the VIP. Its route table says the next hop is `10.99.0.1` (the
   load balancer's real IP), so the frame's destination MAC is the load balancer's.
2. `maglev-lb`'s raw socket on `veth-lb` receives it (this works whether or not
   `net.ipv4.ip_forward` is set in the `lb` namespace - it's deliberately left off,
   since a raw socket taps frames at the device level independent of the kernel's own
   forwarding decision, and there's nothing for the kernel to forward here in the
   first place since the VIP isn't a local address in that namespace).
3. Maglev/conntrack picks a backend; the original packet is wrapped in GRE (outer
   src = the load balancer's own IP, outer dst = the backend's real IP) and sent back
   out the same interface - client and backends are on the same L2 segment, so one
   socket serves both directions.
4. The backend's `gre1` tunnel device (`ip tunnel add gre1 mode gre remote <lb-ip>
   local <backend-ip> ttl 255`) matches the outer (src, dst) pair and decapsulates.
   The inner packet's destination, the VIP, is a local address there (assigned to
   `lo`), so the kernel delivers it straight to the echo server socket.
5. The echo server replies with source address = VIP. Since the client is
   L2-adjacent, the reply goes directly to it - the load balancer is never involved.

## Gotchas this topology actually hit (and why)

These were all found by running the real thing, not anticipated in advance - worth
knowing if you're extending the topology or hitting similar failures elsewhere.

- **`rp_filter` drops the decapsulated packet.** After `gre1` decapsulates, the inner
  packet's source is the *client's* real address, which has no route back out via
  `gre1` (a point-to-point tunnel to the load balancer, not to arbitrary clients).
  Strict/loose reverse-path filtering treats that as a spoofed ("martian") source and
  silently drops it. Fixed by setting `net.ipv4.conf.{all,default,gre1}.rp_filter=0`
  in each backend namespace before/after creating the tunnel. This is the standard,
  well-documented gotcha for any DSR-over-tunnel setup, not specific to this project.

- **veth checksum offload corrupts what the raw socket reads.** veth defers UDP/TCP
  checksum computation to a NIC that doesn't exist for a purely virtual link, and
  that deferred (incomplete) state survives all the way through the peer device and
  the bridge. A real NIC always finalizes the checksum before anything hits the wire;
  a raw socket reading a veth's traffic sees the *un*finalized bytes. Fixed with
  `ethtool -K <iface> tx off` on every veth interface in the topology.

- **The echo server has to bind to `0.0.0.0`, not the VIP.** The health checker
  (added in M2) probes a backend's *real* IP directly, separately from the VIP/GRE
  path. A listener bound only to the VIP is unreachable at the real IP -
  `ECONNREFUSED` regardless of whether the backend is actually healthy.

- **UDP replies need `IP_PKTINFO` to get the source address right.** TCP's
  `accept()` hands back a socket already bound to the exact local address the
  client's SYN targeted, so a reply naturally uses the right source. A single
  wildcard-bound UDP socket has no such per-datagram memory - a plain `sendto()`
  reply picks whatever address routing prefers (the backend's real IP), breaking DSR.
  `scripts/echo_server.py`'s UDP handler uses `IP_PKTINFO` ancillary data to read
  each datagram's actual destination and reply from that same address.

## Proving DSR actually happens

`scripts/run-integration-tests.sh` and `run-flow-regen-test.sh` both install an
`nftables` `netdev`/`ingress` hook counter directly on the load balancer's own
interface, matching `ip saddr == <VIP>`:

```
nft add table netdev l4mlbtest
nft add chain netdev l4mlbtest ingress '{ type filter hook ingress device veth-lb priority 0; }'
nft add rule netdev l4mlbtest ingress ip saddr <VIP> counter
```

This hooks at the device level, the same place a raw socket taps - not `INPUT` or
`FORWARD`, which wouldn't see this traffic anyway since it's never addressed to or
routed through the load balancer. A full passing test run with this counter still at
zero is a direct, automated proof that the load balancer never received a single
frame from the return path, not just that it "didn't need to."

## Running it

```sh
sudo scripts/setup-netns.sh        # build the topology
sudo scripts/run-lb.sh             # run maglev-lb inside the lb namespace
sudo scripts/teardown-netns.sh     # tear it down

sudo scripts/run-integration-tests.sh   # TCP stickiness + UDP DSR + bypass proof + metrics check
sudo scripts/run-flow-regen-test.sh     # M2: a flow survives a table regen after a backend dies
sudo scripts/run-tui-smoke-test.sh      # M3: --tui starts, renders, and exits cleanly (q, SIGTERM)
```

All three `run-*-test.sh` scripts bring the topology up, run `maglev-lb`, check
everything, and tear down again on exit (via a `trap`) regardless of pass or fail.
Pass an alternate binary path as the first argument to run the same checks against
the Asan or Tsan build, e.g. `sudo scripts/run-flow-regen-test.sh build-tsan/maglev-lb`.
`run-integration-tests.sh`'s last check `curl`s the LB's own `/metrics` endpoint
(reachable at `127.0.0.1:9105` from inside the `lb` namespace) and asserts the numbers
it reports (rx packets, conntrack hits, backend series count) actually match the
traffic the test just generated - see `monitoring/README.md` for the Prometheus +
Grafana stack that visualizes this same endpoint.

Everything here needs root (raw sockets, network namespaces). If you're scripting
this outside an interactive terminal where `sudo` can't prompt for a password, a
narrowly-scoped `NOPASSWD` sudoers rule for exactly the commands involved (`ip`,
`sysctl`, `ethtool`, `curl`, `python3`, `kill`, `docker`/`docker-compose` if you're
also bringing up `monitoring/`, and the scripts themselves) works and should be
removed again once you're done - see the commands each script actually runs before
deciding what to allow.
