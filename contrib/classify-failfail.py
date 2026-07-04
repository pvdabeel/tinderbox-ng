#!/usr/bin/env python3
"""Classify fail/fail compare cases (both engines failed) into actionable buckets.

For every compare-matrix wrapper *.log where BOTH engines failed, inspect the
per-package compare logdir and determine, for each engine, *why* it failed and
whether portage-ng's failure is a genuine upstream break or a portage-ng gap.

emerge side (plan-reason taxonomy):
  - failure stage: PLAN reject (never proven unbuildable) vs real BUILD break.
  - plan sub-reason: needs_keyword / needs_license (soft policy gate the user
    can flip) vs needs_unmask (hard package.mask) vs use_dep_unsat /
    required_use / blocker / circular / unsatisfiable (resolver constraints).

portage-ng side (build-failure analysis):
  - completed / failed action counts.
  - the FIRST genuinely-failed ebuild (op + pkg + phase) -- the cascade *root*,
    since portage-ng builds dependencies before the target.
  - a finer *signature* of that root failure (compile / configure_cmake /
    configure_meson / python / collision / fetch / test) plus an error excerpt,
    read from the salvaged build-logs/portage-ng/**.build.log[.gz] and the
    target build log.

Derived judgements:
  - 'should_have_succeeded' (high precision): emerge rejected at PLAN AND
    portage-ng's failure is not a real source break (only trivial always-install
    pkgs failed, or the failing phase is a merge-time phase).
  - 'expectation' bucket (genuine_break / expected_build / ok_to_fail /
    resolver_gap) framing each case against user policy.
  - 'cluster' (A..H): the portage-ng-improvement theme the case belongs to
    (see CLUSTER_DOC below), with a per-cluster cascade-root histogram in the
    summary so a handful of root packages can be prioritised.

Outputs: a human summary on stdout, plus <RUN>/failfail-classified.json (a JSON
array, one object per case) consumed by _ff_unmask_probe.py and show-fail.py.

Usage: classify-failfail.py [RUN_DIR]
"""
import os
import re
import sys
import glob
import gzip
import json
from collections import Counter, defaultdict

RUN = sys.argv[1] if len(sys.argv) > 1 else "/srv/tinderbox-ng/reports/compare-matrix-20260621T174159"

ANSI = re.compile(r"\x1b\[[0-9;]*m")
logs_re = re.compile(r"logs:\s*(/srv/tinderbox-ng/logs/compare-\S+?)\)")
exit_re = re.compile(r"exit\s*\u2502\s*(\S+)\s*\u2502\s*(\S+)")
exit_re2 = re.compile(r"exit\s+\|?\s*([A-Z()0-9_]+)\s+\|?\s*([A-Z()0-9_]+)")
phase_re = re.compile(r"ERROR:\s*(\S+)\s+failed\s*\(([^)]+?)\s+phase\)")
fail_re = re.compile(r"\[step \d+\]\s*FAIL\s*\(([^)]*)\)\s+(\S+)\s+portage://(\S+)")
total_re = re.compile(r"Total:\s*(\d+)\s+completed\s+and\s+(\d+)\s+failed")

SOURCE_PHASES = {"unpack", "prepare", "configure", "compile", "test", "fetch", "download"}
MERGE_PHASES = {"install", "merge", "preinst", "postinst", "setup", "pretend", "config", "prerm", "postrm"}
TRIVIAL_PREFIX = ("acct-user/", "acct-group/", "virtual/")


def read(path):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def strip(s):
    return ANSI.sub("", s)


def read_any(p):
    try:
        if p.endswith(".gz"):
            return strip(gzip.open(p, "rt", errors="replace").read())
        return strip(read(p))
    except OSError:
        return ""


