#!/usr/bin/env python3
"""
binpkg-adjusted.py — emerge-[binary] CPV coverage in portage-ng's merge set

Emerge with a warm binpkg cache plans only the runtime closure (`[binary N]`
lines) and skips BDEPEND. portage-ng still plans the full source-shaped
tree, so raw upper-VDB counts diverge even when both engines merge the
same packages emerge actually installed.

This helper answers the question that count-delta does not:

    For every CPV emerge merges as a binpkg, does portage-ng merge
    that same CPV (BUILD_ID stripped, PMS name/version split)?

A row is a miss when an emerge-binpkg CPV is absent from portage-ng's
merge set. Same cat/pn at a different version is a version-miss (still
a miss for the exact-CPV check). Extra portage-ng BDEPEND installs are
ignored.

Inputs
------
  --emerge-plan FILE   emerge.plan.log (or emerge -vp transcript)
  --pn-vdb FILE        one CPV per line (session upper VDB); preferred
                       when the overlay is still mounted
  --pn-plan FILE       portage-ng.plan.log (install/update/reinstall)
  --pn-build FILE      portage-ng.build.log (same action grammar)

  --logdir DIR         one compare-<label>-<stamp>/ session
  --run DIR            matrix run dir; follows wrapper logs to logdirs
  --kv                 emit binpkg_n= / binpkg_hit= / ... for the shell
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
POWERLINE_RE = re.compile(r"\xee\x82[\xb0-\xbf]|\ue0b0|\ue0b2")
# Same PMS split as compare-merge-emerge.py:split_pn_ver (do not regress).
_PN_VER_SPLIT_RE = re.compile(r"-(?=\d)")
_BUILD_ID_TAIL_RE = re.compile(r"-\d+$")

BINARY_RE = re.compile(
    r"^\[\s*binary\s+[A-Z][^]]*\]\s+(\S+)",
    re.IGNORECASE,
)
EBUILD_RE = re.compile(
    r"^\[\s*ebuild\s+[A-Z][^]]*\]\s+(\S+)",
    re.IGNORECASE,
)
PN_MERGE_RE = re.compile(
    r"\b(install|update|downgrade|reinstall)\s+"
    r"(?:portage|overlay|pkg)://([A-Za-z0-9+_.-]+/[A-Za-z0-9+_.-]+)\b"
)
LOGDIR_RE = re.compile(
    r"(?:logs:\s*|logs\s*:\s+)(/srv/tinderbox-ng/logs/compare-\S+)"
)


def strip_ansi(s: str) -> str:
    return POWERLINE_RE.sub("", ANSI_RE.sub("", s))


def split_pn_ver(pnver: str) -> tuple[str, str]:
    """PMS left-to-right split; strip trailing -<int> BUILD_ID, keep -rN."""
    m = _PN_VER_SPLIT_RE.search(pnver)
    if not m:
        return (pnver, "")
    name = pnver[: m.start()]
    ver = _BUILD_ID_TAIL_RE.sub("", pnver[m.start() + 1 :])
    return (name, ver)


def canon_cpv(token: str) -> Optional[str]:
    """cat/pn-ver with BUILD_ID stripped. None if the token is not a CPV."""
    token = token.strip()
    if not token or "/" not in token:
        return None
    if "::" in token:
        token = token.split("::", 1)[0]
    if ":" in token:
        token = token.split(":", 1)[0]
    cat, _, pnver = token.partition("/")
    if not cat or not pnver:
        return None
    pn, ver = split_pn_ver(pnver)
    if not pn:
        return None
    return f"{cat}/{pn}-{ver}" if ver else f"{cat}/{pn}"


def cn_of(cpv: str) -> str:
    cat, _, pnver = cpv.partition("/")
    pn, _ver = split_pn_ver(pnver)
    return f"{cat}/{pn}" if cat else pn


def read_text(path: Path) -> str:
    return strip_ansi(path.read_text(errors="replace"))


def emerge_sets(plan_text: str) -> tuple[list[str], list[str]]:
    """Return (binpkg CPVs in plan order, ebuild CPVs in plan order)."""
    binaries: list[str] = []
    ebuilds: list[str] = []
    seen_b: set[str] = set()
    seen_e: set[str] = set()
    for line in plan_text.splitlines():
        m = BINARY_RE.match(line)
        if m:
            cpv = canon_cpv(m.group(1))
            if cpv and cpv not in seen_b:
                seen_b.add(cpv)
                binaries.append(cpv)
            continue
        m = EBUILD_RE.match(line)
        if m:
            cpv = canon_cpv(m.group(1))
            if cpv and cpv not in seen_e:
                seen_e.add(cpv)
                ebuilds.append(cpv)
    return binaries, ebuilds


def pn_merge_from_text(text: str) -> set[str]:
    out: set[str] = set()
    for line in text.splitlines():
        m = PN_MERGE_RE.search(line)
        if not m:
            continue
        cpv = canon_cpv(m.group(2))
        if cpv:
            out.add(cpv)
    return out


def pn_merge_from_vdb(text: str) -> set[str]:
    out: set[str] = set()
    for line in text.splitlines():
        tok = line.strip()
        if not tok or tok.startswith("#"):
            continue
        cpv = canon_cpv(tok)
        if cpv:
            out.add(cpv)
    return out


def score(binaries: Iterable[str], pn_merged: set[str]) -> dict:
    binaries = list(binaries)
    pn_by_cn: dict[str, list[str]] = {}
    for p in pn_merged:
        pn_by_cn.setdefault(cn_of(p), []).append(p)

    hit: list[str] = []
    ver_miss: list[dict] = []
    cn_miss: list[str] = []
    for cpv in binaries:
        if cpv in pn_merged:
            hit.append(cpv)
            continue
        alts = pn_by_cn.get(cn_of(cpv), [])
        if alts:
            ver_miss.append({"emerge": cpv, "portage_ng": sorted(alts)})
        else:
            cn_miss.append(cpv)

    n = len(binaries)
    n_hit = len(hit)
    n_ver = len(ver_miss)
    n_cn = len(cn_miss)
    return {
        "binpkg_n": n,
        "binpkg_hit": n_hit,
        "binpkg_miss": n - n_hit,
        "binpkg_ver_miss": n_ver,
        "binpkg_cn_miss": n_cn,
        "matched": hit,
        "version_miss": ver_miss,
        "cn_miss": cn_miss,
    }


def score_pair(
    emerge_plan: Optional[Path],
    pn_vdb: Optional[Path],
    pn_plan: Optional[Path],
    pn_build: Optional[Path],
) -> dict:
    binaries: list[str] = []
    ebuilds: list[str] = []
    if emerge_plan and emerge_plan.is_file():
        binaries, ebuilds = emerge_sets(read_text(emerge_plan))

    pn_merged: set[str] = set()
    pn_source = "none"
    if pn_vdb and pn_vdb.is_file():
        pn_merged = pn_merge_from_vdb(read_text(pn_vdb))
        pn_source = "vdb"
    else:
        if pn_plan and pn_plan.is_file():
            pn_merged |= pn_merge_from_text(read_text(pn_plan))
            pn_source = "plan"
        if pn_build and pn_build.is_file():
            pn_merged |= pn_merge_from_text(read_text(pn_build))
            pn_source = "plan+build" if pn_source == "plan" else "build"

    out = score(binaries, pn_merged)
    out["emerge_ebuild_n"] = len(ebuilds)
    out["pn_merged_n"] = len(pn_merged)
    out["pn_source"] = pn_source
    return out


def find_logdir_from_wrapper(wrapper: Path) -> Optional[Path]:
    text = wrapper.read_text(errors="replace")
    m = LOGDIR_RE.search(text)
    if not m:
        return None
    raw = m.group(1).rstrip(")│").rstrip()
    p = Path(raw)
    return p if p.is_dir() else None


def iter_run_logdirs(run: Path) -> list[tuple[str, Path, dict]]:
    """Yield (target, logdir, tsv_row) for completed TSV rows only.

    In-flight wrappers have emerge.plan.log but often no portage-ng
    install lines yet; counting those as CN-misses inflates the gap.
    """
    rows: list[tuple[str, Path, dict]] = []
    tsv = run / "results.tsv"
    if not tsv.is_file():
        return rows

    def label(atom: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", atom.replace("+", "p")).strip("_")

    with tsv.open() as f:
        header = f.readline().rstrip("\n").split("\t")
        for line in f:
            parts = line.rstrip("\n").split("\t")
            rec = dict(zip(header, parts + [""] * (len(header) - len(parts))))
            tgt = rec.get("target", "")
            if not tgt:
                continue
            wrap = run / f"{label(tgt)}.log"
            ld = find_logdir_from_wrapper(wrap) if wrap.is_file() else None
            if ld is None:
                continue
            if not (ld / "emerge.plan.log").is_file():
                continue
            if not (ld / "portage-ng.plan.log").is_file():
                continue
            rows.append((tgt, ld, rec))
    return rows


def score_logdir(logdir: Path) -> dict:
    return score_pair(
        emerge_plan=logdir / "emerge.plan.log",
        pn_vdb=None,
        pn_plan=logdir / "portage-ng.plan.log",
        pn_build=logdir / "portage-ng.build.log",
    )


def kv_lines(result: dict) -> str:
    keys = (
        "binpkg_n",
        "binpkg_hit",
        "binpkg_miss",
        "binpkg_ver_miss",
        "binpkg_cn_miss",
        "emerge_ebuild_n",
        "pn_merged_n",
        "pn_source",
    )
    return "".join(f"{k}={result[k]}\n" for k in keys)


def print_pair(result: dict, title: str = "") -> None:
    n = result["binpkg_n"]
    hit = result["binpkg_hit"]
    pct = (100.0 * hit / n) if n else 100.0
    if title:
        print(title)
    if n == 0:
        print("  emerge binaries: 0  (n/a — emerge planned no [binary] lines)")
        print(f"  emerge ebuilds : {result['emerge_ebuild_n']}")
        return
    print(f"  emerge binaries : {hit}/{n}  ({pct:.1f}%) in portage-ng merge set")
    print(f"  version misses  : {result['binpkg_ver_miss']}")
    print(f"  CN misses       : {result['binpkg_cn_miss']}")
    print(f"  emerge ebuilds  : {result['emerge_ebuild_n']}")
    if result["version_miss"]:
        print("  different version:")
        for row in result["version_miss"][:30]:
            print(f"    {row['emerge']}  ->  {', '.join(row['portage_ng'])}")
        extra = len(result["version_miss"]) - 30
        if extra > 0:
            print(f"    ... +{extra} more")
    if result["cn_miss"]:
        print("  not merged by portage-ng:")
        for cpv in result["cn_miss"][:30]:
            print(f"    {cpv}")
        extra = len(result["cn_miss"]) - 30
        if extra > 0:
            print(f"    ... +{extra} more")


def _pn_ok(x: str) -> bool:
    return x == "OK" or x.startswith("OK(")


def _em_ok(x: str) -> bool:
    return x == "OK"


def summarize_run(pairs: list[tuple[str, dict]]) -> dict:
    def bucket(items: list[tuple[str, dict]]) -> dict:
        with_bin = [r for _, r in items if r["binpkg_n"] > 0]
        perfect = [r for r in with_bin if r["binpkg_miss"] == 0]
        ver_only = [
            r for r in with_bin
            if r["binpkg_miss"] > 0 and r["binpkg_cn_miss"] == 0
        ]
        cn_gap = [r for r in with_bin if r["binpkg_cn_miss"] > 0]
        sum_n = sum(r["binpkg_n"] for r in with_bin)
        sum_hit = sum(r["binpkg_hit"] for r in with_bin)
        miss_counter = Counter()
        for _, r in items:
            for cpv in r["cn_miss"]:
                miss_counter[cpv] += 1
            for row in r["version_miss"]:
                miss_counter[f"{row['emerge']} (ver)"] += 1
        return {
            "rows": len(items),
            "rows_with_binpkg": len(with_bin),
            "rows_perfect": len(perfect),
            "rows_version_only": len(ver_only),
            "rows_cn_miss": len(cn_gap),
            "cpv_n": sum_n,
            "cpv_hit": sum_hit,
            "top_misses": miss_counter.most_common(25),
        }

    okok = [
        (t, r) for t, r in pairs
        if _pn_ok(str(r.get("pn_exit", ""))) and _em_ok(str(r.get("em_exit", "")))
    ]
    return {"all": bucket(pairs), "okok": bucket(okok)}


def _print_bucket(title: str, s: dict) -> None:
    pct_rows = (
        100.0 * s["rows_perfect"] / s["rows_with_binpkg"]
        if s["rows_with_binpkg"]
        else 100.0
    )
    pct_cpv = 100.0 * s["cpv_hit"] / s["cpv_n"] if s["cpv_n"] else 100.0
    print(title)
    print(f"  compared rows     : {s['rows']}")
    print(f"  with emerge binpkg: {s['rows_with_binpkg']}")
    print(
        f"  exact CPV match   : {s['rows_perfect']}/{s['rows_with_binpkg']}  "
        f"({pct_rows:.1f}% of rows with binaries)"
    )
    print(
        f"  CPV coverage      : {s['cpv_hit']}/{s['cpv_n']}  "
        f"({pct_cpv:.1f}% of emerge-binpkg CPVs)"
    )
    print(f"  version-only miss : {s['rows_version_only']} rows")
    print(f"  CN missing        : {s['rows_cn_miss']} rows")
    if s["top_misses"]:
        print("  most frequent misses:")
        for cpv, n in s["top_misses"]:
            print(f"    {n:4d}  {cpv}")


def emit_run(pairs: list[tuple[str, dict]], as_json: bool) -> int:
    summary = summarize_run(pairs)
    worst = sorted(
        ((t, r) for t, r in pairs if r["binpkg_miss"] > 0),
        key=lambda item: (item[1]["binpkg_cn_miss"], item[1]["binpkg_miss"]),
        reverse=True,
    )[:25]
    if as_json:
        def dump_bucket(s: dict) -> dict:
            return {
                **{k: v for k, v in s.items() if k != "top_misses"},
                "top_misses": [{"cpv": c, "n": n} for c, n in s["top_misses"]],
            }

        json.dump(
            {
                "all": dump_bucket(summary["all"]),
                "okok": dump_bucket(summary["okok"]),
                "worst": [
                    {
                        "target": t,
                        "pn_exit": r.get("pn_exit", ""),
                        "em_exit": r.get("em_exit", ""),
                        "binpkg_n": r["binpkg_n"],
                        "binpkg_hit": r["binpkg_hit"],
                        "binpkg_miss": r["binpkg_miss"],
                        "binpkg_ver_miss": r["binpkg_ver_miss"],
                        "binpkg_cn_miss": r["binpkg_cn_miss"],
                        "cn_miss": r["cn_miss"][:20],
                        "version_miss": r["version_miss"][:20],
                    }
                    for t, r in worst
                ],
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    print("binpkg-adjusted (emerge [binary] CPV in portage-ng merge set)")
    _print_bucket("ok/ok", summary["okok"])
    print()
    _print_bucket("all completed", summary["all"])
    if worst:
        print("  worst rows:")
        for t, r in worst:
            print(
                f"    {t:<42}  {r['binpkg_hit']}/{r['binpkg_n']}  "
                f"pn={r.get('pn_exit','?'):<12}  "
                f"ver={r['binpkg_ver_miss']} cn={r['binpkg_cn_miss']}"
            )
    return 0


def _self_check() -> int:
    cases = {
        "app-misc/jq-1.8.1": "app-misc/jq-1.8.1",
        "app-misc/jq-1.8.1-8": "app-misc/jq-1.8.1",
        "dev-libs/oniguruma-6.9.10-r11": "dev-libs/oniguruma-6.9.10-r11",
        "dev-libs/oniguruma-6.9.10-r11-1": "dev-libs/oniguruma-6.9.10-r11",
        "xfce-base/xfce4-panel-4.18.0": "xfce-base/xfce4-panel-4.18.0",
        "app-admin/1password-cli-2.27.0": "app-admin/1password-cli-2.27.0",
        "x11-libs/gtk+-3.24.0": "x11-libs/gtk+-3.24.0",
        "dev-ruby/abbrev-0.1.2-1::gentoo": "dev-ruby/abbrev-0.1.2",
        "dev-lang/ruby-3.3.12-1:3.3::gentoo": "dev-lang/ruby-3.3.12",
    }
    failed = 0
    for raw, want in cases.items():
        got = canon_cpv(raw)
        if got != want:
            print(f"FAIL {raw!r} -> {got!r} (want {want!r})", file=sys.stderr)
            failed += 1
    if failed:
        return 1
    print(f"ok ({len(cases)} CPV cases)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--emerge-plan", type=Path)
    ap.add_argument("--pn-vdb", type=Path)
    ap.add_argument("--pn-plan", type=Path)
    ap.add_argument("--pn-build", type=Path)
    ap.add_argument("--logdir", type=Path)
    ap.add_argument("--run", type=Path)
    ap.add_argument("--kv", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()

    if args.self_check:
        return _self_check()

    modes = sum(1 for x in (args.emerge_plan, args.logdir, args.run) if x)
    if modes != 1 and not (args.emerge_plan or args.logdir or args.run):
        ap.error("need --emerge-plan, --logdir, or --run")
    if args.run and (args.logdir or args.emerge_plan):
        ap.error("--run is exclusive")
    if args.logdir and args.emerge_plan:
        ap.error("--logdir is exclusive of --emerge-plan")

    if args.run:
        run = args.run
        if not run.is_dir():
            print(f"run dir not found: {run}", file=sys.stderr)
            return 2
        pairs = []
        for tgt, ld, rec in iter_run_logdirs(run):
            result = score_logdir(ld)
            result["pn_exit"] = rec.get("pn_exit", "")
            result["em_exit"] = rec.get("em_exit", "")
            pairs.append((tgt, result))
        return emit_run(pairs, args.json)

    if args.logdir:
        if not args.logdir.is_dir():
            print(f"logdir not found: {args.logdir}", file=sys.stderr)
            return 2
        result = score_logdir(args.logdir)
        if args.kv:
            sys.stdout.write(kv_lines(result))
            return 0
        if args.json:
            json.dump(result, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 0
        print_pair(result, title=str(args.logdir))
        return 0

    result = score_pair(args.emerge_plan, args.pn_vdb, args.pn_plan, args.pn_build)
    if args.kv:
        sys.stdout.write(kv_lines(result))
        return 0
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    print_pair(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
