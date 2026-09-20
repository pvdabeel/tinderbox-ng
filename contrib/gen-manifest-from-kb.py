#!/usr/bin/env python3
"""
gen-manifest-from-kb.py - emit a compare-matrix manifest from portage-ng kb.raw

Reads ordered_entry facts from kb.raw (or kb.qlf via swipl if --qlf is
passed) and writes one atom per line — the same shape compare-matrix
expects.

Two grains:

  package (default)  unique cat/pn, one line each. This is what the
                     all-packages tinderbox consumes: each atom resolves
                     to whatever version emerge / portage-ng pick today.

  ebuild             every cat/pn-ver in the kb, written as =cat/pn-ver
                     so both engines pin that exact ebuild.

--exclude-run DIR walks a finished compare-matrix run, recovers the CPV
each target actually resolved to (from that session's plan logs), and
drops those versions. Use this to follow an all-packages sweep with an
all-ebuild sweep that does not re-test the versions already covered.

Default input is the baseline kb at /srv/tinderbox-ng/baseline/opt/portage-ng/
Knowledge/kb.raw on the VM; override with --kb.

Examples:
  contrib/gen-manifest-from-kb.py \\
      --kb /srv/tinderbox-ng/baseline/opt/portage-ng/Knowledge/kb.raw \\
      --out share/tinderbox-ng/manifest-all-packages.txt

  contrib/gen-manifest-from-kb.py --grain ebuild \\
      --exclude-run /srv/tinderbox-ng/reports/compare-matrix-20260913T042348 \\
      --out /srv/tinderbox-ng/manifests/manifest-all-ebuilds-remaining.txt
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ORDERED_ENTRY_RE = re.compile(r"^ordered_entry\(portage,'([^']+)'")
_PN_VER_SPLIT_RE = re.compile(r"-(?=\d)")
_BUILD_ID_TAIL_RE = re.compile(r"-\d+$")
_LOGDIR_RE = re.compile(r"/srv/tinderbox-ng/logs/compare-[^ )]+")
_EMERGE_PLAN_ATOM_RE = re.compile(
    r"^\[\s*(?:ebuild|binary)\s+[A-Z][^]]*\]\s+(\S+)",
    re.IGNORECASE,
)
_PORTAGE_NG_PLAN_ATOM_RE = re.compile(
    r"Emerging\s*:\s*portage://(\S+)"
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def split_pn_ver(cpv: str) -> tuple[str, str] | None:
    """Split cat/pn-ver into (cat/pn, ver) using the PMS left-to-right rule.

    Strips a trailing -N BUILD_ID (no r prefix) so emerge binpkg tokens
    like app-misc/jq-1.8.2-1 collapse to app-misc/jq + 1.8.2. Revisions
    (-rN) are kept.
    """
    if "/" not in cpv:
        return None
    cat, rest = cpv.split("/", 1)
    m = _PN_VER_SPLIT_RE.search(rest)
    if not m:
        return None
    pn = rest[: m.start()]
    ver = rest[m.start() + 1 :]
    ver = _BUILD_ID_TAIL_RE.sub("", ver)
    if not pn or not ver:
        return None
    return f"{cat}/{pn}", ver


def canonicalize_cpv(token: str) -> str | None:
    """Strip repo/slot/USE decorations and return a bare cat/pn-ver, or None."""
    token = _ANSI_RE.sub("", token).strip()
    token = token.split("::", 1)[0]
    token = token.split("?", 1)[0]
    if "/" not in token:
        return None
    cat, rest = token.split("/", 1)
    rest = rest.split(":", 1)[0]
    split = split_pn_ver(f"{cat}/{rest}")
    if not split:
        return None
    cp, ver = split
    return f"{cp}-{ver}"


def packages_from_kb_text(text: str) -> set[str]:
    atoms: set[str] = set()
    for line in text.splitlines():
        m = ORDERED_ENTRY_RE.match(line)
        if not m:
            continue
        split = split_pn_ver(m.group(1))
        if split:
            atoms.add(split[0])
    return atoms


def ebuilds_from_kb_text(text: str) -> set[str]:
    atoms: set[str] = set()
    for line in text.splitlines():
        m = ORDERED_ENTRY_RE.match(line)
        if not m:
            continue
        cpv = canonicalize_cpv(m.group(1))
        if cpv:
            atoms.add(cpv)
    return atoms


def sanitize_label(atom: str) -> str:
    """Filesystem-safe compare label. Must stay in sync with compare-matrix.sh."""
    s = atom.replace("+", "p")
    s = re.sub(r"[^A-Za-z0-9]+", "_", s)
    return s.strip("_")


def _last_matching_atom(path: Path, regex: re.Pattern[str], target_cp: str) -> str | None:
    if not path.is_file():
        return None
    last_any: str | None = None
    last_match: str | None = None
    try:
        with path.open("r", errors="replace") as fh:
            for line in fh:
                m = regex.search(line)
                if not m:
                    continue
                cpv = canonicalize_cpv(m.group(1))
                if not cpv:
                    continue
                last_any = cpv
                split = split_pn_ver(cpv)
                if split and split[0] == target_cp:
                    last_match = cpv
    except OSError:
        return None
    return last_match or last_any


def recover_cpv_from_logdir(logdir: Path, target_cp: str) -> str | None:
    """Recover the CPV a compare session actually resolved for target_cp."""
    return (
        _last_matching_atom(logdir / "emerge.plan.log", _EMERGE_PLAN_ATOM_RE, target_cp)
        or _last_matching_atom(logdir / "portage-ng.plan.log", _PORTAGE_NG_PLAN_ATOM_RE, target_cp)
        or _last_matching_atom(logdir / "emerge.build.log", _EMERGE_PLAN_ATOM_RE, target_cp)
        or _last_matching_atom(logdir / "portage-ng.build.log", _PORTAGE_NG_PLAN_ATOM_RE, target_cp)
    )


def _logdir_from_wrapper(wrapper: Path) -> Path | None:
    try:
        with wrapper.open("r", errors="replace") as fh:
            for line in fh:
                m = _LOGDIR_RE.search(line)
                if m:
                    path = Path(m.group(0))
                    return path if path.is_dir() else None
    except OSError:
        return None
    return None


def tested_cpvs_from_run(run_dir: Path) -> tuple[set[str], dict[str, int]]:
    """Return CPVs recovered from a compare-matrix run plus recovery stats."""
    tsv = run_dir / "results.tsv"
    if not tsv.is_file():
        raise SystemExit(f"results.tsv not found in {run_dir}")

    stats = {
        "targets": 0,
        "recovered": 0,
        "no_wrapper": 0,
        "no_logdir": 0,
        "no_cpv": 0,
    }
    tested: set[str] = set()

    with tsv.open("r", errors="replace") as fh:
        header = fh.readline()
        if not header:
            return tested, stats
        for raw in fh:
            target = raw.split("\t", 1)[0].strip()
            if not target or target.startswith("#"):
                continue
            stats["targets"] += 1
            # Package-grain rows are bare cat/pn; ebuild-grain rows may be
            # =cat/pn-ver. Recover against the cat/pn either way.
            target_cpv = canonicalize_cpv(target.lstrip("="))
            if target_cpv:
                target_cp = split_pn_ver(target_cpv)[0] if split_pn_ver(target_cpv) else target.lstrip("=")
            else:
                target_cp = target.lstrip("=")

            wrapper = run_dir / f"{sanitize_label(target)}.log"
            if not wrapper.is_file():
                stats["no_wrapper"] += 1
                continue
            logdir = _logdir_from_wrapper(wrapper)
            if logdir is None:
                stats["no_logdir"] += 1
                continue
            cpv = recover_cpv_from_logdir(logdir, target_cp)
            if cpv is None:
                stats["no_cpv"] += 1
                continue
            tested.add(cpv)
            stats["recovered"] += 1

    return tested, stats


def read_kb_raw(path: Path) -> str:
    return path.read_text(errors="replace")


def read_kb_qlf(path: Path) -> str:
    # Dump ordered_entry heads as text via a one-liner consult.
    script = f"""
