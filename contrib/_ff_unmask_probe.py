import json, glob, os, re, gzip
from collections import Counter

J = "/srv/tinderbox-ng/reports/compare-matrix-20260613T162300/failfail-classified.json"
rows = json.load(open(J))
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def rd(p):
    try:
        return ANSI.sub("", (gzip.open(p, "rt", errors="replace") if p.endswith(".gz") else open(p, errors="replace")).read())
    except OSError:
        return ""


kw_filtered = re.compile(r"keyword-filtered dependency on (\S+)")
verify_star = re.compile(r"verify\s+(\S+)\s+\(requires \*\*\)")
unmask_step = re.compile(r"unmask\s+portage://(\S+)")
preinst_grp = re.compile(r"(useradd|usermod|groupadd):\s*group '([^']+)' does not exist")

hits = []
for r in rows:
    d = r["logdir"]
    if not d or not os.path.isdir(d):
        continue
    blob = ""
    for f in ("portage-ng.plan.log", "portage-ng.build.log"):
        blob += rd(os.path.join(d, f))
    # also scan target build log for the useradd signature
    for f in glob.glob(os.path.join(d, "portage-ng.target.*.build.log")):
        blob += rd(f)
    kwf = kw_filtered.findall(blob)
    vst = verify_star.findall(blob)
    unm = unmask_step.findall(blob)
    grp = preinst_grp.findall(blob)
    if kwf or vst:
        hits.append({
            "target": r["target"],
            "em_reason": r["em_reason"],
            "expectation": r.get("expectation"),
            "pn_first_fail_pkg": r["pn_first_fail_pkg"],
            "pn_phase": r["pn_first_fail_phase"] or r["pn_first_fail_op"],
            "dropped_deps": sorted(set(kwf) | set(d for d in vst)),
            "unmasked": sorted(set(unm)),
            "group_missing": sorted(set(g[1] for g in grp)),
        })

print("fail/fail with buildbot-style signature (unmask target + dependency dropped to verify-only/keyword-filtered):", len(hits))
print("\n-- by emerge reason --")
for k, v in Counter(h["em_reason"] for h in hits).most_common():
    print("  %5d  %s" % (v, k))
print("\n-- by portage-ng failing phase --")
for k, v in Counter(h["pn_phase"] for h in hits).most_common():
    print("  %5d  %s" % (v, k))
print("\n-- with a confirmed 'group does not exist' preinst failure --")
gm = [h for h in hits if h["group_missing"]]
print("  count:", len(gm))
for h in gm:
    print("    %-30s missing-group=%s dropped=%s" % (h["target"], h["group_missing"], h["dropped_deps"]))
print("\n-- all signature hits (target | emerge | pn_phase | dropped_deps) --")
for h in hits:
    print("  %-34s %-13s %-9s drop=%s" % (h["target"], h["em_reason"], h["pn_phase"], h["dropped_deps"][:3]))
