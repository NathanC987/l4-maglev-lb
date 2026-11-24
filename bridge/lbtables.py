"""maglev-lb's live Maglev and conntrack tables: GET /tables on its metrics port
(src/ui/inspector.c is the source of truth for the document), and how the bridge
hands them to the canvas (GET /api/v1/lb/tables).

The Maglev table is one byte per slot, base64: backend id + 1, or 0 for an empty
slot. At the default 65537 slots that is ~90 KB, so it travels only when its
generation changes: each hop says which generation it already holds (?gen=N), and
the far side leaves the table out when it matches."""
import json
import urllib.request


def fetch(url, gen=None, timeout=1.0):
    """maglev-lb's /tables document, or {"pending": true} until its first copy."""
    if gen is not None:
        url += f"?gen={int(gen)}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        doc = json.loads(r.read())
    if not isinstance(doc, dict):
        raise ValueError("/tables did not return an object")
    return doc


class TablesCache:
    """The last Maglev table seen, for one maglev-lb process (lb_key: its pid). A
    restarted maglev-lb numbers its generations from 1 again, so a table is only
    reused for the process it came from."""

    def __init__(self):
        self.key = self.gen = self.table = None

    def known(self, lb_key):
        """The generation to ask with, or None to ask for the table too."""
        return self.gen if lb_key == self.key and self.table is not None else None

    def merge(self, lb_key, doc):
        """doc with its table filled in, or None while there is nothing to show yet."""
        if not doc or doc.get("pending"):
            return None
        if "table" in doc:
            self.key, self.gen, self.table = lb_key, doc.get("generation"), doc["table"]
            return doc
        if lb_key == self.key and doc.get("generation") == self.gen and self.table is not None:
            return {**doc, "table": self.table}
        return None


def to_canvas(doc, node_of_lb_id, client_of_ip, client_gen, extra):
    """The canvas's version: maglev-lb's backend ids gain the canvas node that owns
    them (None for an unowned slot), client addresses gain their client node, and
    the table is left out if the canvas already holds this generation."""
    ct = doc.get("conntrack", {})
    out = {
        "generation": doc.get("generation"), "ageMs": doc.get("ageMs"), "m": doc.get("m"),
        "backends": [{"lbId": b["id"], "node": node_of_lb_id.get(b["id"]), "addr": b["addr"],
                      "slots": b["slots"]} for b in doc.get("backends", [])],
        "conntrack": {
            "active": ct.get("active", 0), "scanUs": ct.get("scanUs", 0),
            "rowsPerBackend": ct.get("rowsPerBackend", 0),
            "flows": [{"lbId": f["id"], "node": node_of_lb_id.get(f["id"]), "n": f["n"]}
                      for f in ct.get("flows", [])],
            "rows": [{"client": client_of_ip.get(r["src"]), "src": r["src"], "sport": r["sport"],
                      "dport": r["dport"], "proto": r["proto"], "lbId": r["id"],
                      "node": node_of_lb_id.get(r["id"]), "slot": r["slot"], "idleMs": r["idleMs"]}
                     for r in ct.get("rows", [])],
        },
        **extra,
    }
    if client_gen is None or client_gen != doc.get("generation"):
        out["table"] = doc.get("table")
    return out
