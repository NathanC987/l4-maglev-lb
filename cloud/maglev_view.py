#!/usr/bin/env python3
"""maglev-view: maglev-lb's live Maglev and conntrack tables in a terminal.

Made for the AWS console: on the LB instance, Connect -> Session Manager, then
type `maglev-view` (cloud/deploy.sh installs it in /usr/local/bin). It works in
any terminal on a host where maglev-lb runs, and reads the same document the
canvas's tables page does: GET /tables on maglev-lb's metrics port
(src/ui/inspector.c), once a second. maglev-lb copies its tables only while
someone is asking, so this costs it about a millisecond a second, and nothing
once you quit.

The grid shows as many consecutive slots as fit, one square each ([ and ] page
through the rest); the shares under it count every slot. Keys: q quit, m
show/hide the slots the last rebuild moved, 1-9 show one backend only, 0 all.

Standard library only: the host has python3 and nothing else of ours installed.
"""
import argparse
import base64
import json
import math
import os
import re
import select
import shutil
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:9105/tables"

# The canvas's backend hues (breakscale src/live/live.css, --lt-be-1..5), as the
# nearest xterm-256 colours: blue, orange, green, amber, pink.
COLORS = [32, 202, 36, 214, 211]
EMPTY, FADED, MOVED, WARN, DIM = 238, 236, 231, 214, 245
GLYPHS = "123456789"  # --no-color: a backend's grid cells are its number (its key)


