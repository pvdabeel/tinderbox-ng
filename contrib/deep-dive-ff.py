#!/usr/bin/env python3
import glob, os, re, sys

LOGS = "/srv/tinderbox-ng/logs"


def label(cp):
    return cp.replace("/", "_").replace("-", "_")


def latest_log(cp):
    lab = label(cp)
    dirs = sorted(glob.glob(f"{LOGS}/compare-{lab}-*"), reverse=True)
    return dirs[0] if dirs else None


def em_fail(logdir):
    path = os.path.join(logdir, "emerge.build.log")
    if not os.path.isfile(path):
        return "no build log"
    with open(path, errors="replace") as f:
        text = f.read()
    fails = re.findall(r"\(([^:]+:[^:]+)::gentoo, ebuild scheduled", text)
    if fails:
        return fails[-1]
    for pat in [r"\* ERROR: ([^\s]+)::", r"!!! ([^\n]+)"]:
        ms = re.findall(pat, text)
        if ms:
            return ms[-1][:80]
    return text.strip()[-120:]


def pn_assume(logdir):
    for fn in ("portage-ng.build.log", "portage-ng.plan.log"):
        p = os.path.join(logdir, fn)
        if os.path.isfile(p):
            with open(p, errors="replace") as f:
                t = f.read()
            if "assumed running" in t:
                return f"assume x{t.count('assumed running')}"
            if "domain assumptions" in t:
                return "domain"
    return "-"


def pn_fail_atom(logdir):
    p = os.path.join(logdir, "portage-ng.build.log")
    if not os.path.isfile(p):
        return "?"
    with open(p, errors="replace") as f:
        pn = f.read()
    m = re.search(r"\[step \d+\] FAIL[^\n]*install portage://(\S+)", pn)
    return m.group(1) if m else "?"


samples = sys.argv[1:] or [
    "app-backup/kup",
    "app-accessibility/kontrast",
    "app-misc/gnote",
    "app-text/calibre",
    "app-emulation/virtualbox",
    "app-containers/snapd",
    "dev-libs/libratbag",
    "app-admin/mkosi",
    "acct-user/buildbot",
    "app-accessibility/caribou",
    "app-cdr/xfburn",
    "app-editors/kile",
    "dev-cpp/gtkmm",
]

print(f"{'cp':<42} {'pn_flag':<14} {'pn_atom':<30} em_fail")
for cp in samples:
    d = latest_log(cp)
    if not d:
        print(f"{cp:<42} NO LOG")
        continue
    target = cp.split("/")[1].split("-")[0]
    em = em_fail(d)
    em_short = em[:45]
    same = "SAME" if em.startswith(cp.split("/")[0]) and target in em else ""
    if pn_fail_atom(d) in em:
        same = "SAME"
    print(
        f"{cp:<42} {pn_assume(d):<14} {pn_fail_atom(d):<30} {em_short} {same}"
    )
