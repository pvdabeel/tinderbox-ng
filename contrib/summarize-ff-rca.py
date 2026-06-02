#!/usr/bin/env python3
"""Summarize fail/fail RCA buckets and ok/fail conversion candidates."""
import json
import os
import re
import sys
from collections import defaultdict

LOGS = os.environ.get("LOGS", "/srv/tinderbox-ng/logs")
JSON = os.environ.get("JSON", "/tmp/ff-analysis.json")


def read_tail(path: str, n: int = 8000) -> str:
    if not os.path.isfile(path):
        return ""
    with open(path, errors="replace") as fh:
        return fh.read()[-n:]


def em_failed_pkg(logdir: str) -> str | None:
    text = read_tail(os.path.join(logdir, "emerge.build.log"))
    m = re.search(r"\(([^:]+:[^:]+)::gentoo, ebuild scheduled", text)
    return m.group(1) if m else None


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
    if r.get("pn_fail") and r.get("em_fail") and r["pn_fail"] == r["em_fail"]:
        return "G_same_atom"
    for k in ("pn_err", "em_err"):
        err = str(r.get(k) or "")
        if "Module.symvers" in err or "linux-mod-r1" in err or "vhba" in err.lower():
            return "D_kernel"
    return "Z_other"


def main() -> int:
    with open(JSON) as f:
        rows = json.load(f)

    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        buckets[bucket(r)].append(r)

    print("=== BUCKET SUMMARY ===")
    for name in sorted(buckets):
        print(f"{name}: {len(buckets[name])}")

    pn_bugs = buckets["B_assume_verify"] + buckets["C_domain"] + buckets["F_executor"]
    print(f"\n=== PORTAGE-NG BUG BUCKETS ({len(pn_bugs)} rows) ===")
    print(f"{'cp':<42} {'flags':<12} {'pn_fail':<28} {'em_fail':<28} same?")
    conversion: list[dict] = []
    for r in sorted(pn_bugs, key=lambda x: x["cp"]):
        logdir = os.path.join(LOGS, r["log"]) if r.get("log") else None
        em_pkg = em_failed_pkg(logdir) if logdir else r.get("em_fail")
        pn_pkg = r.get("pn_fail")
        same = em_pkg == pn_pkg if em_pkg and pn_pkg else None
        flags = []
        if r.get("assume"):
            flags.append("assume")
        if r.get("domain"):
            flags.append("domain")
        if r.get("executor"):
            flags.append("executor")
        print(
            f"{r['cp']:<42} {','.join(flags):<12} "
            f"{str(pn_pkg)[:28]:<28} {str(em_pkg)[:28]:<28} {same}"
        )
        # ok/fail candidate: pn bug (assume/domain) and em fails on target or different atom
        target = r["cp"]
        if r.get("assume") or r.get("domain"):
            target_atom = target  # cat/pn without version
            em_is_target = em_pkg and em_pkg.split("/")[1].split("-")[0] in target
            pn_is_dep = pn_pkg and pn_pkg != em_pkg
            if same is False or (pn_is_dep and not em_is_target):
                conversion.append({**r, "em_pkg": em_pkg, "conversion": "likely ok/fail"})

    print(f"\n=== OK/FAIL CONVERSION CANDIDATES ({len(conversion)}) ===")
    print("(pn fails due to assume/domain bug; em fails on different atom or target)")
    for r in sorted(conversion, key=lambda x: x["cp"]):
        print(
            f"  {r['cp']}: pn={r.get('pn_fail')} em={r.get('em_pkg')} "
            f"[{r['conversion']}]"
        )

    # Cluster assume failures by pn_fail atom
    print("\n=== B_assume_verify clusters (by pn_fail atom) ===")
    clusters: dict[str, list[str]] = defaultdict(list)
    for r in buckets["B_assume_verify"]:
        clusters[str(r.get("pn_fail") or "?")].append(r["cp"])
    for atom, cps in sorted(clusters.items(), key=lambda x: -len(x[1])):
        print(f"  {len(cps):3d}  {atom}")
        if len(cps) <= 5:
            for cp in cps:
                print(f"       - {cp}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
