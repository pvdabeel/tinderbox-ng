#!/usr/bin/env python3
"""Classify fail/fail compare cases to surface 'portage-ng should have succeeded'.

For every compare-matrix wrapper *.log where BOTH engines failed, inspect the
per-package compare logdir and determine:

  emerge: failure stage (PLAN reject vs real BUILD break) + reason sub-tag.
  portage-ng: how many ebuilds completed/failed, the FIRST genuinely failed
              ebuild (op + pkg + phase), whether that pkg is the target, and
              the target's package category.

'should have succeeded' (high precision) =
  emerge rejected at PLAN (so the pkg was never proven unbuildable) AND
  portage-ng's failure is NOT a real source break, i.e. either
    - the only failed pkg(s) are trivial always-install pkgs
      (acct-user/*, acct-group/*, virtual/*), or
    - the failing ebuild phase is a merge-time phase (install/merge/
      preinst/postinst/setup) rather than a source phase
      (unpack/prepare/configure/compile/test).
"""
import os
import re
import sys
import glob
import json
from collections import Counter

RUN = sys.argv[1] if len(sys.argv) > 1 else "/srv/tinderbox-ng/reports/compare-matrix-20260613T162300"

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


MASKED_BY = re.compile(r"masked by:\s*([^)\n]+)")


def _mask_subreason(t):
    """Given an emerge plan log that contains masked candidates, decide whether
    the request is satisfiable by *keywording* / *accepting a license* (a soft
    policy gate the user can flip with the reasonable expectation that the build
    then works) versus requiring a hard *package.mask* override (an explicit
    unmask, after which it is 'ok to fail').

    A target lists one or more masked candidates ('One of the following ...').
    If any single candidate is gated ONLY by keyword and/or license (no
    package.mask), flipping that gate suffices -> needs_keyword / needs_license.
    If every satisfying candidate requires a package.mask override ->
    needs_unmask.
    """
    reasons = [r.strip().lower() for r in MASKED_BY.findall(t)]
    if not reasons:
        # 'have been masked' summary without explicit per-candidate reason
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
        # prefer keyword tag if both kinds present
        return "needs_keyword" if "needs_keyword" in soft_only else soft_only[0]
    return "needs_unmask"


def emerge_plan_reason(t):
    # USE-flag dependency that cannot be satisfied (e.g. libselinux[python],
    # acct-user/git[gitea]) -- a resolver constraint, not a user policy gate.
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


def read_any(p):
    try:
        if p.endswith(".gz"):
            import gzip
            return strip(gzip.open(p, "rt", errors="replace").read())
        return strip(read(p))
    except OSError:
        return ""


COLLISION = re.compile(r"file collision|colliding files|detected file collision|preexisting file", re.IGNORECASE)


def fail_type_of_pkg(d, pkg):
    """Classify the real failure of the first-failed pkg.

    Returns one of: compile/configure/install/merge/preinst/postinst/setup/
    unpack/prepare/test (ebuild phase), 'collision' (merge-time file collision),
    'fetch' (download failure), or '' if nothing salvaged.
    """
    if not pkg:
        return ""
    cat, _, pn = pkg.partition("/")
    texts = []
    # specific pkg build log (most precise)
    for g in (os.path.join(d, "build-logs", "portage-ng", cat, pn + "-*.build.log*"),
              os.path.join(d, "build-logs", "portage-ng", cat, "*")):
        for f in glob.glob(g):
            if os.path.basename(f).startswith(pn):
                texts.append(read_any(f))
    # target build log if this is the target
    for f in glob.glob(os.path.join(d, "portage-ng.target.*.build.log")):
        texts.append(read_any(f))
    blob = "\n".join(texts)
    if not blob.strip():
        return ""
    if COLLISION.search(blob):
        return "collision"
    m = phase_re.search(blob)
    if m:
        return m.group(2)
    return ""


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
    # package category/pn of the failed pkg (strip version)
    def cp(pkgver):
        if not pkgver:
            return ""
        catpf = pkgver
        cat, _, pf = catpf.partition("/")
        pn = re.sub(r"-\d+(\.\d+)*.*$", "", pf)
        return f"{cat}/{pn}"
    first_cp = cp(first_pkg)
    phase = fail_type_of_pkg(d, first_pkg) if first_pkg else ""
    # collisions surface as op=install with no ebuild phase; tag fetch failures
    if not phase and first_op in ("download", "fetch"):
        phase = "fetch"

    fail_cps = [cp(p) for (_, _, p) in fails]
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
        "pn_all_fail_cps": fail_cps,
        "all_trivial": all_trivial,
        "target_is_first_fail": target_is_first,
    }


