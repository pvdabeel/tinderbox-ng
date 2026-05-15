#!/usr/bin/env python3
"""
extract-timing.py
=================

Extract per-engine, per-phase wall-clock timing from `tinderbox-ng compare`
session log directories and emit a consolidated JSON report comparing
portage-ng to emerge across one or many sessions.

Adapted from prolog/Reports/Scripts/extract-timing.py, which read paired
``<entry>.merge`` / ``<entry>.emerge`` files in a flat graph directory
and parsed embedded ``% emerge wall_time_ms: NNN`` markers. tinderbox-ng
does not rely on those markers (emerge has never emitted them; portage-ng
emits them only in the writer.pl output path, which the ``--ci`` compare
mode bypasses), so this version reads the canonical sources tinderbox-ng
itself produces.

Canonical input layout
----------------------

``tinderbox-ng compare`` writes a per-session directory of the form::

    <logdir>/
      portage-ng.plan.log
      portage-ng.plan.log.exit         # rc as decimal int
      portage-ng.plan.log.timing       # key=value: started, ended,
                                       #            wall_time_ms, rc
      portage-ng.build.log[.exit][.timing]
      emerge.plan.log[.exit][.timing]
      emerge.build.log[.exit][.timing]
      portage-ng.target.<safe>.build.log[.gz]
      emerge.target.<safe>.<pf>.log[.gz]
      phase_stats.pl                   # portage-ng builds only

The ``*.timing`` companion files are the canonical wall-clock source -
``cmd_compare`` writes them at the moment each pass finishes. When a
``.timing`` file is absent (older sessions, partial runs), we fall back
to the log file's ctime/mtime delta and flag the entry with
``mtime_estimated: true`` so consumers know the value is approximate.

Discovery
---------

Pick exactly one source. They mirror ``tinderbox-ng phase-stats``:

  --src DIR       Walk DIR for compare-* subdirs (the typical input is
                  ``/srv/tinderbox-ng/logs``). Recurses one level deep.
  --logdir DIR    Same as ``--src`` but conceptually a flat container of
                  compare sessions. Equivalent in practice.
  --run RUN_DIR   Matrix run dir (``compare-matrix-<stamp>/``). Each
                  per-package wrapper log under the run dir contains a
                  ``logs: <abs-path>`` line that points at the actual
                  compare session dir; we follow that pointer. This
                  scopes the report to a single matrix run.
  --session DIR   A single ``compare-<label>-<stamp>/`` directory.

Output
------

``--out FILE`` (default depends on source: see ``cmd_extract_timing`` in
``bin/tinderbox-ng``). The JSON has the shape::

    {
      "source": "<absolute path>",
      "source_kind": "src" | "logdir" | "run" | "session",
      "summary": {
        "sessions_total":         42,
        "sessions_with_timing":   42,
        "sessions_mtime_fallback": 0,

        "portage_ng_plan_count":  42,
        "portage_ng_plan_avg_ms": 4123.0,
        "portage_ng_plan_p50_ms": 3500,
        "portage_ng_plan_p95_ms": 12000,
        "portage_ng_build_count": 38,
        ... (same for each of the four passes) ...

        "build_ratio_pn_over_em_avg": 1.48,
        "build_ratio_pn_over_em_p50": 1.32,
        "build_ratio_pn_over_em_p95": 3.10,
        "plan_ratio_pn_over_em_avg": 2.27,
        ...,

        "pn_build_faster":  3,    # portage-ng build wall < emerge build
        "em_build_faster": 35,    # emerge build wall < portage-ng build
        "build_tied":       0,
        "pn_plan_faster":   ...,
        ...
      },
      "entries": {
        "<cpv-or-safe-label>": {
          "session":     "compare-dev_libs_popt-20260515T161353",
          "session_dir": "/srv/tinderbox-ng/logs/compare-...",
          "label":       "dev_libs_popt",          # safe label as
                                                   # encoded in dirname
                                                   # (lossy: '-' and '/'
                                                   # both flatten to '_')
          "cpv":         "dev-libs/popt-1.19-r1"   # when recoverable
                                                   # from log content,
          "portage_ng_plan":  {"started": 1772248866,
                               "ended":   1772248870,
                               "wall_time_ms": 4123,
                               "rc": 0},
          "portage_ng_build": {...},
          "emerge_plan":      {...},
          "emerge_build":     {...},
          "ratios": {
            "plan_pn_over_em":  2.27,    # null if either side missing
            "build_pn_over_em": 1.48
          },
          "phase_stats": {                 # only when phase_stats.pl present
            "compile":    {"seconds": 14.2,  "bytes":  1024000},
            "install":    {"seconds":  2.1,  "bytes":   524288},
            ...
          }
        },
        ...
      }
    }

Wall times are integer milliseconds. ``ratios`` are floats rounded to 4
decimal places, or ``null`` when the matching pass didn't complete.

The script is stdlib-only Python 3, mirroring the rest of
``libexec/tinderbox-ng/``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from statistics import median
from typing import Dict, Iterable, List, Optional, Tuple


# =============================================================================
# Per-pass timing extraction
# =============================================================================

# .timing files are key=value, one pair per line. We accept whitespace
# around the '=' to be forgiving.
_TIMING_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(.*?)\s*$")


def _read_timing_file(path: Path) -> Optional[Dict[str, int]]:
    """Parse a <log>.timing companion file. Returns None on read error."""
    try:
        data = path.read_text(errors="replace")
    except OSError:
        return None
    out: Dict[str, int] = {}
    for line in data.splitlines():
        m = _TIMING_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        try:
            out[key] = int(val)
        except ValueError:
            # rc could in principle be non-numeric ("?"). Tolerate it.
            continue
    return out


def _read_exit_file(path: Path) -> Optional[int]:
    """Parse a <log>.exit file. Returns None on read error or empty file."""
    try:
        s = path.read_text(errors="replace").strip()
    except OSError:
        return None
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _mtime_fallback(log_path: Path) -> Optional[Dict[str, int]]:
    """Reconstruct timing from file ctime + mtime when no .timing file exists.

    ctime is closer to "first write" than birthtime on Linux (which is rarely
    populated reliably), and mtime is the moment the last byte was flushed.
    The delta is therefore a close approximation of wall time spent writing
    output, which for a tinderbox-ng pass is dominated by the actual command.
    """
    try:
        st = log_path.stat()
    except OSError:
        return None
    ctime = int(st.st_ctime)
    mtime = int(st.st_mtime)
    if mtime < ctime:
        # Clock skew / FS quirks. Treat as missing rather than producing
        # negative wall time.
        return None
    return {
        "started":      ctime,
        "ended":        mtime,
        "wall_time_ms": (mtime - ctime) * 1000,
    }


def _pass_timing(logdir: Path, log_basename: str) -> Optional[dict]:
    """Resolve timing for one pass. Returns None when the log itself is
    missing (i.e. that pass never ran). Otherwise returns a dict with
    ``started``, ``ended``, ``wall_time_ms``, plus ``rc`` from the .exit
    file when available, plus ``mtime_estimated: true`` when the timing
    came from the mtime fallback rather than a .timing file.
    """
    log_path = logdir / log_basename
    if not log_path.is_file():
        return None

    timing_path = logdir / f"{log_basename}.timing"
    exit_path   = logdir / f"{log_basename}.exit"

    timing = _read_timing_file(timing_path) if timing_path.is_file() else None
    if not timing or "wall_time_ms" not in timing:
        # Older session or partial run. Fall back to mtime delta.
        fb = _mtime_fallback(log_path)
        if not fb:
            return None
        out = dict(fb)
        out["mtime_estimated"] = True
    else:
        out = {
            "started":      timing.get("started", 0),
            "ended":        timing.get("ended", 0),
            "wall_time_ms": timing["wall_time_ms"],
        }

    rc = _read_exit_file(exit_path) if exit_path.is_file() else timing.get("rc") if timing else None
    if rc is not None:
        out["rc"] = rc
    return out


# =============================================================================
# Phase stats integration (portage-ng only)
# =============================================================================

# Matches phase_seconds(Entry, Phase, Seconds). and phase_bytes(...)
# emitted by portage-ng's writer; we reuse the regex from
# consolidate-phase-stats.py rather than importing it, to keep
# extract-timing.py independently runnable.
_PHASE_SECS_RE  = re.compile(
    r"^phase_seconds\(\s*(?P<entry>[^,]+?)\s*,\s*"
    r"(?P<phase>[a-zA-Z_][a-zA-Z0-9_]*)\s*,\s*"
    r"(?P<value>[0-9]+(?:\.[0-9]+)?)\s*\)\s*\.\s*$"
)
_PHASE_BYTES_RE = re.compile(
    r"^phase_bytes\(\s*(?P<entry>[^,]+?)\s*,\s*"
    r"(?P<phase>[a-zA-Z_][a-zA-Z0-9_]*)\s*,\s*"
    r"(?P<value>[0-9]+)\s*\)\s*\.\s*$"
)


def _read_phase_stats(path: Path) -> Optional[Dict[str, Dict[str, float]]]:
    """Aggregate the phase_stats.pl in a single session into per-phase
    summary {phase: {seconds: total, bytes: total}}.

    A single session typically has one (Entry, Phase) row per phase per
    package built (deps + target). We collapse to per-phase totals because
    the consumer here is "wall-time per session"; the per-(Entry, Phase)
    detail is consolidate-phase-stats.py's job, not ours.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None

    secs:  Dict[str, float] = {}
    bytes_: Dict[str, int]  = {}
    for line in text.splitlines():
        m = _PHASE_SECS_RE.match(line)
        if m:
            phase = m.group("phase")
            try:
                secs[phase] = secs.get(phase, 0.0) + float(m.group("value"))
            except ValueError:
                pass
            continue
        m = _PHASE_BYTES_RE.match(line)
        if m:
            phase = m.group("phase")
            try:
                bytes_[phase] = bytes_.get(phase, 0) + int(m.group("value"))
            except ValueError:
                pass
    if not secs and not bytes_:
        return None
    out: Dict[str, Dict[str, float]] = {}
    for phase in set(secs.keys()) | set(bytes_.keys()):
        d: Dict[str, float] = {}
        if phase in secs:
            d["seconds"] = round(secs[phase], 3)
        if phase in bytes_:
            d["bytes"] = bytes_[phase]
        out[phase] = d
    return out