def tail_any(p, nbytes=150_000):
    """Read only the tail of a (possibly .gz) log. The Portage failure banner and
    the real error are always near the end, so tailing keeps regex/splitlines off
    multi-MB build logs -- essential for large runs (systemd/rust/llvm logs)."""
    try:
        if p.endswith(".gz"):
            buf = ""
            with gzip.open(p, "rt", errors="replace") as fh:
                while True:
                    chunk = fh.read(1_000_000)
                    if not chunk:
                        break
                    buf = (buf + chunk)[-nbytes:]
            return strip(buf)
        sz = os.path.getsize(p)
        with open(p, "r", errors="replace") as fh:
            if sz > nbytes:
                fh.seek(sz - nbytes)
            return strip(fh.read())
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# emerge plan-reason taxonomy
# ---------------------------------------------------------------------------
MASKED_BY = re.compile(r"masked by:\s*([^)\n]+)")


def _mask_subreason(t):
    """Decide whether a masked request is satisfiable by *keywording* / accepting
    a *license* (soft gates the user can flip) vs requiring a hard *package.mask*
    override. If any single satisfying candidate is gated ONLY by keyword/license,
    flipping that suffices; if every candidate needs package.mask -> needs_unmask.
    """
    reasons = [r.strip().lower() for r in MASKED_BY.findall(t)]
    if not reasons:
        if "license" in t.lower() and ("accept_license" in t.lower() or "license(s)" in t.lower()):
            return "needs_license"
        return "needs_unmask"
    soft_only = []
    for r in reasons:
        has_mask = "package.mask" in r
        has_kw = "keyword" in r
        has_lic = "license" in r
        if not has_mask and (has_kw or has_lic):
            soft_only.append("needs_license" if (has_lic and not has_kw) else "needs_keyword")
    if soft_only:
        return "needs_keyword" if "needs_keyword" in soft_only else soft_only[0]
    return "needs_unmask"


def emerge_plan_reason(t):
    if "there are no ebuilds built with USE flags to satisfy" in t or \
       "no ebuilds built with USE flags" in t:
        return "use_dep_unsat"
    if "unmet requirements" in t or "REQUIRED_USE" in t:
        return "required_use"
    if re.search(r"Conflict:[^\n]*block", t) or "unsatisfied" in t.lower() or "slot conflict" in t.lower():
        return "blocker"
    if "circular" in t.lower():
        return "circular"
    if "have been masked" in t or "masked by:" in t:
        return _mask_subreason(t)
    if "All ebuilds that could satisfy" in t or "there are no ebuilds" in t.lower() or "no ebuilds to satisfy" in t.lower():
        return "unsatisfiable"
    return "other_plan"


# ---------------------------------------------------------------------------
# portage-ng build-failure signature
# ---------------------------------------------------------------------------
COLLISION = re.compile(r"file collision|colliding files|detected file collision|preexisting file", re.IGNORECASE)
FETCH = re.compile(r"ERROR 404|404 Not Found|Couldn't download|failed \(.*fetch|unable to fetch", re.I)

# Priority-ordered fine signatures applied to a failing pkg's log blob.
SIGS = [
    ("configure_cmake", re.compile(r"CMake Error|Could not find a package configuration file", re.I)),
    ("configure_meson", re.compile(r"meson\.build:\d+:\d+:\s*ERROR|ERROR: Dependency \"", re.I)),
    ("python", re.compile(r"ModuleNotFoundError|No module named|ImportError:", re.I)),
    ("test", re.compile(r"failed \(test phase\)", re.I)),
    ("compile", re.compile(r"emake failed|failed \(compile phase\)|\berror:|undefined reference|ld returned|collect2:", re.I)),
]


def _sig_of(blob):
    """Return (signature, matched_line) for a failing pkg's log blob."""
    for name, rx in SIGS:
        m = rx.search(blob)
        if m:
            line = ""
            for ln in blob.splitlines():
                if rx.search(ln):
                    line = ln.strip()[:200]
                    break
            return name, line
    return "", ""


