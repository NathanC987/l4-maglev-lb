"""The EC2 instances behind the AWS canvas: which should be running, and getting them
there. Started and stopped in batches on the bridge's asyncio loop, with AWS and SSH
work off on threads so a slow call never stalls the 4 Hz stream. The AWS and SSH side
are injected (cloud_bridge.AwsCli and .HostPrep in production, fakes in
bridge/tests/test_cloud_hosts.py), and the clock is passed in on each tick.

A host's life:  stopped -> starting -> ready -> stopping -> stopped
"starting" spans the EC2 boot and then preparing the host over SSH (network setup, and
the lease below). A host nobody wants stays up for stop_grace_s first: EC2 bills at
least a minute per start, and undo on the canvas should be instant.

The lease is what bounds the cost if this laptop dies: each host is told to shut itself
down in lease_min minutes, and only the running bridge keeps pushing that back.
"""
import asyncio
import time

import livecommon as lc

POLL_BUSY_S = 3.0        # describe-instances while something is changing...
POLL_IDLE_S = 60.0       # ...and otherwise, to notice a host stopping by itself
RETRY_AWS_S = 15.0
PREPARE_RETRY_S = 5.0
PREPARE_GIVE_UP_S = 300.0
LEASE_RENEW_S = 300.0
STOP_WAIT_S = 120.0

# us-east-1 on-demand USD per hour, with the $0.005 public IPv4 charge. For the
# on-screen estimate only: AWS's bill is the real number, and burst above a T3's
# baseline in unlimited mode ($0.05 per vCPU-hour) is not in here.
HOURLY_USD = {"t3.micro": 0.0154, "t3.small": 0.0258, "t2.micro": 0.0166,
              "c7i-flex.large": 0.0898, "m7i-flex.large": 0.1008}
UNKNOWN_TYPE_USD = 0.25
EBS_USD_PER_HOUR = 8 * 0.08 / 730  # an 8 GB gp3 root volume, billed even while stopped

ACTIVE = ("pending", "running")


class Host:
    def __init__(self, info):
        self.role = info["role"]
        self.id = info["id"]
        self.private_ip = info["privateIp"]
        self.mac = info["mac"]
        self.type = info["type"]
        self.state = info["state"]        # EC2's own: pending, running, stopping, stopped...
        self.public_ip = info.get("publicIp")
        self.wanted = False
        self.unwanted_since = None
        self.prepared = False
        self.preparing = False
        self.prepare_since = None
        self.prepare_after = 0.0
        self.lease_at = 0.0
        self.note = None

    @property
    def phase(self):
        if self.state == "running" and self.prepared:
            return "ready"
        if self.state in ACTIVE:
            return "starting" if self.wanted else "stopping"
        if self.state in ("stopping", "shutting-down"):
            return "stopping"
        return "stopped"

    @property
    def usd_per_hour(self):
        return HOURLY_USD.get(self.type, UNKNOWN_TYPE_USD)


