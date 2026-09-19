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

## The benchmark dashboard

A second, independent dashboard, "L4 Maglev Load Balancer — Benchmark"
(`grafana/dashboards/maglev-lb-bench.json`), scrapes the separate bench topology
(`scripts/bench/`) via its own Prometheus instance (`docker-compose-bench.yml` +
`prometheus-bench.yml`, port 9091) and its own Grafana datasource - kept fully
separate from the base dashboard above specifically so the two topologies can never
collide or make each other's panels ambiguous. Bring both up together:

```sh
cd monitoring
docker compose -f docker-compose.yml -f docker-compose-bench.yml up -d
```

Tear down with the **same `-f` flags**, not just `docker compose down`: Compose only
knows about the services/volumes declared in whatever files you pass it, so a plain
`docker compose down` (or `up`) only ever sees `docker-compose.yml` and leaves the
`prometheus-bench` container and its volume running/behind.

```sh
docker compose -f docker-compose.yml -f docker-compose-bench.yml down -v
```

If you've already done the asymmetric thing above and want to clean up the orphan
directly instead: `docker rm -f maglev-lb-prometheus-bench && docker volume rm
monitoring_prometheus-bench-data`.

See `docs/benchmarking.md` for what's on it (including the headline "DSR in action"
panel, comparing the LB's own bandwidth against what the client actually receives)
and how to run the scenario that feeds it.
