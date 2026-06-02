#!/usr/bin/env python3
"""Batch-analyze fail/fail compare-matrix rows from tinderbox-ng logs."""
import glob
import json
import os
import re
import sys
from collections import defaultdict

MATRIX = os.environ.get(
    "MATRIX",
    "/srv/tinderbox-ng/reports/compare-matrix-20260529T194633/results.tsv",
)
LOGS = os.environ.get("LOGS", "/srv/tinderbox-ng/logs")


def label(cp: str) -> str:
    return cp.replace("/", "_").replace("-", "_")


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def read_file(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""
    if path.endswith(".gz"):
        import gzip

        with gzip.open(path, "rt", errors="replace") as f:
            return strip_ansi(f.read())
    with open(path, errors="replace") as f:
        return strip_ansi(f.read())


def first_fail(text: str) -> str | None:
    for pat in (
        r"\[step \d+\] FAIL[^\n]*install portage://(\S+)",
        r"\[step \d+\] FAIL[^\n]*download portage://(\S+)",
        r"\[step \d+\] FAIL[^\n]*",
        r"\* ERROR: ([^\s]+)::",
    ):
        m = re.search(pat, text)
        if m:
            return m.group(1) if m.lastindex else m.group(0)[:100]
    return None


def emerge_failed_pkg(text: str) -> str | None:
    m = re.search(r"\(([^:]+:[^:]+)::gentoo, ebuild scheduled", text)
    if m:
        return m.group(1)
    for ln in text.splitlines():
        if "dropped because" in ln or "failed to build" in ln:
            return ln.strip()[:100]
    return None


def first_compile_error(logdir: str, engine: str) -> str | None:
    if engine == "portage-ng":
        paths = glob.glob(
            os.path.join(logdir, "build-logs/portage-ng/**/*.build.log.gz"),
            recursive=True,
        )
    else:
        paths = glob.glob(os.path.join(logdir, "emerge.target.*"))
    for path in sorted(paths):
        text = read_file(path)
        for ln in text.splitlines():
            low = ln.lower()
            if "fatal error:" in low or "error:" in low or "* error" in low:
                return f"{os.path.basename(path)}: {ln.strip()[:110]}"
    return None


def bucket(r: dict) -> str:
    cp = r["cp"]
    if cp.startswith("app-dicts/stardict"):
        return "A_stardict"
    if r.get("assume"):
        return "B_assume_verify"
    if "download" in str(r.get("pn_fail") or ""):
        return "E_fetch"
    if r.get("domain"):
        return "C_domain"
    if r.get("executor"):
        return "F_executor"
    for k in ("pn_fail", "em_fail", "pn_err", "em_err"):
        if "sdcv" in str(r.get(k) or ""):
            return "A_sdcv_dep"
    return "Z_other"


def main() -> int:
    ff: list[str] = []
    with open(MATRIX) as f:
        for line in f:
            p = line.strip().split("\t")
            if len(p) >= 4 and p[2].startswith("FAIL") and p[3].startswith("FAIL"):
                ff.append(p[0])

    by_label: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for d in glob.glob(os.path.join(LOGS, "compare-*")):
        m = re.match(r"compare-(.+)-(\d{8}T\d{6})$", os.path.basename(d))
        if m:
            by_label[m.group(1)].append((m.group(2), d))

    results: list[dict] = []
    for cp in sorted(ff):
        lab = label(cp)
        dirs = sorted(by_label.get(lab, []), reverse=True)
        logdir = dirs[0][1] if dirs else None
        r: dict = {"cp": cp, "log": os.path.basename(logdir) if logdir else None}
        if not logdir:
            results.append(r)
            continue
        pn = read_file(os.path.join(logdir, "portage-ng.build.log"))
        em = read_file(os.path.join(logdir, "emerge.build.log"))
        pn_plan = read_file(os.path.join(logdir, "portage-ng.plan.log"))
        r["pn_fail"] = first_fail(pn)
        r["em_fail"] = first_fail(em) or emerge_failed_pkg(em)
        r["assume"] = "assumed running" in pn or "assumed running" in pn_plan
        r["domain"] = "domain assumptions" in pn_plan
        r["executor"] = "executor_failed" in pn
        r["pn_err"] = first_compile_error(logdir, "portage-ng")
        r["em_err"] = first_compile_error(logdir, "emerge")
        results.append(r)

    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        buckets[bucket(r)].append(r)

    for name in sorted(buckets):
        print(f"\n=== {name} ({len(buckets[name])}) ===")
        for r in buckets[name]:
            pn = (r.get("pn_fail") or "?")[:55]
            em = (r.get("em_fail") or "?")[:40]
            print(f"{r['cp']}: pn={pn} em={em}")
            if r.get("pn_err"):
                print(f"  pn_err: {r['pn_err'][:90]}")
            if r.get("em_err"):
                print(f"  em_err: {r['em_err'][:90]}")

    out = os.environ.get("OUT", "/tmp/ff-analysis.json")
    with open(out, "w") as fh:
        json.dump(results, fh, indent=2)
    print(f"\nWrote {out} ({len(results)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
