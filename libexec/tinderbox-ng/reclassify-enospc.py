#!/usr/bin/env python3
"""
reclassify-enospc.py — tag compare outcomes as INFRA(ENOSPC)

Disk-full is host/infra, not a resolver or ebuild bug. Compare already
reclassifies RESTRICT(fetch) and INFRA(overlay-inode-flicker) from log
markers; this helper does the same for ENOSPC.

Markers (any one is enough):
  No space left on device
  ENOSPC
  Cannot write to … (Success)     # wget's ENOSPC errno-as-Success
  os error 28                     # rustc / cargo

`--check DIR` prints `pn=0|1 em=0|1` for one compare logdir (used by
`_compare_summarize`). `--run DIR` walks a matrix `results.tsv`, follows
each wrapper's `logs: …` path, and rewrites FAIL(*) on that side to
INFRA(ENOSPC). A FAIL(build) whose timing ends within CLUSTER_S of an
explicit ENOSPC death and whose target log is truncated (no ebuild
ERROR footer) is tagged too — the KDE wave that died together when the
sessions tmpfs filled.
"""

from __future__ import annotations

import argparse
import gzip
import os
import re
import sys
from pathlib import Path

LABEL = "INFRA(ENOSPC)"
CLUSTER_S = 15

ENOSPC_RE = re.compile(
    r"No space left on device"
    r"|ENOSPC"
    r"|Cannot write to .{0,240}\(Success\)"
    r"|os error 28",
    re.IGNORECASE,
)
LOGS_RE = re.compile(r"logs:\s*(\S+?)\)")
ERROR_FOOTER_RE = re.compile(
    r"ERROR:\s+\S+\s+failed\s+\(",
    re.IGNORECASE,
)
FAIL_RE = re.compile(r"^FAIL")
KEEP_RE = re.compile(r"^(OK|RESTRICT|INFRA)")
# Plan-only FAIL(1)/FAIL(plan) almost never carry ENOSPC; scanning those
# (thousands of ok/fail rows) is what made a full-TSV walk too slow.
SCAN_RE = re.compile(r"^FAIL\(build\)|^KILLED")
LABEL_SAFE_RE = re.compile(r"[^A-Za-z0-9]+")
GZ_MAX_BYTES = 2 * 1024 * 1024
GZ_MAX_FILES = 40


def _read_text(path: Path, tail: int | None = None) -> str:
    try:
        if tail is None:
            return path.read_text(errors="replace")
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > tail:
                fh.seek(-tail, os.SEEK_END)
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _read_gz_tail(path: Path, tail: int = 262144) -> str:
    # Skip huge salvaged logs (qtwebengine, mesa). Those deaths are
    # caught by the ±CLUSTER_S truncated-FAIL(build) pass instead of
    # decompressing tens of megabytes per row.
    try:
        if path.stat().st_size > GZ_MAX_BYTES:
            return ""
        with gzip.open(path, "rb") as fh:
            data = fh.read()
        if len(data) > tail:
            data = data[-tail:]
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _blob_has_enospc(text: str) -> bool:
    return bool(text and ENOSPC_RE.search(text))


def _iter_engine_files(logdir: Path, engine: str) -> list[Path]:
    files: list[Path] = []
    for name in (
        f"{engine}.build.log",
        f"{engine}.plan.log",
    ):
        p = logdir / name
        if p.is_file():
            files.append(p)
    files.extend(sorted(logdir.glob(f"{engine}.target*")))
    salvaged = logdir / "build-logs" / engine
    if salvaged.is_dir():
        gz = [p for p in salvaged.rglob("*.build.log.gz") if p.stat().st_size <= GZ_MAX_BYTES]
        plain = list(salvaged.rglob("*.build.log"))
        # Newest first: the CPV that hit ENOSPC is usually the last write.
        gz.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        files.extend(plain)
        files.extend(gz[:GZ_MAX_FILES])
    return files


def engine_has_enospc(logdir: Path, engine: str) -> bool:
    if not logdir.is_dir():
        return False
    for path in _iter_engine_files(logdir, engine):
        if path.suffix == ".gz" or path.name.endswith(".log.gz"):
            text = _read_gz_tail(path)
        else:
            # wget ENOSPC is often early; rustc/ld ENOSPC is at the end.
            head = _read_text(path, tail=None) if path.stat().st_size < 1_000_000 else (
                _read_text(path, tail=524288)
            )
            if path.stat().st_size >= 1_000_000 and not _blob_has_enospc(head):
                with path.open("rb") as fh:
                    head = fh.read(65536).decode("utf-8", errors="replace") + head
            text = head
        if _blob_has_enospc(text):
            return True
    return False


def check_logdir(logdir: Path) -> tuple[bool, bool]:
    return engine_has_enospc(logdir, "portage-ng"), engine_has_enospc(
        logdir, "emerge"
    )


def _sanitize_atom(atom: str) -> str:
    s = atom.lstrip("=").replace("+", "p")
    return LABEL_SAFE_RE.sub("_", s).strip("_")


def _wrapper_logdir(run: Path, atom: str) -> Path | None:
    wrapper = run / f"{_sanitize_atom(atom)}.log"
    if not wrapper.is_file():
        return None
    m = LOGS_RE.search(_read_text(wrapper, tail=65536))
    if not m:
        return None
    d = Path(m.group(1))
    return d if d.is_dir() else None


