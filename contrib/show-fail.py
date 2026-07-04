#!/usr/bin/env python3
"""Drill-down companion to classify-failfail.py.

Given the classifier's output (a JSON array, default <RUN>/failfail-classified.json)
and a collapsed package atom (cat/pn), find the first case whose first-failed
package matches, locate that package's salvaged build log, and print the region
around the Portage failure banner. Use this to eyeball the real error behind a
cascade root surfaced by classify-failfail.py.

Usage: show-fail.py <failfail-classified.json> <cat/pn> [N]
"""
import gzip
import json
import os
import re
import sys

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
BANNER = re.compile(r"ERROR:\s+(\S+?)\s+failed\s+\((\w+)\s+phase\)")


def strip(s):
    return ANSI.sub("", s)


def read_any(path):
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", errors="replace") as fh:
                return strip(fh.read())
        with open(path, "r", errors="replace") as fh:
            return strip(fh.read())
    except OSError:
        return ""


def load_rows(path):
    with open(path) as fh:
        text = fh.read()
    text = text.lstrip()
    if text.startswith("["):
        return json.loads(text)
    # tolerate JSONL
    return [json.loads(l) for l in text.splitlines() if l.strip()]


def find_log(logdir, cp):
    pn = cp.split("/")[-1]
    cands = []
    bl = os.path.join(logdir, "build-logs", "portage-ng")
    if os.path.isdir(bl):
        for root, _, fs in os.walk(bl):
            for f in fs:
                if ".build.log" in f and pn in f:
                    cands.append(os.path.join(root, f))
    for f in os.listdir(logdir):
        if f.startswith("portage-ng.target.") and ".build.log" in f and pn in f:
            cands.append(os.path.join(logdir, f))
    return cands


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: show-fail.py <failfail-classified.json> <cat/pn> [N]")
    rows = load_rows(sys.argv[1])
    want_cp = sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    seen = 0
    for r in rows:
        if r.get("pn_first_fail_pkg", "") != want_cp:
            continue
        seen += 1
        if seen > n:
            break
        logdir = r.get("logdir", "")
        print(f"\n########## target={r['target']}  fail_pkg={r.get('pn_first_fail_pkg')}  "
              f"phase={r.get('pn_first_fail_phase')}  sig={r.get('pn_first_fail_sig')}  "
              f"cluster={r.get('cluster')}  emerge={r.get('em_reason')}")
        if not logdir or not os.path.isdir(logdir):
            print("  (no logdir)", logdir)
            continue
        logs = find_log(logdir, want_cp)
        if not logs:
            print("  (no build log found under)", logdir)
            continue
        body = read_any(sorted(logs)[-1])
        lines = body.splitlines()
        idx = None
        for i, ln in enumerate(lines):
            if BANNER.search(ln):
                idx = i
        if idx is None:
            print("\n".join(lines[-30:]))
        else:
            print("\n".join(lines[max(0, idx - 30):idx + 8]))


if __name__ == "__main__":
    main()
