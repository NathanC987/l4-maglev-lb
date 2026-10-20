#!/usr/bin/env python3
"""Fills the cloud dashboard templates' placeholders (__LB__, __BE1__, ...,
__LOG_GROUP__, __LOG_GROUP_ARN__) with this stack's instance ids and log group,
which change on every cloud/up.sh. Run on the mon host by cloud/deploy.sh:

    LB=i-... CLIENT=i-... BE1=i-... REGION=... ACCOUNT=... LOG_GROUP=... \\
        render_dashboard.py SRC_DIR DST_DIR
"""
import os
import pathlib
import sys

ROLES = ("LB", "CLIENT", "BE1", "BE2", "BE3", "BE4")


def substitutions(env):
    subs = {f"__{r}__": env.get(r) or "none" for r in ROLES}
    group = env["LOG_GROUP"]
    subs["__LOG_GROUP__"] = group
    subs["__LOG_GROUP_ARN__"] = (
        f"arn:aws:logs:{env['REGION']}:{env['ACCOUNT']}:log-group:{group}")
    return subs


def render(text, subs):
    # Longest placeholders first, so __LOG_GROUP_ARN__ is not eaten by __LOG_GROUP__.
    for k in sorted(subs, key=len, reverse=True):
        text = text.replace(k, subs[k])
    return text


def main(argv):
    if len(argv) != 3:
        sys.exit(__doc__)
    src, dst = pathlib.Path(argv[1]), pathlib.Path(argv[2])
    dst.mkdir(parents=True, exist_ok=True)
    subs = substitutions(os.environ)
    for f in sorted(src.glob("*.json")):
        (dst / f.name).write_text(render(f.read_text(), subs))
        print(f"rendered {f.name}")


if __name__ == "__main__":
    main(sys.argv)