def fail_type_of_pkg(d, pkg):
    """Classify the real failure of the first-failed pkg.

    Returns (phase, sig, excerpt):
      phase   - ebuild phase from the ERROR banner (compile/configure/install/...),
                'collision', 'fetch', or '' if nothing salvaged.
      sig     - finer signature (configure_cmake/configure_meson/python/compile/...)
                or '' / 'collision' / 'fetch'.
      excerpt - up to ~600 chars around the failure, for cluster keyword matching.
    """
    if not pkg:
        return "", "", ""
    cat, _, pn = pkg.partition("/")
    texts = []
    seen = set()
    for g in (os.path.join(d, "build-logs", "portage-ng", cat, pn + "-*.build.log*"),
              os.path.join(d, "build-logs", "portage-ng", cat, "*")):
        for f in glob.glob(g):
            if f not in seen and os.path.basename(f).startswith(pn):
                seen.add(f)
                texts.append(tail_any(f))
    for f in glob.glob(os.path.join(d, "portage-ng.target.*.build.log")):
        if f not in seen:
            seen.add(f)
            texts.append(tail_any(f))
    blob = "\n".join(texts)
    if not blob.strip():
        return "", "", ""

    # excerpt around the banner (or the tail if no banner)
    lines = blob.splitlines()
    bidx = None
    for i, ln in enumerate(lines):
        if phase_re.search(ln):
            bidx = i
    if bidx is not None:
        excerpt = "\n".join(lines[max(0, bidx - 12):bidx + 3])
    else:
        excerpt = "\n".join(lines[-16:])
    excerpt = excerpt[-600:]

    if COLLISION.search(blob):
        return "collision", "collision", excerpt
    if FETCH.search(blob):
        return "fetch", "fetch", excerpt
    m = phase_re.search(blob)
    phase = m.group(2) if m else ""
    sig, _line = _sig_of(blob)
    return phase, sig, excerpt


# ---------------------------------------------------------------------------
# per-case classification
# ---------------------------------------------------------------------------
def _cp(pkgver):
    if not pkgver:
        return ""
    cat, _, pf = pkgver.partition("/")
    pn = re.sub(r"-\d+(\.\d+)*.*$", "", pf)
    return f"{cat}/{pn}"


def classify(target, d):
    ep_exit = read(os.path.join(d, "emerge.plan.log.exit")).strip()
    eb = os.path.join(d, "emerge.build.log")
    eplan = read(os.path.join(d, "emerge.plan.log"))
    if ep_exit == "0" and os.path.isfile(eb):
        em_stage, em_reason = "BUILD", "build_fail"
    else:
        em_stage, em_reason = "PLAN", emerge_plan_reason(eplan)

    pbuild = strip(read(os.path.join(d, "portage-ng.build.log")))
    pplan = strip(read(os.path.join(d, "portage-ng.plan.log")))
    blob = pbuild or pplan
    tm = total_re.search(blob)
    pn_completed = int(tm.group(1)) if tm else None
    pn_failed = int(tm.group(2)) if tm else None
    fails = fail_re.findall(blob)  # list of (rc, op, pkg)
    pn_reached_build = bool(fails) or os.path.isfile(os.path.join(d, "portage-ng.build.log")) or \
        bool(glob.glob(os.path.join(d, "portage-ng.target.*.build.log")))
    pn_stage = "BUILD" if pn_reached_build else "PLAN"

    first = fails[0] if fails else ("", "", "")
    first_pkg = first[2]
    first_op = first[1]
    first_cp = _cp(first_pkg)
    phase, sig, excerpt = fail_type_of_pkg(d, first_pkg) if first_pkg else ("", "", "")
    # collisions surface as op=install with no ebuild phase; tag fetch failures
    if not phase and first_op in ("download", "fetch"):
        phase, sig = "fetch", "fetch"

    fail_cps = [_cp(p) for (_, _, p) in fails]
    all_trivial = bool(fail_cps) and all(c.startswith(TRIVIAL_PREFIX) for c in fail_cps)
    target_is_first = first_cp == target

    return {
        "target": target,
        "logdir": d,
        "em_stage": em_stage,
        "em_reason": em_reason,
        "pn_stage": pn_stage,
        "pn_completed": pn_completed,
        "pn_failed": pn_failed,
        "pn_first_fail_pkg": first_cp,
        "pn_first_fail_op": first_op,
        "pn_first_fail_phase": phase,
        "pn_first_fail_sig": sig,
        "pn_err_excerpt": excerpt,
        "pn_all_fail_cps": fail_cps,
        "all_trivial": all_trivial,
        "target_is_first_fail": target_is_first,
    }