:- set_prolog_flag(encoding, utf8).
:- consult('{path.as_posix()}').
:- dynamic(ordered_entry/5).
findall(CPV, ordered_entry(portage, CPV, _, _, _), CPVs),
        sort(CPVs, Sorted),
        forall(member(C, Sorted), format('ordered_entry(portage,~q).~n', [C])),
        halt.
"""
    proc = subprocess.run(
        ["swipl", "--no-signals", "-q"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"swipl failed reading {path} (exit {proc.returncode})")
    return proc.stdout


def write_manifest(
    out_path: Path,
    atoms: list[str],
    *,
    kb_path: Path,
    tree_commit: str | None,
    grain: str,
    exclude_runs: list[Path],
    kb_count: int,
    excluded_count: int,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if grain == "ebuild":
        shape = "One =cat/pn-ver atom per line (exact ebuild) for compare-matrix."
        derived = "Derived from portage-ng ordered_entry facts (every CPV)."
    else:
        shape = "One cat/pn atom per line for compare-matrix / tinderbox-ng continue."
        derived = "Derived from portage-ng ordered_entry facts (unique cat/pn)."
    header = [
        "# Generated by contrib/gen-manifest-from-kb.py",
        f"# kb: {kb_path}",
        f"# grain: {grain}",
    ]
    if tree_commit:
        header.append(f"# portage-tree: {tree_commit}")
    for run in exclude_runs:
        header.append(f"# exclude-run: {run}")
    header.extend([
        f"# generated: {now}",
        f"# kb_entries: {kb_count}",
        f"# excluded: {excluded_count}",
        f"# entries: {len(atoms)}",
        "#",
        f"# {shape}",
        f"# {derived}",
        "",
    ])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(header + atoms) + "\n")


def default_out_path(grain: str, exclude_runs: list[Path]) -> Path:
    if exclude_runs and grain == "ebuild":
        return Path("share/tinderbox-ng/manifest-all-ebuilds-remaining.txt")
    if grain == "ebuild":
        return Path("share/tinderbox-ng/manifest-all-ebuilds.txt")
    return Path("share/tinderbox-ng/manifest-all-packages.txt")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--kb",
        type=Path,
        default=Path("/srv/tinderbox-ng/baseline/opt/portage-ng/Knowledge/kb.raw"),
        help="Path to kb.raw (default: baseline kb.raw on the VM)",
    )
    ap.add_argument(
        "--tree-commit",
        type=Path,
        default=Path("/srv/tinderbox-ng/shared/portage-tree.commit"),
        help="Optional portage-tree.commit file to record in the header",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output manifest path (default depends on --grain / --exclude-run)",
    )
    ap.add_argument(
        "--qlf",
        action="store_true",
        help="Read kb.qlf via swipl instead of parsing kb.raw directly",
    )
    ap.add_argument(
        "--grain",
        choices=("package", "ebuild"),
        default="package",
        help="package = unique cat/pn (default); ebuild = every =cat/pn-ver",
    )
    ap.add_argument(
        "--exclude-run",
        dest="exclude_runs",
        action="append",
        type=Path,
        default=[],
        help="Drop CPVs already resolved in this compare-matrix run (repeatable)",
    )
    args = ap.parse_args()

    if not args.kb.is_file():
        raise SystemExit(f"kb not found: {args.kb}")

    if args.qlf:
        text = read_kb_qlf(args.kb)
    else:
        text = read_kb_raw(args.kb)

    if args.grain == "ebuild":
        raw_atoms = ebuilds_from_kb_text(text)
    else:
        raw_atoms = packages_from_kb_text(text)
    if not raw_atoms:
        raise SystemExit(f"no ordered_entry atoms found in {args.kb}")
    kb_count = len(raw_atoms)

    excluded: set[str] = set()
    for run in args.exclude_runs:
        if not run.is_dir():
            raise SystemExit(f"exclude-run not found: {run}")
        tested, stats = tested_cpvs_from_run(run)
        print(
            f"exclude-run {run}: targets={stats['targets']} "
            f"recovered={stats['recovered']} no_wrapper={stats['no_wrapper']} "
            f"no_logdir={stats['no_logdir']} no_cpv={stats['no_cpv']}",
            file=sys.stderr,
        )
        if args.grain == "ebuild":
            excluded |= tested
        else:
            for cpv in tested:
                split = split_pn_ver(cpv)
                if split:
                    excluded.add(split[0])
            # Also drop any TSV target that is already a bare cat/pn, so a
            # package-grain leftover manifest can subtract completed rows
            # even when CPV recovery failed.
            tsv = run / "results.tsv"
            with tsv.open("r", errors="replace") as fh:
                next(fh, None)
                for raw in fh:
                    target = raw.split("\t", 1)[0].strip().lstrip("=")
                    if target and "/" in target and split_pn_ver(target) is None:
                        excluded.add(target)

    remaining = raw_atoms - excluded
    if args.grain == "ebuild":
        atoms = sorted(f"={cpv}" for cpv in remaining)
    else:
        atoms = sorted(remaining)

    if not atoms:
        raise SystemExit("manifest empty after applying --exclude-run")

    tree_commit = None
    if args.tree_commit.is_file():
        tree_commit = args.tree_commit.read_text().strip().splitlines()[0]

    out_path = args.out or default_out_path(args.grain, args.exclude_runs)
    write_manifest(
        out_path,
        atoms,
        kb_path=args.kb.resolve(),
        tree_commit=tree_commit,
        grain=args.grain,
        exclude_runs=[p.resolve() for p in args.exclude_runs],
        kb_count=kb_count,
        excluded_count=len(excluded),
    )
    print(
        f"wrote {len(atoms)} {args.grain} atoms "
        f"(kb={kb_count} excluded={len(excluded)}) -> {out_path}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