# =============================================================================
# Session / atom resolution
# =============================================================================

# Session dir name format: compare-<safe-label>-<stamp>
# where <safe-label> is the package atom run through `tr -c '[:alnum:]' '_'`
# in cmd_compare. That encoding is *lossy*: both '-' and '/' get flattened
# to '_', so `app-misc/jq` and `app/misc/jq` and `app-misc-jq` all round-trip
# to the same `app_misc_jq` safe label. We therefore do NOT try to decode
# the safe label back to cat/pn here -- we keep it as-is for fallback
# entry-key purposes, and recover the canonical CPV from the log content
# (which is the only authoritative source).
_SESSION_DIR_RE = re.compile(
    r"^compare-(?P<safe>.+?)-(?P<stamp>\d{8}T\d{6})$"
)


def _safe_label_from_session_dirname(name: str) -> Optional[str]:
    m = _SESSION_DIR_RE.match(name)
    return m.group("safe") if m else None


# CPV recovery from a salvaged target log filename.
#   emerge.target.<safe>.<cat>:<pf>:<stamp>.log[.gz]
# emerge writes <pf> (portage's canonical version-with-revision string),
# so this is the most precise CPV source when present. portage-ng's target
# log is just <engine>.target.<safe>.build.log (no version), so we ignore
# that one for CPV.
_EM_TARGET_RE = re.compile(
    r"^emerge\.target\.[^.]+\.(?P<cat>[^:]+):(?P<pf>[^:]+):"
    r"\d{8}-\d{6}\.log(?:\.gz)?$"
)