# ---------------------------------------------------------------------------
# user-expectation grouping (unchanged contract)
# ---------------------------------------------------------------------------
EXPECT = {
    "build_fail": "genuine_break",
    "needs_keyword": "expected_build",
    "needs_license": "expected_build",
    "needs_unmask": "ok_to_fail",
    "use_dep_unsat": "resolver_gap",
    "required_use": "resolver_gap",
    "blocker": "resolver_gap",
    "circular": "resolver_gap",
    "unsatisfiable": "resolver_gap",
    "other_plan": "resolver_gap",
    "no_logdir": "unknown",
}


def expectation(r):
    return EXPECT.get(r["em_reason"], "unknown")


def should_have(r):
    if r["em_stage"] != "PLAN":
        return False
    if r["pn_stage"] != "BUILD":
        return False
    if r.get("all_trivial"):
        return True
    ph = r.get("pn_first_fail_phase", "")
    op = r.get("pn_first_fail_op", "")
    if (r.get("pn_failed") == 1) and (ph in MERGE_PHASES or (not ph and op in MERGE_PHASES)):
        return True
    return False


# ---------------------------------------------------------------------------
# portage-ng-improvement clustering
# ---------------------------------------------------------------------------
CLUSTER_DOC = {
    "A": "Qt6/KDE build-dep gap (qtwayland/qtpaths absent; dep[wayland] unhonored)",
    "B": "dep[useflag] / configure gap (e.g. cairo[X]) -- USE-dep not satisfied",
    "C": "toolchain/setup USE prerequisite (e.g. gcc[objc])",
    "D": "Haskell/GHC (and OCaml) ABI-hash reverse-dep rebuild propagation",
    "E": "genuine upstream breakage (compile/configure; not portage-ng's fault)",
    "F": "fetch-restricted / dead distfile (not a resolver failure)",
    "G": "resource (timeout / OOM-kill)",
    "H": "merge-time collision / circular / opaque (needs manual look)",
}


def cluster(r):
    cp = r.get("pn_first_fail_pkg", "") or ""
    cat = cp.split("/")[0] if "/" in cp else ""
    ph = r.get("pn_first_fail_phase", "") or ""
    sig = r.get("pn_first_fail_sig", "") or ""
    exc = r.get("pn_err_excerpt", "") or ""
    op = r.get("pn_first_fail_op", "") or ""

    if ph == "fetch" or sig == "fetch" or op in ("download", "fetch") or str(r.get("pn_exit", "")).startswith("RESTRICT"):
        return "F"
    if sig == "collision" or ph == "collision":
        return "H"
    if cat in ("kde-frameworks", "kde-plasma", "kde-apps", "kde-misc") or \
       cp in ("dev-libs/plasma-wayland-protocols", "dev-qt/qtbase") or \
       "Qt6Wayland" in exc or "qtpaths" in exc:
        return "A"
    if cat == "dev-haskell" or cp.startswith("dev-ml/") or "haskell-updater" in exc or "ghc-pkg" in exc:
        return "D"
    if cp.startswith("gnustep") or ph == "setup":
        return "C"
    if r.get("em_stage") == "BUILD":
        return "E"
    if ph == "configure" or sig in ("configure_cmake", "configure_meson"):
        # cairo[X]/gcr/lrelease/etc. USE-dep gaps land here; genuine configure
        # breaks (no obvious dep signature) also land here but are rarer.
        return "B"
    if ph == "compile" or sig == "compile":
        return "E"
    if ph in MERGE_PHASES:
        return "H"
    return "H"


