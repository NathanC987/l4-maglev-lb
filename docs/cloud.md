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
   each host. **VPC Flow Logs** record every flow on every network interface,
   including the source address written in each packet. That is enough to check,
   without trusting anything this project runs, that the LB really sends only
   GRE, that the replies really leave the backends with the VIP as their source,
   and that `maglev-lb`'s own count of forwarded packets matches AWS's count.
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

> **Status:** written and tested offline (unit tests, template and script checks).
> It has not yet been run on AWS; the [Results](#results) section is filled in
> from the first full run.

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
| t2/t3 instances run on CPU credits | Out of credits, a VM is throttled to 10–20% of a vCPU, and the result measures AWS, not the LB | `CPUCredits: standard` (never billed for bursting); benchmarks check `CPUCreditBalance` first |
| Each instance has a packets-per-second and bandwidth allowance | Past it, the hypervisor drops or queues packets, whatever the LB does | Runs record ENA's `*_allowance_exceeded` counters before and after |
| `maglev_lb_rx_packets_total` counts every IPv4 frame on the raw socket, including the LB host's own traffic | Comparing it with AWS's packets-in would mislead | The referee compares `tx_packets` (GRE only) with Flow Logs' GRE count |

## AWS as referee

`cloud/dsr-proof.sh` takes a measured window (the run scripts mark its start and
end, recording `maglev-lb`'s counters at each) and waits for the flow logs to
arrive, which takes a few minutes. It then fetches the CloudWatch metrics and
runs `cloud/referee.py`, which checks:

| Check | From | Passes when |
|---|---|---|
| The LB sends no reply traffic | Flow Logs, LB interface, egress | Nothing with source VIP, nothing to the client; only GRE |
| Backends reply with the VIP as source | Flow Logs, backend interfaces, egress | TCP with `pkt-srcaddr` = VIP, to the client |
| The client got what the backends sent | Flow Logs, client ingress vs backends' egress | Within 5% |
| `maglev-lb` counts its forwarding correctly | `tx_packets` delta vs Flow Logs' GRE packets out of the LB | Within 2% (inconclusive if AWS reports `SKIPDATA`) |
| DSR ratio | Client bytes received ÷ LB bytes sent | Reported |

The field that makes this possible is `pkt-srcaddr`. For traffic leaving an
interface, `srcaddr` is always the interface's own address; `pkt-srcaddr` is the
address written in the packet, which for a backend's replies is the VIP.

The Grafana dashboard **"L4 Maglev — AWS as referee"** shows the same evidence
live: AWS's packet count for the LB next to `maglev-lb`'s, bytes sent per host
(the backends send far more than the LB), a Flow Logs table of who sent what
from which address, and every instance's CPU credit balance.

## Cost: staying in the free tier

AWS changed its free tier on 2025-07-15, and the terms keep changing. **Check
https://aws.amazon.com/free for your account before the first run.**

- **Accounts created on or after 2025-07-15** are on the **Free plan**: sign-up
  credits (up to $200, for 6 months), and the account cannot be charged unless
  you upgrade it to a paid plan. Free-plan instance types include t3.micro and
  c7i-flex.large. The latter is non-burstable and the better choice for
  benchmarks (`--instance-type c7i-flex.large`).
- **Older accounts** have the legacy 12-month free tier: 750 hours a month of
  t2.micro (t3.micro in regions without t2), 30 GB of EBS, and 750 hours of
  public IPv4. Use `--instance-type t2.micro` there.

| Resource | Use | Covered by |
|---|---|---|
| EC2 | 7 instances (3 with `--profile minimal`), only while a stack is up | Instance hours (legacy: 750 h/month shared, about 100 h of the full stack), or credits |
| EBS | 7 × 8 GB root volumes, deleted with the instances | 30 GB-months (legacy), or credits |
| Public IPv4 | One per instance, for SSH and package installs | 750 h/month (legacy), or credits |
| Data transfer | All traffic between hosts uses private IPs in one AZ | Free |
| CloudWatch metrics | Basic 5-minute EC2 metrics on every host; 1-minute (7 metrics) on the LB only | Always free (basic metrics, 10 detailed metrics) |
| CloudWatch API | Grafana's CloudWatch panels (refresh 1 min), `dsr-proof.sh` | `GetMetricData` is billed at about $0.01 per 1,000 metrics, a few cents per hour of dashboard. This is the one item not strictly free. |
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

cloud/up.sh --profile full           # adds be2..be4 and mon (Prometheus + Grafana)
cloud/grafana-tunnel.sh              # then http://localhost:3000 (separate terminal)
cloud/run-demo.sh                    # streaming demo: crash, drain, scale-out + referee

cloud/up.sh --flow-logs off          # before the sweep (instances are kept)
cloud/run-rr-bench.sh                # request/response sweep: 100 → 2000 req/s
cloud/up.sh --flow-logs on

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
| `cloud/tests/` | Unit tests (no AWS needed): `python3 -m unittest discover -s cloud/tests` |
| `monitoring/docker-compose-cloud.yml`, `prometheus-cloud.yml`, `grafana-cloud/` | The monitoring stack on mon, with the two cloud dashboards |

## Results

Not yet run on AWS. This section will hold the bring-up findings, the demo's
crash/drain/scale-out timings, the referee's verdicts, and the request/response
sweep next to the local veth numbers.

## Limits

- One availability zone, one subnet, one LB. Multi-AZ would need `maglev-lb` to
  route via a gateway, which it does not do yet.
- Burstable instances and shared tenancy make results noisy run to run. Compare
  runs on the same instance type, and check the credit and allowance columns
  before trusting a number.
- The live canvas (`docs/live-mode.md`) still runs locally. Driving the cloud
  hosts from the canvas is possible future work (the bridge's reconciler already
  takes an injectable executor), but out of scope here.