class HostManager:
    def __init__(self, aws, prep, infos, event, stop_grace_s=60.0, lease_min=20):
        self.aws = aws
        self.prep = prep
        self.event = event
        self.stop_grace_s = stop_grace_s
        self.lease_min = lease_min
        self.hosts = {i["role"]: Host(i) for i in infos}
        self.aws_busy = False
        self.aws_retry_at = 0.0
        self.next_poll = 0.0
        self.last_meter = None
        self.spent_usd = 0.0

    # --- views -------------------------------------------------------------------
    def ready(self, role):
        h = self.hosts.get(role)
        return h is not None and h.phase == "ready"

    def usd_per_hour(self):
        """Roughly what the current state costs per hour."""
        compute = sum(h.usd_per_hour for h in self.hosts.values() if h.state in ACTIVE)
        return compute + EBS_USD_PER_HOUR * len(self.hosts)

    def active(self):
        return [h for h in self.hosts.values() if h.state not in ("stopped", "terminated")]

    def mark_unreachable(self, role):
        """A channel to this host broke: prepare it again (which reconnects), whether
        the network blinked or its address changed."""
        h = self.hosts.get(role)
        if h is not None and h.prepared:
            h.prepared = False
            self.next_poll = 0.0

    # --- the loop ----------------------------------------------------------------
    def tick(self, now, wanted):
        self._meter(now)
        for h in self.hosts.values():
            h.wanted = h.role in wanted
            if h.wanted:
                h.unwanted_since = None
            elif h.unwanted_since is None:
                h.unwanted_since = now
        if not self.aws_busy and now >= self.aws_retry_at:
            to_start = [h for h in self.hosts.values() if h.wanted and h.state == "stopped"]
            to_stop = [h for h in self.hosts.values() if not h.wanted and h.state == "running"
                       and now - h.unwanted_since >= self.stop_grace_s]
            if to_start or to_stop:
                self.aws_busy = True
                lc.spawn(self._change(now, to_start, to_stop))
            elif now >= self.next_poll:
                self.aws_busy = True
                lc.spawn(self._refresh(now))
        for h in self.hosts.values():
            if h.state == "running" and h.wanted and not h.prepared and not h.preparing \
                    and h.public_ip and now >= h.prepare_after:
                h.preparing = True
                h.prepare_since = h.prepare_since or now
                lc.spawn(self._prepare(h, now))
            elif h.phase == "ready" and now >= h.lease_at:
                h.lease_at = now + LEASE_RENEW_S
                lc.spawn(asyncio.to_thread(self._renew_lease, h))

    def _meter(self, now):
        if self.last_meter is not None:
            self.spent_usd += self.usd_per_hour() * (now - self.last_meter) / 3600.0
        self.last_meter = now

    def _busy(self):
        return any(h.state in ("pending", "stopping", "shutting-down")
                   or (h.wanted and h.state == "stopped") for h in self.hosts.values())

    async def _change(self, now, to_start, to_stop):
        try:
            if to_start:
                ids = [h.id for h in to_start]
                await asyncio.to_thread(self.aws.start, ids)
                for h in to_start:
                    h.state = "pending"
                self.event(f"AWS: starting {', '.join(h.role for h in to_start)}")
            if to_stop:
                ids = [h.id for h in to_stop]
                for h in to_stop:
                    h.prepared = False
                    await asyncio.to_thread(self.prep.disconnect, h)
                await asyncio.to_thread(self.aws.stop, ids)
                for h in to_stop:
                    h.state = "stopping"
                self.event(f"AWS: stopping {', '.join(h.role for h in to_stop)}")
            self.next_poll = 0.0
        except Exception as e:  # the CLI's errors are many; the loop must survive them all
            self.aws_retry_at = now + RETRY_AWS_S
            self.event(f"AWS call failed, will retry: {e}", "error")
        finally:
            self.aws_busy = False

    async def _refresh(self, now):
        try:
            infos = await asyncio.to_thread(self.aws.describe)
            for info in infos:
                h = self.hosts.get(info["role"])
                if h is None:
                    continue
                if h.state != info["state"] and info["state"] in ("stopped", "terminated"):
                    h.prepared = False
                    await asyncio.to_thread(self.prep.disconnect, h)
                    if h.state == "running":
                        self.event(f"AWS: {h.role} stopped by itself", "warn")
                h.state, h.public_ip = info["state"], info.get("publicIp")
            self.next_poll = now + (POLL_BUSY_S if self._busy() else POLL_IDLE_S)
        except Exception as e:
            self.aws_retry_at = now + RETRY_AWS_S
            self.event(f"AWS call failed, will retry: {e}", "error")
        finally:
            self.aws_busy = False

    async def _prepare(self, h, now):
        try:
            await asyncio.to_thread(self.prep.prepare, h, self.lease_min)
            h.prepared = True
            h.lease_at = now + LEASE_RENEW_S
            h.prepare_since = None
            self.event(f"AWS: {h.role} ready")
            self.next_poll = 0.0
        except Exception as e:
            # SSH takes a moment to answer on a fresh boot; only a long failure matters.
            h.prepare_after = now + PREPARE_RETRY_S
            if now - h.prepare_since > PREPARE_GIVE_UP_S:
                h.note = str(e)
                h.prepare_since = now
                self.event(f"AWS: cannot reach {h.role} over SSH: {e}", "error")
        finally:
            h.preparing = False

    def _renew_lease(self, h):
        try:
            self.prep.renew_lease(h, self.lease_min)
        except Exception as e:
            self.event(f"AWS: could not renew {h.role}'s lease: {e}", "warn")

    # --- shutdown ----------------------------------------------------------------
    async def stop_all(self, sleep=asyncio.sleep, clock=time.monotonic):
        """Stops everything and waits to see it stopped. Returns roles still not
        stopped (their leases and dead-man switches are the backstop)."""
        for h in self.hosts.values():
            await asyncio.to_thread(self.prep.disconnect, h)
        deadline = clock() + STOP_WAIT_S
        last = {}
        while True:
            try:
                infos = await asyncio.to_thread(self.aws.describe)
            except Exception as e:
                self.event(f"AWS call failed: {e}", "error")
                infos = None
            if infos is not None:
                last = {i["role"]: i["state"] for i in infos}
                running = [i["id"] for i in infos if i["state"] == "running"]
                if running:
                    try:
                        await asyncio.to_thread(self.aws.stop, running)
                    except Exception as e:
                        self.event(f"AWS call failed: {e}", "error")
                if all(s in ("stopped", "terminated") for s in last.values()):
                    return []
            if clock() >= deadline:
                return [r for r in self.hosts if last.get(r) not in ("stopped", "terminated")]
            await sleep(POLL_BUSY_S)