# ---------------------------------------------------------------------------
def main():
    rows = []
    for wl in sorted(glob.glob(os.path.join(RUN, "*.log"))):
        txt = read(wl)
        m = re.search(r"target\s*:\s*(\S+)", txt)
        if not m:
            continue
        target = m.group(1)
        em = exit_re.search(txt) or exit_re2.search(txt)
        if not em:
            continue
        pn_exit, emr_exit = em.group(1), em.group(2)
        if pn_exit == "OK" or emr_exit == "OK":
            continue
        lm = logs_re.search(txt)
        d = lm.group(1) if lm else ""
        if d and os.path.isdir(d):
            r = classify(target, d)
        else:
            r = {"target": target, "logdir": d, "em_stage": "?", "em_reason": "no_logdir",
                 "pn_stage": "?", "pn_failed": None, "pn_first_fail_pkg": "",
                 "pn_first_fail_op": "", "pn_first_fail_phase": "", "pn_first_fail_sig": "",
                 "pn_err_excerpt": "", "all_trivial": False, "target_is_first_fail": False}
        r["pn_exit"], r["em_exit"] = pn_exit, emr_exit
        r["should_have"] = should_have(r)
        r["expectation"] = expectation(r)
        r["cluster"] = cluster(r)
        rows.append(r)

    out = os.path.join(RUN, "failfail-classified.json")
    json.dump(rows, open(out, "w"), indent=1)

    print(f"total fail/fail: {len(rows)}")
    print("\n== emerge failure stage/reason ==")
    for k, v in Counter((r["em_stage"], r["em_reason"]) for r in rows).most_common():
        print(f"  {v:5d}  {k[0]}:{k[1]}")

    print("\n== USER-EXPECTATION grouping ==")
    order = ["genuine_break", "expected_build", "ok_to_fail", "resolver_gap", "unknown"]
    ec = Counter(r["expectation"] for r in rows)
    for g in order:
        if ec.get(g):
            print(f"  {ec[g]:5d}  {g}")

    print("\n== PORTAGE-NG IMPROVEMENT CLUSTERS ==")
    cc = Counter(r["cluster"] for r in rows)
    for k in sorted(cc, key=lambda x: -cc[x]):
        print(f"  {cc[k]:5d}  {k}: {CLUSTER_DOC.get(k, '')}")

    print("\n== per-cluster cascade roots (root pkg -> #targets blocked) ==")
    roots = defaultdict(Counter)
    for r in rows:
        if r.get("pn_first_fail_pkg"):
            roots[r["cluster"]][r["pn_first_fail_pkg"]] += 1
    for k in sorted(roots, key=lambda x: -cc[x]):
        top = roots[k].most_common(8)
        if not top:
            continue
        print(f"  [{k}] {CLUSTER_DOC.get(k, '')}")
        for pkg, n in top:
            print(f"       {n:4d}  {pkg}")

    exp = [r for r in rows if r["expectation"] == "expected_build"]
    print(f"\n== 'EXPECTED TO BUILD' (emerge refused only on keyword/license): {len(exp)} ==")
    print("   -> a fail here is a real concern (user would just accept_keywords/license)")
    print("   -- by portage-ng real failure type --")
    for k, v in Counter((r["pn_first_fail_phase"] or r["pn_first_fail_op"] or "(none)") for r in exp).most_common():
        tag = "  <-- portage-ng merge/own bug" if k in ("collision", "install", "merge", "preinst", "postinst", "setup") else ""
        print(f"     {v:5d}  {k}{tag}")

    with open(os.path.join(RUN, "failfail-expected-build.txt"), "w") as f:
        for r in sorted(exp, key=lambda x: x["target"]):
            f.write("%s\t%s\t%s\t%s\n" % (
                r["target"], r["em_reason"],
                r["pn_first_fail_phase"] or r["pn_first_fail_op"], r["pn_first_fail_pkg"]))

    sh = [r for r in rows if r["should_have"]]
    print(f"\n== 'portage-ng should have succeeded' candidates: {len(sh)} ==")
    for k, v in Counter(r["em_reason"] for r in sh).most_common():
        print(f"  {v:5d}  {k}")
    print("-- sample candidates --")
    for r in sh[:30]:
        print(f"  {r['target']:45s} emerge={r['em_reason']:13s} pn_fail={r['pn_first_fail_pkg']} "
              f"phase={r['pn_first_fail_phase']} sig={r['pn_first_fail_sig']} nfail={r['pn_failed']}")
    print(f"\nfull json -> {out}")


if __name__ == "__main__":
    main()