def _cpv_from_target_logs(logdir: Path) -> Optional[str]:
    for entry in logdir.iterdir():
        m = _EM_TARGET_RE.match(entry.name)
        if not m:
            continue
        return f"{m.group('cat')}/{m.group('pf')}"
    return None


# Resolver-line CPV recovery. Each engine emits unambiguous atom lines:
#   emerge -vp:    [ebuild   N  ~ ] dev-libs/popt-1.19-r1::gentoo  USE="..."
#   portage-ng:    >>> Emerging : portage://dev-libs/popt-1.19-r1:run?{[]}
# In both engines' plan output, deps come first and the *target* package
# comes last. We therefore take the LAST matching line in each plan log;
# that's the target with vanishingly few false positives.
_EMERGE_PLAN_ATOM_RE = re.compile(
    r"^\[ebuild\s+[A-Z][^]]*\]\s+(?P<atom>[A-Za-z0-9+_][A-Za-z0-9+_./-]*-\d[A-Za-z0-9+_./-]*)"
)
_PORTAGE_NG_PLAN_ATOM_RE = re.compile(
    r"Emerging\s*:\s*portage://(?P<atom>[A-Za-z0-9+_][A-Za-z0-9+_./-]*-\d[A-Za-z0-9+_./-]*)"
)


