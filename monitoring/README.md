# Monitoring: Prometheus + Grafana

`maglev-lb` exposes a Prometheus `/metrics` endpoint on its own (port 9105 by default,
`--metrics-port 0` disables it - see `src/ui/metrics_http.c`). This directory brings up
Prometheus (scraping it) and Grafana (with the dashboard below pre-loaded) via Docker.

## Running it

The netns test topology must be up first (`sudo scripts/setup-netns.sh`) and
`maglev-lb` running inside it, so there's something to scrape.

```sh
cd monitoring
docker compose up -d
```

- Grafana: http://localhost:3000 (anonymous viewing is enabled; login `admin`/`admin`
  for anything that needs write access, e.g. editing the dashboard)
- Prometheus: http://localhost:9090

Both containers use `network_mode: host`, and `scripts/setup-netns.sh` gives the root
network namespace its own address on the shared bridge (`10.99.0.254`, see
`scripts/topology.sh`'s `HOST_IP`) specifically so a host-networked container can reach
`maglev-lb` at `10.99.0.1:9105` inside its own network namespace - see
`docs/testing-topology.md` for why this is safe and doesn't affect the DSR bypass
proof. If you're running `maglev-lb` somewhere other than this test topology, edit the
scrape target in `prometheus.yml`.

`docker compose down` tears both containers down; add `-v` to also drop their data
volumes (dashboard edits made through the UI, and Prometheus's stored history).

## What's on the dashboard

One dashboard, "L4 Maglev Load Balancer" (`grafana/dashboards/maglev-lb.json`),
organized to follow the request path:

- **Overview** - uptime, backend counts, active flows, table generation, current
  throughput: the at-a-glance row.
- **Client → Load Balancer** - ingress packet rate and bandwidth, conntrack hit/miss
  rate and ratio (is the fast path - a flow already pinned - doing most of the work?).
- **Load Balancer Internals** - egress rate to backends, drops broken down by reason,
  conntrack evictions, active flow count over time.
- **Backends** - a health timeline (green/red per backend over time), per-backend
  packet rate (this is where Maglev's load-splitting is directly visible), bandwidth,
  active flows, and health-check failure rate.
- **Maglev Table Regeneration** - generation count and regeneration duration over
  time, so a backend health flip and its effect on the table are both visible
  together.

Every panel's metric is defined and commented in `src/ui/metrics_http.c`; the dashboard
just visualizes what's already there.
