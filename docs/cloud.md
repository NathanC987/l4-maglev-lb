# Cloud deployment: the real LB on AWS, with AWS as the referee

Everywhere else in this project, the client, the load balancer and the backends
are network namespaces on one machine, wired together with veth pairs. That is a
good place to develop, but it is still a simulation of a network, and every
number the project reports is the system measuring itself. `cloud/` takes both
of those away:

1. **EC2 + VPC: real hosts on a real network.** The client, `maglev-lb`, four
   backends and a monitoring host are separate EC2 instances in one VPC subnet.
   Packets cross a real hypervisor and a real virtual network, and every shortcut
   veth allowed has to work for real (see [What AWS forced](#what-aws-forced)).
2. **CloudWatch: an independent referee.** AWS measures the same traffic itself.
   CloudWatch's EC2 metrics are the hypervisor's own packet and byte counts for
   each host. **VPC Flow Logs** record the flows on each network interface,
   including the source address written in each packet. That is enough to check,
   without trusting anything this project runs, that the LB really sends only
   GRE, that the client's replies really come from the VIP and from the backends
   rather than the LB, and that `maglev-lb`'s own count of forwarded packets
   matches AWS's count.
3. **CloudFormation: the environment as code.** One command creates all of it
   and one command deletes all of it, so a run is reproducible and nothing is
   left behind.

Together with Prometheus and Grafana, which now run on the monitoring host and
show CloudWatch next to the system's own metrics, that is five cloud services.
Each one does a job the project needed done.

**Be precise about what the results mean.** They come from small, shared,
burstable VMs, so they show the system working across real hosts and what the
cloud imposes on it. They are not `maglev-lb`'s peak throughput; that remains the
local `pktgen` number in [`benchmarking.md`](benchmarking.md).

> **Status:** run on AWS end to end (bring-up, smoke test, demo, sweep, Grafana on
> mon). See [Results](#results). The live canvas can also drive the instances
> directly: see [The live canvas on AWS](#the-live-canvas-on-aws).

## Architecture

```
your laptop: cloud/*.sh (AWS CLI + ssh)
   │ CloudFormation (cloud/stack.yaml)
   ▼
VPC 10.99.0.0/16, one subnet 10.99.1.0/24              VPC route: 10.200.0.10/32 (VIP) → lb
 ┌──────────┐ TCP to VIP ┌────────────────────┐ GRE ┌───────────────────────────┐
 │ client   │──────────▶│ lb 10.99.1.20       │────▶│ be1..be4 10.99.1.31..34   │
 │10.99.1.10│           │ maglev-lb on ens5   │     │ gre1 + VIP on lo          │
 └──────────┘◀──────────┤ src/dst check OFF   │     │ src/dst check OFF         │
      ▲      (never)    └────────────────────┘     └─────────────┬─────────────┘
      └────────────── replies, source = VIP, straight to the client (DSR) ──┘
 ┌──────────┐ scrapes lb:9105 and client:9106; reads CloudWatch with its IAM role
 │ mon      │ Prometheus + Grafana (Docker), reached only through an SSH tunnel
 │10.99.1.40│
 └──────────┘
VPC Flow Logs (whole subnet) → CloudWatch Logs, 1-day retention, deleted with the stack
```

- **The LB and every backend share one subnet.** `maglev-lb` ARPs each backend's
  address directly (it has no next-hop gateway support), and the VPC answers ARP
  only for addresses in the same subnet.
- **The VIP, 10.200.0.10, belongs to no instance.** It is outside the VPC's
  range, and a route-table entry sends it to the LB. This is the cloud version
  of the local topology's `ip route add VIP via LB` on the client.
- **be4 starts dormant** for the demo's scale-out scene: the host is up, but
  nothing listens on it until the scene starts its responder.
- **The workload is the existing code:** `scripts/bench/streaming_server.py`
  and `viewer_gen.py` for the streaming demo, and `bridge/service_server.py` and
  `bridge/load_client.py` for request/response. Nothing was rewritten for the cloud.

## What AWS forced

Each of these is something the local veth topology hid. The fix is in the
template or in `cloud/hostnet/`, next to a comment explaining why.

| What the network does | Why it matters here | Fix |
|---|---|---|
| Drops packets addressed to an IP the receiving instance doesn't own ("source/destination check") | Client packets for the VIP arrive at the LB, which doesn't own the VIP | `SourceDestCheck: false` on the LB |
| Drops packets *sent* from an IP the instance doesn't own | DSR replies leave the backends with the VIP as their source | `SourceDestCheck: false` on every backend |
| Routes only to addresses it knows | Nobody owns the VIP | Route table: VIP/32 → the LB instance |
| Security groups filter by IP protocol, not just TCP/UDP | LB→backend traffic is GRE (IP protocol 47) | SG allows all protocols inside the VPC, plus the VIP as a source |
| ENA merges TCP segments (GRO) before packet sockets see them; veth doesn't | `maglev-lb` would receive, and try to encapsulate, frames far larger than the MTU | `ethtool -K <iface> gro off lro off` on the LB |
| The VPC MTU is 9001; GRE adds 24 bytes | A full-size client packet plus GRE must still fit | Client MTU 1500 (as the local topology does with 1500 / 1600) |
| t2/t3 instances run on CPU credits, and a T3 in `standard` mode **starts with none** (measured: 0.5 after 10 minutes, then 12 an hour) | Under load, the LB or the client would be throttled to its 10% baseline within minutes, and the result would measure AWS, not the LB | The LB and the client, the hosts the benchmarks measure, are `c7i-flex.large` (no credits; `--measured-type`). The rest stay `t3.micro`, with `--cpu-credits`: `standard` is never billed for bursting, and `unlimited` keeps the sweep's busy backends at full speed for a few cents. Benchmarks refuse to run on `standard` hosts low on credits |
| Each instance has a packets-per-second and bandwidth allowance | Past it, the hypervisor drops or queues packets, whatever the LB does | Runs record ENA's `*_allowance_exceeded` counters before and after |
| `maglev_lb_rx_packets_total` counts every IPv4 frame on the raw socket, including the LB host's own traffic | Comparing it with AWS's packets-in would mislead | The referee compares `tx_packets` (GRE only) with Flow Logs' GRE count |
| **Flow Logs don't log what an interface sends from an address it doesn't own.** A backend's VIP-sourced replies get no records; its interface gets `SKIPDATA` records for those intervals instead (seen in every run, though AWS's docs suggest `pkt-srcaddr` should show them). The LB and client got `SKIPDATA` only during request/response load, not streaming | The referee can't use the backends' own flow logs to show that they sent the replies | The referee proves it from the other ends: the client's flow logs show replies from the VIP, the LB's show it sent none, and CloudWatch's packet counters show the backends sent at least that many |
| `NetworkPacketsOut` is 5-minute only, even with detailed monitoring on, and a host's samples can land in the neighbouring period (a backend's packets from 03:14 were counted in its 03:15 period, the client's in 03:10) | A count over exactly the window's periods can come up short | `dsr-proof.sh` fetches the 5-minute periods covering the window plus the next one, and the CloudWatch checks are "at least" |
| Flow-log records can be stamped well after their packets (seen: about 70 s) | With a generous time margin, the previous scene's traffic counted as this one's (47,159 "GRE packets" against a true 21,799) | The referee takes only records ending after the window starts, and the run scripts keep 95 s before a window free of VIP traffic |
| New accounts may run 5 on-demand vCPUs at a time | Even the minimal profile needs 6 | `up.sh` checks the quota first; ask for 16 once (below) |

## AWS as referee

`cloud/dsr-proof.sh` takes a measured window (the run scripts mark its start and
end, recording `maglev-lb`'s counters at each) and waits for the flow logs to
arrive, which takes a few minutes. It then fetches the CloudWatch metrics and
runs `cloud/referee.py`, which checks:

| Check | From | Passes when |
|---|---|---|
| The LB sends no reply traffic | Flow Logs, LB interface, egress | Nothing with source VIP, nothing to the client; only GRE |
| The client gets its replies from the VIP | Flow Logs, client interface, ingress | TCP with `pkt-srcaddr` = VIP |
| The backends sent them | CloudWatch `NetworkPacketsOut` of the backends | At least as many packets as the client got from the VIP (the LB sent none, so they came from somewhere else) |
| The client got what the backends sent | Flow Logs, client ingress vs backends' egress | Within 5%. Only when the backends' egress is logged, which AWS was not seen to do (reported as INFO with the `SKIPDATA` count) |
| `maglev-lb` counts its forwarding correctly | `tx_packets` delta vs Flow Logs' GRE packets out of the LB | Within 2% (inconclusive if the LB's interface has `SKIPDATA` records) |
| AWS saw at least that much leave the LB | CloudWatch `NetworkPacketsOut` of the LB | At least `maglev-lb`'s `tx_packets` delta |
| DSR ratio | Client bytes received ÷ LB bytes sent | Reported (INFO) |

The field that makes this possible is `pkt-srcaddr`. For traffic arriving at an
interface, it is the address written in the packet, so the client's records show
its replies came from the VIP even though no host owns it.

The Grafana dashboard **"L4 Maglev — AWS as referee"** shows the same evidence
live: AWS's packet count for the LB next to `maglev-lb`'s, bytes sent per host
(the backends send far more than the LB), a Flow Logs table of who sent what
from which address, and every instance's CPU credit balance.

## Cost: staying in the free tier

AWS changed its free tier on 2025-07-15, and the terms keep changing. **Check
https://aws.amazon.com/free for your account before the first run.**

- **Accounts created on or after 2025-07-15** are on the **Free plan**: sign-up
  credits (up to $200, for 6 months), and the account cannot be charged unless
  you upgrade it to a paid plan. Instance hours are paid from the credits
  (about $0.015/hour for a t3.micro with its public IP, $0.09 for a
  c7i-flex.large). Free-plan instance types include both, and the defaults use
  c7i-flex.large for the LB and the client and t3.micro for the rest: about
  $0.26/hour for the full stack, $0.20 for the minimal one. The whole run in
  [Results](#results) kept the full stack up for 50 minutes: about $0.22.
- **Older accounts** have the legacy 12-month free tier: 750 hours a month of
  t2.micro (t3.micro in regions without t2), 30 GB of EBS, and 750 hours of
  public IPv4. Use `--instance-type t2.micro --measured-type t2.micro` there.

| Resource | Use | Covered by |
|---|---|---|
| EC2 | 7 instances (3 with `--profile minimal`), only while a stack is up | Instance hours (legacy: 750 h/month shared, about 100 h of the full stack), or credits |
| EBS | 7 × 8 GB root volumes, deleted with the instances | 30 GB-months (legacy), or credits |
| Public IPv4 | One per instance, for SSH and package installs | 750 h/month (legacy), or credits |
| Data transfer | All traffic between hosts uses private IPs in one AZ | Free |
| CloudWatch metrics | Basic 5-minute EC2 metrics on every host; 1-minute on the LB only (CPU and bytes; packet counts stay 5-minute) | Always free (basic metrics, 10 detailed metrics) |
| CloudWatch API | Grafana's CloudWatch panels (refresh 1 min), `dsr-proof.sh`; on the canvas, the bridge's reads for its dashboard (only while it is open: well under a cent an hour) | `GetMetricData` is billed at about $0.01 per 1,000 metrics, a few cents per hour of dashboard. This is the one item not strictly free. |
| CloudWatch Logs | VPC Flow Logs, 1-day retention, deleted with the stack | 5 GB/month ingestion. The streaming workload makes a few MB per run; the request/response sweep refuses to run with flow logs on |
| CloudFormation, VPC, IAM | The stack itself | Free |

Guard rails built in:

- **Dead-man switch:** every instance runs `shutdown -h +240` at boot and is set
  to terminate on shutdown. A forgotten stack loses its instances (and their
  volumes) after 4 hours. Change it with `--auto-terminate MINUTES`.
- `cloud/down.sh` deletes everything; `cloud/status.sh` lists anything still running.
- No NAT gateway, load balancer or Elastic IP: the usual surprise bills.
- SSH is open only to your IP. Grafana listens on 127.0.0.1 on the mon host,
  reached through an SSH tunnel.
- **Do this once yourself:** create a zero-spend budget (Billing → Budgets →
  "Zero spend budget") so AWS emails you if anything ever costs money.

## Running it

### Before you start (once)

1. Install the **AWS CLI v2** and sign in: `aws configure sso` (or `aws
   configure` with an access key), and pick a region, e.g. `us-east-1`.
2. Create an EC2 key pair named `l4mlb` and keep the private key at
   `~/.ssh/l4mlb.pem` (`chmod 600`). In the console: EC2 → Key pairs → Create.
   To use another name or path, set `L4MLB_KEY_NAME` / `L4MLB_SSH_KEY`.
3. Create the zero-spend budget (above).

### Commands

```sh
cloud/up.sh --profile minimal        # client + lb + be1 only: cheapest bring-up
cloud/check-bringup.sh               # does every assumption hold on this network?
cloud/dsr-proof.sh                   # a few minutes later: AWS checks that run

cloud/up.sh --profile full --cpu-credits unlimited
                                     # adds be2..be4 and mon (Prometheus + Grafana)
cloud/grafana-tunnel.sh              # then http://localhost:3000 (separate terminal)
cloud/run-demo.sh                    # streaming demo: crash, drain, scale-out + referee

cloud/up.sh --flow-logs off --no-deploy   # before the sweep (instances are kept)
cloud/run-rr-bench.sh                # request/response sweep: 100 → 8000 req/s
cloud/up.sh --flow-logs on --no-deploy

cloud/run-cloud-smoke-test.sh        # everything above, with assertions
cloud/down.sh                        # delete it all
cloud/status.sh                      # confirm nothing is left
```

`up.sh` remembers its options between runs (`cloud/.run/params.env`) and re-runs
`deploy.sh`. `deploy.sh` installs packages on first run, ships the current
working tree (uncommitted changes included; no host needs access to GitHub),
builds `maglev-lb` on the LB, applies `cloud/hostnet/`, and starts Prometheus and
Grafana on mon. Re-run it after changing code. Results land in `cloud/results/`.

The full profile needs 14 on-demand vCPUs (7 × 2) and the minimal one 6, but new
accounts start with a quota of 5, which `up.sh` checks before creating anything.
Ask for 16 once in Service Quotas → Amazon EC2 → "Running On-Demand Standard (A, C,
D, H, I, M, R, T, Z) instances" (free; it can take from minutes to a day or two), or
from the CLI:

```
aws service-quotas request-service-quota-increase --service-code ec2 \
    --quota-code L-1216C47A --desired-value 16
```

On the new Free plan, 1-vCPU `t2.micro` is not free-tier eligible, so that is not a
way around the quota.

## The live canvas on AWS

`cloud/canvas.sh` puts the live canvas ([`live-mode.md`](live-mode.md)) in front of
real EC2 instances instead of local network namespaces. Every node on the canvas is
an instance: wiring a backend to the LB starts one and runs `service_server.py` on
it, and deleting it drains it, stops the process and stops the instance. Each
client (`load_client.py`) and the LB (`maglev-lb`) own an instance the same way, so
the offered-load slider is real traffic on real machines. The inspector shows each
node's instance and that instance's own CPU and network use.

```sh
cloud/canvas.sh up       # once: create the stack, install and build on every host, stop them all
cloud/canvas.sh run      # prints a link; nothing on AWS starts until that page is open
cloud/canvas.sh grafana  # optional, in another terminal: the dashboard at http://localhost:3001
cloud/canvas.sh down     # delete it all, disks included
```

**The pool.** The `canvas` profile is 8 instances: `client` and `client2`
(t3.micro), `lb` (c7i-flex.large) and `be1`..`be5` (t3.micro), 16 vCPUs, which is the
account's on-demand quota exactly. So the canvas runs at most 5 backends and 2
clients. A sixth backend shows as pending and starts nothing, and the inspector says
why.

**Stop, not terminate.** A removed node's instance is stopped, not terminated. A
stopped instance costs only its 8 GB disk (about $0.001 an hour), keeps its
software and private address, and is serving again 13-30 s after it is wanted.
Terminating would mean reinstalling and rebuilding on every add. It would also mean
letting the bridge launch instances, which is what keeps the pool a hard limit.

**How it works.** `bridge/cloud_bridge.py` is the same bridge with a different
executor (the reconciler, API and canvas are shared with local live mode).

- `bridge/cloud_hosts.py` decides which instances should run, starts and stops
  them in batches, and keeps an instance nobody wants for 60 s first. EC2 bills
  at least a minute per start, and undo should be instant.
- Each process runs over one multiplexed SSH connection per host. Stdin closing
  still means exit, so a dropped connection or a dead bridge leaves nothing running
  remotely.
- The LB runs under `bridge/lb_agent.py`, which pushes `maglev-lb`'s metrics and
  CPU back over that channel. Polling its HTTP exporter four times a second across
  the internet is too slow for the 0.5 s it allows.
- `bridge/host_agent.py` reports each instance's CPU and network.
- The load balancer's **Open its Maglev and conntrack tables** button shows the
  real tables from `maglev-lb` on the LB instance, live: `lb_agent.py` reads them
  from `maglev-lb`'s `/tables` endpoint and sends them over the same SSH channel,
  the ~90 KB Maglev table only when it changes. See "Inside maglev-lb" in
  [`live-mode.md`](live-mode.md).
- A powered-off backend does not answer ARP, and `maglev-lb` used to exit when a
  backend didn't. It now takes `--backend IP:PORT@MAC`, and the bridge passes each
  instance's MAC address, which EC2 keeps across stop and start.

**Safety.**

| Risk | What stops it |
|---|---|
| A bug starts too much | The bridge runs as `CanvasOperatorRole`, with 1-hour credentials that can only start and stop instances tagged for this stack, and describe them. It cannot launch one, so AWS itself enforces the pool |
| Spending runs on | Everything stops at `--budget-usd` (default $3 of estimated spend), after `--max-minutes` (120), 10 minutes after the last browser tab closes, with `cloud/canvas.sh stop`, or when the bridge exits (Ctrl-C included) |
| The laptop dies | Each running instance is told to shut down in 20 minutes, and only a live bridge keeps pushing that back. A per-boot dead-man switch (90 minutes) covers an instance started some other way. Shutting down stops these instances; it doesn't terminate them |
| The browser is hostile | Same API rules as local live mode: loopback only, a token, strict validation. No canvas id or label reaches an AWS call or a shell. Remote commands are fixed paths plus validated numbers, each quoted |
| A host is impersonated | SSH host keys are recorded per instance id during `canvas.sh up` and checked strictly afterwards, though a stop and start changes the public address. SSH is open to your IP only, and IMDSv2 is required |
| Root on the laptop | None needed: the bridge runs as you, with your AWS CLI sign-in and the `l4mlb` key |

**Measured on AWS (2026-11-17, us-east-1).** A scripted session drove the
bridge's API with the same documents the canvas sends, and checked every instance's
state with EC2 directly. The real canvas then drove it from a headless browser.

| Step | Result |
|---|---|
| LB, one client, two backends, from all stopped | EC2 started exactly `client`, `lb`, `be1`, `be2`; both backends serving 30 s after the design arrived |
| Raise the load (table below) | Each instance's CPU rose with it; Maglev split requests evenly |
| Add a backend under 800 req/s | `be3` started and was serving 22 s after it was added |
| Add a second client | `client2` started; its requests were answered 20 s after it was added, from 10.99.1.11 |
| Crash a backend at 1,200 req/s | LB marked it down within 2-3 s; failures only in that window (see below) |
| Drain, then undrain | No new flows to it within 2 s; 0 failures |
| Remove a backend and a client | Both instances stopping after the 60 s grace (and the drain), stopped 20 s later; only the 4 on the canvas still running |
| Six backends on the canvas | Five served; the sixth showed as pending; EC2 ran exactly 5 backend instances |
| Delete the LB, then add it back | All 8 instances stopped within 84 s; from all stopped, traffic flowed again 24 s after adding it back. The VIP route recovers by itself after the LB instance stops and starts |
| Open the real canvas | Its default design (3 backends) was serving on EC2 20 s later |
| "Stop all on AWS" (a canvas button then; its API, `POST /api/v1/cloud/stop`, remains) | The bridge stopped every instance, confirmed it with EC2 and exited 0 |

| Offered req/s | OK req/s | Failed/s | p99 ms | be1 CPU | be2 CPU | Client CPU | LB datapath |
|---|---|---|---|---|---|---|---|
| 50 | 50 | 0 | 19.5 | 1.0% | 1.7% | 2.0% | 0.5% of a core |
| 300 | 302 | 0 | 16.8 | 3.7% | 3.7% | 7.5% | 1.3% |
| 800 | 802 | 0 | 16.7 | 7.4% | 7.9% | 15.6% | 2.5% |
| 1,500 | 1,504 | 2.5 | 83.0 | 12.4% | 16.1% | 26.7% | 4.2% |

Instance CPU is the share of all of the instance's vCPUs, from `/proc/stat` on the
instance. The whole session, with both deploys, cost less than $0.10. The bridge
estimated $0.02 for the 10-minute run itself.

**A bug the canvas found.** In the first run, after a crashed backend was marked
down, about 2.5% of new connections kept timing out for as long as the backend
stayed down. Conntrack still held ~7,000 flows pinned to it (15 s idle timeout), and
the clients' kernels reused source ports within that time. Each new connection's
SYN matched a stale entry, and `maglev-lb` dropped it as `backend_unhealthy`. Now a
SYN whose pinned backend is gone is treated as the new connection it is: it gets a
fresh Maglev pick and the entry is re-pinned. Packets of established connections to
a dead backend are still dropped. Rerun under the same 1,200 req/s: failures only
in the 3 s before detection, then none for the 19 s the backend stayed down.

### Grafana for the canvas

The canvas profile's 8 instances use the whole 16-vCPU quota, so there is no room
for the monitoring host. Grafana and Prometheus run in Docker on the machine that
runs the bridge instead, listening on 127.0.0.1 only:

```sh
cloud/canvas.sh run        # as usual, and open the canvas
cloud/canvas.sh grafana    # another terminal; then http://localhost:3001
cloud/canvas.sh grafana-down
```

The first `grafana` pulls `grafana/grafana:11.3.0` and `prom/prometheus:v2.55.0`.
Prometheus scrapes the bridge every 2 s at `127.0.0.1:9109/metrics`
(`bridge/promexport.py`, `--metrics-port`, 0 turns it off): read-only, no token,
Host-checked, rendered from the same snapshot the canvas gets, so the two always
agree. The dashboard, **"L4 Maglev — live canvas on AWS"**, has five rows:

- **Right now**: offered load, OK and failed requests, system p99, pinned
  connections, Maglev table generation, instances running, estimated spend.
- **Traffic**: what the canvas shows, over time, per client and per backend.
- **maglev-lb**: packets, conntrack hits and misses, pinned connections per
  backend, each backend's health in `maglev-lb` (up, draining, down), drops, CPU.
- **EC2 instances, measured on each instance**: CPU and bytes sent per instance
  (`host_agent.py`), the instance list, the cost estimate, budget and time left.
- **AWS's own view: CloudWatch**: the same instances' CPU, bytes and packets sent,
  as the hypervisor measured them.

The CloudWatch row is read by the bridge (`bridge/cloudwatch.py`) with
`CanvasOperatorRole`, which may call `cloudwatch:GetMetricData` and nothing else
new, so Grafana holds no AWS credentials. It asks only while Prometheus has scraped
in the last 2 minutes, only about running instances, and for the 5-minute metrics
only every 5 minutes. It is late by a minute or more, and only the LB has 1-minute
metrics. `canvas.sh run` redeploys the template, so the permission arrives on the
first run after pulling this; until then the row says "not reading".

The canvas still has to be open: with no canvas tab the load pauses after 10 s and
everything stops 10 minutes later, whatever Grafana is doing.

## Files

| File | What it is |
|---|---|
| `cloud/stack.yaml` | CloudFormation: VPC, subnet, VIP route, security group, instances, IAM roles, flow logs |
| `cloud/lib.sh` | Shared helpers: stack outputs, SSH, starting and stopping remote processes, measurement windows |
| `cloud/up.sh`, `down.sh`, `status.sh` | Create/update, delete, and check for leftovers |
| `cloud/deploy.sh`, `cloud/hostnet/*.sh` | Install, ship code, build, per-host network setup |
| `cloud/check-bringup.sh` | Hop-by-hop bring-up checks with `tcpdump` on each host |
| `cloud/run-demo.sh` | The streaming demo on real hosts |
| `cloud/run-rr-bench.sh`, `rrstats.py` | Request/response sweep and its summarizer |
| `cloud/dsr-proof.sh`, `referee.py` | AWS as referee |
| `cloud/run-cloud-smoke-test.sh` | All of it, with assertions |
| `cloud/canvas.sh`, `bridge/cloud_bridge.py`, `cloud_hosts.py`, `lb_agent.py`, `host_agent.py` | The live canvas on AWS |
| `bridge/promexport.py`, `cloudwatch.py`, `monitoring/docker-compose-canvas.yml`, `prometheus-canvas.yml`, `grafana-canvas/` | The canvas's Grafana dashboard, run on your machine |
| `cloud/tests/` | Unit tests (no AWS needed): `python3 -m unittest discover -s cloud/tests` |
| `monitoring/docker-compose-cloud.yml`, `prometheus-cloud.yml`, `grafana-cloud/` | The monitoring stack on mon, with the two cloud dashboards |

## Results

### Bring-up (minimal profile, 2026-11-03, us-east-1, 3 × t3.micro)

Ubuntu 24.04, kernel 7.0.0-1014-aws, Python 3.12.3. After two fixes to the
scripts (below; neither was in `maglev-lb`), `cloud/check-bringup.sh` passed
every check:

| Check | Result |
|---|---|
| Bridge unit tests on the hosts' Python | 48 pass on Python 3.12.3 |
| GRO off on the LB | off |
| ARP to the backend from `maglev-lb`'s raw socket | 1 of 1 resolved |
| Health checks over the VPC | 1 of 1 eligible |
| Client → VIP reaches the LB (route + source/dest check) | 3,796 packets seen on lb (`tcpdump`) |
| GRE from the LB reaches the backend (protocol 47 through the SG) | 3,798 seen on be1 |
| Replies from the VIP reach the client (backend source/dest check) | 3,798 seen on the client |
| Requests end to end, 50 req/s for 20 s, a new connection each | 871 ok, 0 failed; p50 7.2 ms, p90 11.4 ms, p99 16.7 ms |

The referee on the same 20 seconds (`cloud/dsr-proof.sh`):

| Check | AWS's numbers | Verdict |
|---|---|---|
| The LB sends no reply traffic | 0 bytes from the VIP or to the client; 4,980 GRE packets out | PASS |
| The client gets its replies from the VIP | 4,957 packets, 276 KB | PASS |
| The backends sent them | be1 `NetworkPacketsOut` 5,224 ≥ 4,957 | PASS |
| `maglev-lb`'s forwarded count vs AWS's GRE count | 4,957 vs 4,980 (0.46% apart) | PASS |
| AWS saw at least that much leave the LB | `NetworkPacketsOut` 6,069 ≥ 4,957 | PASS |
| Backends' own flow logs of their replies | none; 3 `SKIPDATA` records on be1 | INFO |

On request/response traffic the "DSR ratio" is 0.6x: each reply is a few bytes,
so the LB's GRE overhead outweighs it. It is the streaming demo, with large
replies, where the ratio shows what DSR saves.

Script bugs found on AWS: `rstart` backgrounded `mkdir && cd && nohup cmd &` as
a whole subshell, which held SSH's output open, so the first remote start never
returned; and the CloudWatch fetch asked for a 21-second window with 60 and
300-second periods, which contains no datapoint.

### Full deployment (2026-11-05, us-east-1)

`c7i-flex.large` LB and client, `t3.micro` backends and mon (`unlimited` credits).
Smoke test, demo and sweep together kept the stack up for 50 minutes.

**Failure handling, under real load (`cloud/run-cloud-smoke-test.sh`):**

| Scene | Result |
|---|---|
| Bring-up across 4 backends | Every check passes; Maglev spreads 832 requests over all 4 |
| Crash: SIGKILL a backend at 100 req/s | Marked down in 1.0 s; table regenerated; 41 of 4,418 requests failed, none in the last 15 s |
| Restart | Health-checked back in |
| Graceful drain at 100 req/s | 0 of 3,032 requests failed, 0 `drops_backend_unhealthy`; its assigned-flow count stayed at 1,789 for 12 s |

**The streaming demo (`cloud/run-demo.sh`), 40 concurrent viewers:** a crash
regenerated the table in 1.1 s, a drain caused 0 drops, and be4 picked up traffic
after the scale-out. `viewer_gen` completed 430 sessions and received 992 MB, with
time to first byte p50 1.4 ms and p99 4.0 ms. It records a clean close as
`content_end`, so that count includes sessions the crash cut short. The last table
regeneration took 692 µs. Every ENA allowance counter on every host stayed at 0.

**AWS's verdict on that demo (`cloud/dsr-proof.sh`):**

| Claim | AWS's numbers | Verdict |
|---|---|---|
| The LB sends no replies | 0 bytes from the VIP or to the client | PASS |
| The client gets its replies from the VIP | 698,117 packets, 981 MB | PASS |
| The backends sent them | `NetworkPacketsOut` 702,442 in total, ≥ 698,117 | PASS |
| `maglev-lb`'s forwarded count vs AWS's GRE count | 124,744 vs 124,849: **0.08% apart** | PASS |
| AWS saw at least that much leave the LB | `NetworkPacketsOut` 131,110 ≥ 124,744 | PASS |
| DSR ratio | **99×**: the client received 99 bytes for each byte the LB sent | INFO |

**Request/response sweep (`cloud/run-rr-bench.sh`):** every request is a new TCP
connection, so a new flow through the Maglev lookup and the conntrack insert.

| Offered req/s | OK req/s | Failed | p50 ms | p99 ms | LB datapath CPU | LB drops/s | ENA hits |
|---|---|---|---|---|---|---|---|
| 100 | 104 | 0 | 4.5 | 9.0 | 0.7% of a core | 0 | 0 |
| 500 | 496 | 0 | 4.2 | 11.4 | 1.6% | 0 | 0 |
| 1,000 | 1,012 | 0 | 3.9 | 8.4 | 2.6% | 0 | 0 |
| 2,000 | 2,002 | 0 | 3.9 | 24.5 | 4.4% | 0 | 0 |
| 3,000 | 3,010 | 0 | 3.6 | 7.2 | 6.1% | 0 | 0 |
| 4,000 | 4,000 | 0 | 3.6 | 7.2 | 7.6% | 0 | 0 |
| 6,000 | 5,986 | 0 | 3.6 | 7.2 | 10.1% | 0 | 0 |
| 8,000 | 7,990 | 0 | 4.2 | 228 | 12.3% | 0 | 0 |

The rows up to 3,000 come from one sweep and the last three from a second one on
the same stack. The LB is nowhere near its limit at the top of the table. p99 rises
at 8,000 with the LB at 12% of a core and no ENA hits, and the load generator's own
lag reached 17.5 ms, which points at the Python client and backends (not measured
separately). So this table bounds the LB's cost per connection; it does not measure
its ceiling.

**One unexplained failure.** The first sweep's 4,000 req/s step had 14,086 timeouts,
all on be1: that server answered nothing from about 3 s into the step, while its
kernel kept completing handshakes. `maglev-lb` behaved correctly and could not have
known. Its health check is a TCP connect, which passed (0 failures, table generation
unchanged), and it kept forwarding. Timeouts then faded because the client's abandoned
sockets parked about 17,500 ports, the third that hash to be1, so new connections
stopped using them. Four more runs at 4,000 req/s (two of them back to back on one
server) had 0 failures, and be1's log had already been overwritten, so the cause is
unknown. The table's 4,000 row is from the later runs.

Script bugs found on this run: the referee's ±90 s margin and the dashboard's and
referee's 1-minute packet counts (both in [What AWS forced](#what-aws-forced)), and
`run-rr-bench.sh` read ENA counters on the LB and client but not the backends.

## Limits

- One availability zone, one subnet, one LB. Multi-AZ would need `maglev-lb` to
  route via a gateway, which it does not do yet.
- `maglev-lb`'s health check is a TCP connect, so a backend whose application hangs
  while its kernel still accepts connections stays in the table. The sweep hit exactly
  this once (see [Results](#results)). An application-level check would catch it.
- Burstable instances and shared tenancy make results noisy run to run. Compare
  runs on the same instance type, and check the credit and allowance columns
  before trusting a number.
- On AWS the canvas runs at most 5 backends and 2 clients, the pool that fits the
  16-vCPU quota. A higher quota and more instances in `cloud/stack.yaml` would raise
  that; nothing in the bridge is sized to it. The canvas profile has no monitoring
  host and no flow logs, so there is no referee there, and its Grafana runs on your
  machine (`cloud/canvas.sh grafana`).
- The canvas's spend figure is an estimate from list prices: it leaves out T3 CPU
  above baseline in `unlimited` mode ($0.05 per vCPU-hour). AWS's bill is the real
  number.