def _cpv_from_plan_log(path: Path, regex: re.Pattern) -> Optional[str]:
    if not path.is_file():
        return None
    last: Optional[str] = None
    try:
        with path.open("r", errors="replace") as f:
            for line in f:
                m = regex.search(line)
                if m:
                    last = m.group("atom")
    except OSError:
        return None
    return last


def _recover_cpv(session_dir: Path) -> Optional[str]:
    """Try the strongest source first, then fall back."""
    cpv = _cpv_from_target_logs(session_dir)
    if cpv:
        return cpv
    cpv = _cpv_from_plan_log(session_dir / "emerge.plan.log",
                             _EMERGE_PLAN_ATOM_RE)
    if cpv:
        return cpv
    return _cpv_from_plan_log(session_dir / "portage-ng.plan.log",
                              _PORTAGE_NG_PLAN_ATOM_RE)


# =============================================================================
# Per-session timing extraction
# =============================================================================

PASSES = (
    ("portage_ng_plan",  "portage-ng.plan.log"),
    ("portage_ng_build", "portage-ng.build.log"),
    ("emerge_plan",      "emerge.plan.log"),
    ("emerge_build",     "emerge.build.log"),
)


def extract_session(session_dir: Path) -> Optional[dict]:
    """Build the entry dict for one compare session directory. Returns
    None when the directory doesn't look like a compare session at all
    (no logs found)."""
    if not session_dir.is_dir():
        return None

    passes: Dict[str, dict] = {}
    any_log = False
    any_mtime_fallback = False

    for key, basename in PASSES:
        timing = _pass_timing(session_dir, basename)
        if timing is not None:
            any_log = True
            if timing.get("mtime_estimated"):
                any_mtime_fallback = True
            passes[key] = timing

    if not any_log:
        return None

    label = _safe_label_from_session_dirname(session_dir.name)
    cpv = _recover_cpv(session_dir)

    entry: Dict = {
        "session":     session_dir.name,
        "session_dir": str(session_dir.resolve()),
    }
    if label:
        entry["label"] = label
    if cpv:
        entry["cpv"] = cpv

    entry.update(passes)
    if any_mtime_fallback:
        entry["mtime_estimated"] = True

    # Ratios
    ratios: Dict[str, Optional[float]] = {
        "plan_pn_over_em":  None,
        "build_pn_over_em": None,
    }
    if "portage_ng_plan" in passes and "emerge_plan" in passes:
        em_plan = passes["emerge_plan"].get("wall_time_ms", 0)
        if em_plan > 0:
            ratios["plan_pn_over_em"] = round(
                passes["portage_ng_plan"].get("wall_time_ms", 0) / em_plan, 4
            )
    if "portage_ng_build" in passes and "emerge_build" in passes:
        em_build = passes["emerge_build"].get("wall_time_ms", 0)
        if em_build > 0:
            ratios["build_pn_over_em"] = round(
                passes["portage_ng_build"].get("wall_time_ms", 0) / em_build, 4
            )
    entry["ratios"] = ratios

    # Phase stats (portage-ng only, only when build pass ran successfully).
    phase_stats_path = session_dir / "phase_stats.pl"
    if phase_stats_path.is_file():
        phs = _read_phase_stats(phase_stats_path)
        if phs:
            entry["phase_stats"] = phs

    return entry