def _timing_ended(logdir: Path, engine: str) -> int | None:
    p = logdir / f"{engine}.build.log.timing"
    if not p.is_file():
        p = logdir / f"{engine}.plan.log.timing"
    if not p.is_file():
        return None
    for line in _read_text(p).splitlines():
        if line.startswith("ended="):
            try:
                return int(float(line.split("=", 1)[1].strip()))
            except ValueError:
                return None
    return None


def _engine_truncated(logdir: Path, engine: str) -> bool:
    """True when the in-flight target log has no ebuild ERROR footer."""
    targets = list(logdir.glob(f"{engine}.target*"))
    if not targets:
        return True
    text = _read_text(targets[0], tail=65536)
    return not ERROR_FOOTER_RE.search(text)


def _should_rewrite(status: str) -> bool:
    if not status or KEEP_RE.match(status):
        return False
    return bool(FAIL_RE.match(status) or status.startswith("KILLED") or status == "TIMEOUT")


def _should_scan(status: str) -> bool:
    return bool(status and SCAN_RE.match(status))


def reclassify_run(run: Path, *, dry_run: bool = False, cluster_s: int = CLUSTER_S) -> int:
    tsv = run / "results.tsv"
    if not tsv.is_file():
        raise SystemExit(f"results.tsv not found in {run}")

    lines = tsv.read_text(errors="replace").splitlines()
    if not lines:
        return 0
    header = lines[0]
    rows = [line.split("\t") for line in lines[1:] if line.strip()]

    explicit: list[tuple[int, Path, str]] = []
    hits: dict[int, tuple[bool, bool]] = {}

    scanned = 0
    for i, cols in enumerate(rows):
        if len(cols) < 4:
            continue
        atom, pn, em = cols[0], cols[2], cols[3]
        if not (_should_scan(pn) or _should_scan(em)):
            continue
        scanned += 1
        if scanned % 50 == 0:
            print(f"scanned {scanned} FAIL(build)/KILLED rows…", file=sys.stderr)
        logdir = _wrapper_logdir(run, atom)
        if logdir is None:
            continue
        pn_hit, em_hit = check_logdir(logdir)
        if not pn_hit and not em_hit:
            continue
        hits[i] = (pn_hit, em_hit)
        ended = _timing_ended(logdir, "portage-ng") or _timing_ended(logdir, "emerge")
        if ended is not None:
            explicit.append((ended, logdir, atom))

    # Cluster: truncated FAIL(build) that died with an explicit ENOSPC peer.
    if cluster_s > 0 and explicit:
        explicit_ends = [t[0] for t in explicit]
        for i, cols in enumerate(rows):
            if i in hits or len(cols) < 4:
                continue
            pn, em = cols[2], cols[3]
            if not (_should_scan(pn) or _should_scan(em)):
                continue
            logdir = _wrapper_logdir(run, cols[0])
            if logdir is None:
                continue
            ended = _timing_ended(logdir, "portage-ng")
            if ended is None:
                continue
            if not any(abs(ended - e) <= cluster_s for e in explicit_ends):
                continue
            pn_hit = _should_rewrite(pn) and _engine_truncated(logdir, "portage-ng")
            em_hit = _should_rewrite(em) and engine_has_enospc(logdir, "emerge")
            if pn_hit or em_hit:
                hits[i] = (pn_hit, em_hit)

    changed = 0
    for i, (pn_hit, em_hit) in hits.items():
        cols = rows[i]
        if pn_hit and _should_rewrite(cols[2]) and cols[2] != LABEL:
            cols[2] = LABEL
            changed += 1
        if em_hit and _should_rewrite(cols[3]) and cols[3] != LABEL:
            cols[3] = LABEL
            changed += 1

    if dry_run or changed == 0:
        return changed

    lock = run / ".tsv.lock"
    payload = header + "\n" + "\n".join("\t".join(c) for c in rows) + "\n"
    tmp = tsv.with_suffix(".tsv.enospc.tmp")
    tmp.write_text(payload)
    # Best-effort flock via lockfile existence; compare-matrix holds
    # flock 9 on .tsv.lock during append. Atomic replace is enough for
    # progress readers (they re-read the whole file each frame).
    os.replace(tmp, tsv)
    lock.touch()
    return changed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", type=Path, help="one compare logdir; print pn= em=")
    ap.add_argument("--run", type=Path, help="matrix run dir; rewrite results.tsv")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--cluster-seconds",
        type=int,
        default=CLUSTER_S,
        help="tag truncated FAIL(build) ending within N seconds of an "
        f"explicit ENOSPC death (default {CLUSTER_S}; 0 disables)",
    )
    args = ap.parse_args()
    if args.check:
        pn, em = check_logdir(args.check)
        print(f"pn={int(pn)} em={int(em)}")
        return
    if args.run:
        n = reclassify_run(
            args.run, dry_run=args.dry_run, cluster_s=args.cluster_seconds
        )
        print(f"reclassified {n} exit label(s) -> {LABEL}")
        return
    ap.error("specify --check DIR or --run DIR")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
