#!/usr/bin/env python3
"""Cluster fail/fail assume cases where emerge stops at plan."""
import glob
import os
import re
from collections import defaultdict

MATRIX = os.environ.get(
    "MATRIX",
    "/srv/tinderbox-ng/reports/compare-matrix-20260529T194633/results.tsv",
)
RUN_DIR = os.path.dirname(MATRIX)


def label(cp: str) -> str:
    return cp.replace("/", "_").replace("-", "_")


def load_logdirs() -> dict[str, str]:
    logdirs: dict[str, str] = {}
    for w in glob.glob(os.path.join(RUN_DIR, "*.log")):
        with open(w) as f:
            for line in f:
                m = re.search(
                    r"logs: (/srv/tinderbox-ng/logs/compare-[A-Za-z0-9_]+-\d{8}T\d{6})",
                    line,
                )
                if not m:
                    continue
                lab = re.match(r"compare-(.+)-\d{8}T", os.path.basename(m.group(1)))
                if lab:
                    logdirs[lab.group(1)] = m.group(1)
    return logdirs


def pn_fail_atom(logdir: str) -> str:
    p = os.path.join(logdir, "portage-ng.build.log")
    if not os.path.isfile(p):
        return "?"
    with open(p, errors="replace") as f:
        pm = re.findall(r"FAIL[^\n]*install portage://(\S+)", f.read())
    return pm[-1] if pm else "?"


def em_plan_reason(logdir: str) -> str:
    t = open(os.path.join(logdir, "emerge.plan.log"), errors="replace").read()
    if "no ebuilds built with USE flags" in t:
        m = re.search(r'satisfy "([^"]+)"', t)
        return f"USE: {m.group(1) if m else '?'}"
    if "slot conflict" in t:
        return "slot conflict"
    if "masked packages" in t:
        return "masked dep"
    if "unmet requirements" in t:
        m = re.search(r'satisfy "([^"]+)" has unmet', t)
        return f"REQUIRED_USE: {m.group(1) if m else 'target'}"
    m = re.search(r"!!! ([^\n]+)", t)
    return m.group(1)[:60] if m else "?"


def has_assume(logdir: str) -> bool:
    for fn in ("portage-ng.plan.log", "portage-ng.build.log"):
        p = os.path.join(logdir, fn)
        if os.path.isfile(p):
            with open(p, errors="replace") as f:
                if "assumed" in f.read():
                    return True
    return False


def main() -> None:
    logdirs = load_logdirs()
    assume_cases: list[tuple[str, str, str]] = []
    with open(MATRIX) as f:
        next(f)
        for line in f:
            p = line.strip().split("\t")
            if len(p) < 4:
                continue
            if not (p[2].startswith("FAIL") and p[3].startswith("FAIL")):
                continue
            cp = p[0]
            d = logdirs.get(label(cp))
            if not d or os.path.isfile(os.path.join(d, "emerge.build.log")):
                continue
            if not has_assume(d):
                continue
            assume_cases.append((cp, pn_fail_atom(d), em_plan_reason(d)))

    by_pn: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for cp, pn, em in assume_cases:
        by_pn[pn].append((cp, em))

    print(f"Assume cases (em plan-only fail): {len(assume_cases)}")
    for pn, items in sorted(by_pn.items(), key=lambda x: -len(x[1])):
        print(f"\n[{len(items)}] pn build dies on: {pn}")
        ems: dict[str, list[str]] = defaultdict(list)
        for cp, em in items:
            ems[em].append(cp)
        for em, cps in sorted(ems.items(), key=lambda x: -len(x[1])):
            print(f"  em: {em} ({len(cps)})")
            for cp in cps[:3]:
                print(f"    - {cp}")


if __name__ == "__main__":
    main()