# =============================================================================
# Source discovery
# =============================================================================

# Per-package wrapper log inside a matrix run dir. cmd_compare inside a
# matrix worker writes lines like:
#     [tinderbox-ng] running both engines in parallel (logs: /srv/.../compare-...)
# We grep the wrapper log for that path. _ANALYZE_LOGDIR_RE is the
# permissive form of compare_run_pass's inner echo.
_ANALYZE_LOGDIR_RE = re.compile(
    r"(/srv/tinderbox-ng/logs/compare-[^\s)]+)"
)


def _enumerate_sessions_from_run(run_dir: Path) -> Iterable[Path]:
    """Each matrix run wrapper log embeds the absolute path of its
    per-package compare session dir. Grep them out."""
    for wrapper in run_dir.iterdir():
        if not wrapper.is_file() or not wrapper.name.endswith(".log"):
            continue
        if wrapper.name.startswith("all-packages-driver"):
            continue
        try:
            text = wrapper.read_text(errors="replace")
        except OSError:
            continue
        m = _ANALYZE_LOGDIR_RE.search(text)
        if not m:
            continue
        yield Path(m.group(1))


def _enumerate_sessions_from_dir(parent: Path) -> Iterable[Path]:
    for entry in parent.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("compare-"):
            yield entry


# =============================================================================
# Aggregation
# =============================================================================