# How a fail/fail case maps onto user expectation, per the rig's contract:
#   genuine_break    - emerge actually built it and the source failed: both right to fail.
#   expected_build   - emerge only refused on a soft policy gate the user can flip
#                      (accept_keywords / accept_license). A user who keyworded the
#                      package would reasonably expect it to build -> a fail here is
#                      a real concern.
#   ok_to_fail       - emerge refused because the package is package.mask'd. Building
#                      it requires an explicit unmask, so it is 'ok' for it to fail.
#   resolver_gap     - emerge refused on a constraint portage-ng ignored (unsatisfiable
#                      USE-dep, REQUIRED_USE, blocker, circular): portage-ng should have
#                      *refused at plan*, not failed mid-build.
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
    # merge-time failure (built ok, merge bugged) and only a single failure
    if (r.get("pn_failed") == 1) and (ph in MERGE_PHASES or (not ph and op in MERGE_PHASES)):
        return True
    return False


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
                 "pn_first_fail_op": "", "pn_first_fail_phase": "", "all_trivial": False,
                 "target_is_first_fail": False}
        r["pn_exit"], r["em_exit"] = pn_exit, emr_exit
        r["should_have"] = should_have(r)
        r["expectation"] = expectation(r)
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

    exp = [r for r in rows if r["expectation"] == "expected_build"]
    print(f"\n== 'EXPECTED TO BUILD' (emerge refused only on keyword/license): {len(exp)} ==")
    print("   -> a fail here is a real concern (user would just accept_keywords/license)")
    print("   -- by emerge reason --")
    for k, v in Counter(r["em_reason"] for r in exp).most_common():
        print(f"     {v:5d}  {k}")
    print("   -- by portage-ng real failure type --")
    for k, v in Counter((r["pn_first_fail_phase"] or r["pn_first_fail_op"] or "(none)") for r in exp).most_common():
        tag = "  <-- portage-ng merge/own bug" if k in ("collision", "install", "merge", "preinst", "postinst", "setup") else ""
        print(f"     {v:5d}  {k}{tag}")
    merge_bugs = [r for r in exp if (r["pn_first_fail_phase"] or r["pn_first_fail_op"]) in ("collision", "install", "merge", "preinst", "postinst", "setup")]
    print(f"\n   -- {len(merge_bugs)} EXPECTED-TO-BUILD cases where portage-ng died at merge/own step (strongest 'should have succeeded') --")
    for r in merge_bugs[:60]:
        print(f"     {r['target']:42s} pn_fail={r['pn_first_fail_pkg']} "
              f"type={r['pn_first_fail_phase'] or r['pn_first_fail_op']}")
    # write the expected-build list out for follow-up
    with open(os.path.join(RUN, "failfail-expected-build.txt"), "w") as f:
        for r in sorted(exp, key=lambda x: x["target"]):
            f.write("%s\t%s\t%s\t%s\n" % (
                r["target"], r["em_reason"],
                r["pn_first_fail_phase"] or r["pn_first_fail_op"], r["pn_first_fail_pkg"]))

    print("\n== CROSS-TAB: emerge reject reason  x  portage-ng real failure type ==")
    ct = Counter((r["em_reason"], r.get("pn_first_fail_phase") or r.get("pn_first_fail_op") or "(none)") for r in rows)
    reasons = [k for k, _ in Counter(r["em_reason"] for r in rows).most_common()]
    ftypes = [k for k, _ in Counter((r.get("pn_first_fail_phase") or r.get("pn_first_fail_op") or "(none)") for r in rows).most_common()]
    hdr = "  %-14s" % "emerge\\pn" + "".join("%-11s" % f[:10] for f in ftypes)
    print(hdr)
    for rs in reasons:
        line = "  %-14s" % rs
        for f in ftypes:
            v = ct.get((rs, f), 0)
            line += "%-11s" % (str(v) if v else ".")
        print(line)

    sh = [r for r in rows if r["should_have"]]
    print(f"\n== 'portage-ng should have succeeded' candidates: {len(sh)} ==")
    print("-- by emerge reject reason --")
    for k, v in Counter(r["em_reason"] for r in sh).most_common():
        print(f"  {v:5d}  {k}")
    print("-- by pn failing phase --")
    for k, v in Counter((r["pn_first_fail_phase"] or r["pn_first_fail_op"] or "?") for r in sh).most_common():
        print(f"  {v:5d}  {k}")
    print("-- by target category --")
    for k, v in Counter(r["target"].split("/")[0] for r in sh).most_common(15):
        print(f"  {v:5d}  {k}")

    print("\n-- sample candidates --")
    for r in sh[:40]:
        print(f"  {r['target']:45s} emerge={r['em_reason']:13s} pn_fail={r['pn_first_fail_pkg']} "
              f"op={r['pn_first_fail_op']} phase={r['pn_first_fail_phase']} nfail={r['pn_failed']}")
    print(f"\nfull json -> {out}")


if __name__ == "__main__":
    main()