# --- the document ---------------------------------------------------------------------
def fetch(url, timeout=2.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        doc = json.loads(r.read())
    if not isinstance(doc, dict):
        raise ValueError("/tables did not return an object")
    return doc


def decode(b64):
    """One byte per slot: backend id + 1, 0 for an empty slot."""
    return base64.b64decode(b64)


def host_of(addr):
    """be<N> for the canvas's backend addresses (10.99.1.31..35), else ''."""
    try:
        a, b, c, d = (int(x) for x in addr.split("."))
    except (AttributeError, ValueError):
        return ""
    return f"be{d - 30}" if (a, b, c) == (10, 99, 1) and 31 <= d <= 39 else ""


def table_ids(table):
    return sorted({v - 1 for v in set(table) if v > 0})


def diff(before, after):
    """What one rebuild changed, or None if the sizes differ (maglev-lb restarted
    with another table size)."""
    if len(before) != len(after):
        return None
    was, now = set(table_ids(before)), set(table_ids(after))
    moved = bytearray(len(after))
    count = between = 0
    for i, (x, y) in enumerate(zip(before, after)):
        if x != y:
            moved[i] = 1
            count += 1
            if x > 0 and (x - 1) in now:
                between += 1
    return {"moved": moved, "count": count, "between": between,
            "added": sorted(now - was), "removed": sorted(was - now)}


def modulo_moved(before_ids, after_ids, m):
    """How many slots picking a backend by hash mod N would have moved instead."""
    if not before_ids or not after_ids:
        return m
    nb, na = len(before_ids), len(after_ids)
    return sum(1 for i in range(m) if before_ids[i % nb] != after_ids[i % na])


def fmt(n):
    return f"{int(n):,}"


def pct(n, of):
    return f"{100.0 * n / of:.1f}%" if of else "0%"


def describe(c, m, from_gen, to_gen, name):
    """The same sentence the canvas's tables page writes (breakscale src/live/tables.ts)."""
    head = f"Generation {fmt(from_gen)} → {fmt(to_gen)}: "
    share = f"{fmt(c['count'])} of {fmt(m)} slots ({pct(c['count'], m)}) changed owner"
    mod = f" Picking a backend by hash mod N instead would have moved {pct(c['modulo'], m)}."
    names = " and ".join(name(i) for i in (c["removed"] or c["added"]))
    if c["removed"] and not c["added"]:
        return (f"{head}{names} left. {share}: the slots {'it' if len(c['removed']) == 1 else 'they'} had, "
                "handed to the others"
                + (f", plus {fmt(c['between'])} that moved between backends that stayed."
                   if c["between"] else ". No other slot moved.") + mod)
    if c["added"] and not c["removed"]:
        return (f"{head}{names} joined. {share}: the share it took from the others"
                + (f", plus {fmt(c['between'])} that moved between the others."
                   if c["between"] else ". No other slot moved.") + mod)
    return f"{head}{share}.{mod}"


_ESC = re.compile(r"(\x1b\[[0-9;]*m)")


def clip(line, width):
    """line cut to width visible characters, colour codes kept (a line wider than
    the terminal would wrap and push everything under it down)."""
    out, n = [], 0
    for part in _ESC.split(line):
        if _ESC.fullmatch(part):
            out.append(part)
        elif n < width:
            out.append(part[: width - n])
            n += len(part[: width - n])
    return "".join(out)


def wrap(text, width):
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    return lines + ([line] if line else [])


# --- the view -------------------------------------------------------------------------
class View:
    def __init__(self, color=True):
        self.color = color
        self.doc = None
        self.table = None
        self.gen = None
        self.change = None       # (diff, from_gen, to_gen)
        self.show_moved = True
        self.focus = None
        self.page = 0
        self.names = {}          # backend id -> "be1 10.99.1.31", kept after it leaves
        self.status = "Asking maglev-lb for a copy of its tables…"

    def update(self, doc):
        if doc.get("pending"):
            self.status = "maglev-lb is making its first copy…"
            return
        table = decode(doc["table"]) if "table" in doc else self.table
        if table is None:
            return
        if self.table is not None and table != self.table:
            c = diff(self.table, table)
            if c is not None:
                c["modulo"] = modulo_moved(table_ids(self.table), table_ids(table), len(table))
                self.change = (c, self.gen, doc.get("generation"))
                self.show_moved = True
        for b in doc.get("backends", []):
            host = host_of(b.get("addr"))
            self.names[b["id"]] = f"{host} {b['addr']}" if host else b.get("addr", f"backend {b['id']}")
        self.doc, self.table, self.gen = doc, table, doc.get("generation")
        self.status = None

    def name(self, bid):
        return self.names.get(bid, f"backend {bid + 1}")

    # -- colour helpers --
    def fg(self, c, text):
        return f"\x1b[38;5;{c}m{text}\x1b[39m" if self.color else text

    def bold(self, text):
        return f"\x1b[1m{text}\x1b[22m" if self.color else text

    def swatch(self, bid):
        # Without colour the key number in front already names it on the grid.
        return self.fg(COLORS[bid % len(COLORS)], "■") if self.color else ""

    def cell(self, slot, moved):
        """The colour (or, without colour, the character) of one sampled slot."""
        if slot >= len(self.table):
            return None
        v = self.table[slot]
        if moved is not None and moved[slot]:
            return MOVED if self.color else "*"
        if v == 0:
            return EMPTY if self.color else "."
        if self.focus is not None and v - 1 != self.focus:
            return FADED if self.color else " "
        return COLORS[(v - 1) % len(COLORS)] if self.color else GLYPHS[(v - 1) % len(GLYPHS)]

    # -- the frame --
    def frame(self, width, height):
        width, height = max(40, width), max(16, height)
        out = []
        clock = time.strftime("%H:%M:%S")
        keys = "q quit · m moved slots · 1-9 one backend · 0 all · [ ] pages"
        title = self.bold("maglev-lb: Maglev and conntrack tables")
        out.append(f"{title}  {self.fg(DIM, clock)}")
        if self.status or self.doc is None:
            out += ["", *wrap(self.status or "", width)]
            return [clip(ln, width) for ln in out[:height]]
        d, m = self.doc, len(self.table)
        ct = d.get("conntrack", {})
        out.append(self.fg(DIM, f"Generation {fmt(d.get('generation', 0))} · {fmt(m)} slots · "
                                f"copied in {ct.get('scanUs', 0) / 1000:.1f} ms · {keys}"))

        backends = self.backend_rows()
        # Connections pinned elsewhere than their slot now points come first: they
        # are the point of the table, and a short window would cut them off.
        flows = sorted(ct.get("rows", []), key=lambda r: (not self.differs(r), r.get("idleMs", 0)))
        change_lines = self.change_lines(width)
        fixed = 2 + 2 + 1 + len(backends) + 1 + len(change_lines) + 1 + 3
        grid_lines = max(4, min(28, height - fixed - min(len(flows), 8)))
        out += self.grid(width, grid_lines)

        out.append(self.bold("BACKENDS") + self.fg(DIM, "  share of the table · slots · connections pinned"))
        out += backends
        out.append("")
        out += change_lines
        out.append("")
        out += self.conntrack(width, max(0, height - len(out) - 3), ct, flows)
        # A window too small for everything loses the bottom and the right.
        return [clip(ln, width) for ln in out[:height]]

    def grid(self, width, lines):
        """A run of consecutive slots, exactly, one square each. (Sampling every k-th
        slot instead draws stripes that are not there: Maglev fills its table in
        strides, and a fixed sampling step lines up with them.)"""
        cols = width - 2
        per_line = 2 if self.color else 1
        m = len(self.table)
        page = cols * lines * per_line
        pages = max(1, math.ceil(m / page))
        self.page = min(self.page, pages - 1)
        first = self.page * page
        shown = min(page, m - first)
        moved = None
        if self.show_moved and self.change and self.change[2] == self.gen:
            moved = self.change[0]["moved"]
        caption = (f"Slots {fmt(first)}-{fmt(first + shown - 1)} of {fmt(m)}, one square each, left to right"
                   + (f" (page {self.page + 1} of {pages}: [ and ] for the others)" if pages > 1 else ""))
        out = [self.fg(DIM, caption)]

        def at(r, c):
            i = r * cols + c
            return self.cell(first + i, moved) if i < shown else None

        rows = math.ceil(shown / cols)
        if not self.color:
            for r in range(rows):
                out.append("  " + "".join(at(r, c) or " " for c in range(cols)))
            return out
        for r in range(0, rows, 2):
            line, last = ["  "], None
            for c in range(cols):
                top, bot = at(r, c), at(r + 1, c)
                if top is None:
                    line.append("\x1b[0m ")
                    last = None
                    continue
                key = (top, bot)
                if key != last:
                    line.append(f"\x1b[38;5;{top}m" + (f"\x1b[48;5;{bot}m" if bot is not None else "\x1b[49m"))
                    last = key
                line.append("▀")
            out.append("".join(line) + "\x1b[0m")
        return out

    def backend_rows(self):
        d, m = self.doc, len(self.table)
        ct = d.get("conntrack", {})
        pinned = {f["id"]: f["n"] for f in ct.get("flows", [])}
        in_table = {b["id"]: b for b in d.get("backends", [])}
        out = []
        for bid in sorted(set(in_table) | set(pinned)):
            b = in_table.get(bid)
            slots = b["slots"] if b else 0
            bar_n = round(16 * slots / m) if m else 0
            bar = self.fg(COLORS[bid % len(COLORS)], "█" * bar_n) + self.fg(EMPTY, "░" * (16 - bar_n)) \
                if self.color else "#" * bar_n + "." * (16 - bar_n)
            note = ""
            if not b:
                note = "  left the table"
            elif slots == 0:
                note = "  draining: no new connections"
            key = f"{bid + 1}" if bid < 9 else " "
            focus = "›" if self.focus == bid else " "
            out.append(f"{focus}{key} {self.swatch(bid)} {self.name(bid):<18} {bar} {pct(slots, m):>6} "
                       f"{fmt(slots):>7} slots {fmt(pinned.get(bid, 0)):>7} pinned{note}")
        return out

    def change_lines(self, width):
        if not self.change or self.change[2] != self.gen:
            return [self.fg(DIM, x) for x in wrap(
                "No rebuild since this viewer started. Add, remove or drain a backend on the canvas "
                "and watch: the slots that changed owner light up white.", width)]
        c, a, b = self.change
        text = describe(c, len(self.table), a or 0, b or 0, self.name)
        lines = wrap(text, width)
        if self.show_moved and self.color:
            lines.append(self.fg(MOVED, "▀") + self.fg(DIM, " white: the slots it moved (m hides them)"))
        return [self.bold("LAST REBUILD")] + lines

    def owner(self, slot):
        return self.table[slot] - 1 if 0 <= slot < len(self.table) else -1

    def differs(self, r):
        return self.owner(r["slot"]) != r["id"]

    def conntrack(self, width, room, ct, flows):
        out = [self.bold("CONNECTION TRACKING") + self.fg(
            DIM, f"  {fmt(ct.get('active', 0))} connections pinned · rows: the "
                 f"{ct.get('rowsPerBackend', 0)} most recently active per backend")]
        if not flows:
            return out + [self.fg(DIM, "No connections yet. Turn the load up on the canvas.")]
        out.append(self.fg(DIM, f"  {'client':<21}{'slot':>7}  {'pinned to':<19}{'slot points to now':<19}{'idle':>6}"))
        shown = flows[:max(1, room - 2)]
        for r in shown:
            owner = self.owner(r["slot"])
            differs = owner != r["id"]
            now = ("no backend" if owner < 0 else self.name(owner)) if differs else "same"
            line = (f"  {r['src'] + ':' + str(r['sport']):<21}{fmt(r['slot']):>7}  "
                    f"{self.name(r['id']):<19}{now:<19}{r.get('idleMs', 0) / 1000:>5.1f}s")
            if differs:
                line = self.fg(WARN, line + "  the pin wins") if self.color else line + "  ! the pin wins"
            out.append(line)
        if len(flows) > len(shown):
            out.append(self.fg(DIM, f"  … and {len(flows) - len(shown)} more (a taller window shows them)"))
        return out


# --- the terminal ---------------------------------------------------------------------
def run(url, color):
    import termios
    import tty
    view = View(color)
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    w = sys.stdout.write
    w("\x1b[?1049h\x1b[?25l")
    try:
        tty.setcbreak(fd)
        next_fetch = 0.0
        while True:
            now = time.monotonic()
            if now >= next_fetch:
                try:
                    view.update(fetch(url))
                except (urllib.error.URLError, OSError, ValueError) as e:
                    view.doc = None
                    view.status = (f"maglev-lb is not answering on {url} ({getattr(e, 'reason', e)}). "
                                   "On the canvas it runs only while `cloud/canvas.sh run` is going and the "
                                   "canvas has a load balancer. Trying again every second.")
                next_fetch = now + 1.0
            size = shutil.get_terminal_size((100, 40))
            lines = view.frame(size.columns, size.lines)[: size.lines]
            w("\x1b[H" + "\x1b[K\n".join(lines) + "\x1b[K\x1b[J")
            sys.stdout.flush()
            ready, _, _ = select.select([fd], [], [], max(0.0, next_fetch - time.monotonic()))
            if ready:
                key = os.read(fd, 8).decode(errors="ignore")[:1]
                if key in ("q", "Q", "\x03", "\x04"):
                    return 0
                if key in ("m", "M"):
                    view.show_moved = not view.show_moved
                elif key.isdigit():
                    view.focus = None if key == "0" else int(key) - 1
                elif key == "]":
                    view.page += 1
                elif key == "[":
                    view.page = max(0, view.page - 1)
    except KeyboardInterrupt:
        return 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        w("\x1b[0m\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--url", default=DEFAULT_URL, help=f"maglev-lb's /tables (default {DEFAULT_URL})")
    p.add_argument("--once", action="store_true", help="print one frame and exit")
    p.add_argument("--no-color", action="store_true", help="plain characters instead of colours")
    p.add_argument("--size", help="COLSxLINES for --once (default: this terminal)")
    a = p.parse_args(argv)
    color = not a.no_color and "NO_COLOR" not in os.environ
    if a.once or not sys.stdin.isatty() or not sys.stdout.isatty():
        view = View(color)
        try:
            view.update(fetch(a.url))
            if view.doc is None and not view.status.startswith("Asking"):
                time.sleep(1.2)  # the first ask starts maglev-lb copying
                view.update(fetch(a.url))
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"maglev-view: cannot read {a.url}: {getattr(e, 'reason', e)}", file=sys.stderr)
            return 1
        cols, lines = (int(x) for x in a.size.split("x")) if a.size else shutil.get_terminal_size((100, 40))
        print("\n".join(view.frame(cols, lines)) + ("\x1b[0m" if color else ""))
        return 0
    return run(a.url, color)


if __name__ == "__main__":
    sys.exit(main())