def _percentile(data: List[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def _summarize_pass(entries: Iterable[dict], key: str) -> Dict[str, float]:
    times = [e[key]["wall_time_ms"] for e in entries
             if key in e and "wall_time_ms" in e[key]]
    if not times:
        return {f"{key}_count": 0}
    return {
        f"{key}_count":  len(times),
        f"{key}_avg_ms": round(sum(times) / len(times), 1),
        f"{key}_p50_ms": int(round(median(times))),
        f"{key}_p95_ms": int(round(_percentile(times, 95))),
        f"{key}_max_ms": max(times),
    }


def _summarize_ratios(entries: Iterable[dict], ratio_key: str) -> Dict[str, float]:
    vals = [e["ratios"][ratio_key]
            for e in entries
            if "ratios" in e
            and e["ratios"].get(ratio_key) is not None]
    if not vals:
        return {f"{ratio_key}_count": 0}
    return {
        f"{ratio_key}_count": len(vals),
        f"{ratio_key}_avg":   round(sum(vals) / len(vals), 4),
        f"{ratio_key}_p50":   round(median(vals), 4),
        f"{ratio_key}_p95":   round(_percentile(vals, 95), 4),
        f"{ratio_key}_max":   round(max(vals), 4),
    }


def compute_summary(entries: Dict[str, dict]) -> Dict[str, float]:
    rows = list(entries.values())
    summary: Dict[str, float] = {
        "sessions_total":           len(rows),
        "sessions_mtime_fallback":  sum(1 for r in rows if r.get("mtime_estimated")),
    }
    for key, _basename in PASSES:
        summary.update(_summarize_pass(rows, key))
    summary.update(_summarize_ratios(rows, "plan_pn_over_em"))
    summary.update(_summarize_ratios(rows, "build_pn_over_em"))

    # Head-to-head wins. We only count sessions where BOTH passes have
    # a wall_time_ms.
    def _h2h(plan_or_build: str) -> Tuple[int, int, int]:
        pn_key = f"portage_ng_{plan_or_build}"
        em_key = f"emerge_{plan_or_build}"
        pn_faster = em_faster = tied = 0
        for r in rows:
            if pn_key not in r or em_key not in r:
                continue
            pn_ms = r[pn_key].get("wall_time_ms")
            em_ms = r[em_key].get("wall_time_ms")
            if pn_ms is None or em_ms is None:
                continue
            if pn_ms < em_ms:
                pn_faster += 1
            elif em_ms < pn_ms:
                em_faster += 1
            else:
                tied += 1
        return pn_faster, em_faster, tied

    pn_pf, em_pf, tied_p = _h2h("plan")
    pn_bf, em_bf, tied_b = _h2h("build")
    summary["pn_plan_faster"]  = pn_pf
    summary["em_plan_faster"]  = em_pf
    summary["plan_tied"]       = tied_p
    summary["pn_build_faster"] = pn_bf
    summary["em_build_faster"] = em_bf
    summary["build_tied"]      = tied_b
    return summary


# =============================================================================
# Main
# =============================================================================


def _resolve_sources(args: argparse.Namespace) -> Tuple[List[Path], str, Path]:
    """Returns (session_dirs, source_kind, source_root)."""
    chosen = [(args.session,  "session"),
              (args.run,      "run"),
              (args.logdir,   "logdir"),
              (args.src,      "src")]
    chosen = [(v, k) for v, k in chosen if v]
    if len(chosen) != 1:
        sys.exit(
            "extract-timing: pick exactly one of --session, --run, --logdir, --src"
        )
    raw, kind = chosen[0]
    root = Path(raw).resolve()
    if not root.exists():
        sys.exit(f"extract-timing: {root} does not exist")
    if kind == "session":
        if not root.is_dir():
            sys.exit(f"extract-timing: --session {root} is not a directory")
        return [root], kind, root
    if not root.is_dir():
        sys.exit(f"extract-timing: --{kind} {root} is not a directory")
    if kind == "run":
        sessions = list(_enumerate_sessions_from_run(root))
    else:
        sessions = list(_enumerate_sessions_from_dir(root))
    return sessions, kind, root


def main() -> int:
    p = argparse.ArgumentParser(
        prog="extract-timing.py",
        description="Extract wall-clock timing from tinderbox-ng compare sessions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--src",     help="Directory containing compare-* subdirs (e.g. /srv/tinderbox-ng/logs).")
    p.add_argument("--logdir",  help="Equivalent to --src; preserved for symmetry with `analyze`/`phase-stats`.")
    p.add_argument("--run",     help="Matrix run dir (compare-matrix-<stamp>/); follow per-package wrapper logs.")
    p.add_argument("--session", help="A single compare-<label>-<stamp>/ directory.")
    p.add_argument("--out",     required=True, help="Output JSON path.")
    p.add_argument("--quiet",   action="store_true", help="Suppress per-session progress on stderr.")
    args = p.parse_args()

    session_dirs, source_kind, source_root = _resolve_sources(args)
    if not session_dirs:
        print(f"extract-timing: no compare-* sessions found under {source_root}",
              file=sys.stderr)

    entries: Dict[str, dict] = {}
    skipped = 0
    for sd in sorted(session_dirs):
        entry = extract_session(sd)
        if entry is None:
            skipped += 1
            if not args.quiet:
                print(f"  skip: {sd.name} (no compare logs)", file=sys.stderr)
            continue
        # Key by cpv if known (most useful), else by session label, else
        # fall back to the bare session dirname (always unique).
        key = entry.get("cpv") or entry.get("label") or entry["session"]
        # Handle the rare case where two sessions share a key (e.g. the
        # same package run twice in different matrix runs piped into one
        # --src). Disambiguate with the session dirname.
        if key in entries:
            key = f"{key}@{entry['session']}"
        entries[key] = entry
        if not args.quiet:
            print(f"  ok:   {sd.name} -> {key}", file=sys.stderr)

    summary = compute_summary(entries)
    out_obj = {
        "source":      str(source_root),
        "source_kind": source_kind,
        "summary":     summary,
        "entries":     entries,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_obj, indent=2, sort_keys=False))

    print(f"extract-timing: {len(entries)} session(s), {skipped} skipped",
          file=sys.stderr)
    print(f"  source ({source_kind}): {source_root}", file=sys.stderr)
    print(f"  out:                    {out_path}", file=sys.stderr)
    print("  summary:", file=sys.stderr)
    for k, v in summary.items():
        print(f"    {k}: {v}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
