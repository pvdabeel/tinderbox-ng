#!/usr/bin/env python3
"""
compare-portage-ng-split.py - split one portage-ng --build transcript

`tinderbox-ng compare --build` runs portage-ng once (`--build` already
plans then executes in a single SWI-Prolog process). This helper splits
the combined stdout into the legacy per-phase log files the compare
pipeline expects:

  portage-ng.plan.log   planner output ending at ``Total: N actions``
  portage-ng.build.log  binpkg index refresh + step execution

Timing metadata is estimated by line-fraction between pass start/end
wall clocks (good enough for extract-timing aggregates).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
PLAN_FOOTER_RE = re.compile(r"Total:\s+\d+\s+actions?", re.IGNORECASE)
BUILD_MARKERS = (
    re.compile(r"%\s*Binpkg:", re.IGNORECASE),
    re.compile(r"\[step\s+\d+\]", re.IGNORECASE),
    re.compile(r"Updated prolog knowledgebase\.\s*Binpkg variants:", re.IGNORECASE),
)


def strip_ansi(line: str) -> str:
    return ANSI_RE.sub("", line)


def is_build_marker(clean: str) -> bool:
    return any(p.search(clean) for p in BUILD_MARKERS)


def split_lines(lines: list[str]) -> tuple[list[str], list[str], int | None]:
    """Return (plan_lines, build_lines, split_index). split_index is the
    first line index routed to build.log (None when build never started)."""
    plan: list[str] = []
    build: list[str] = []
    saw_footer = False
    split_at: int | None = None

    for idx, line in enumerate(lines):
        clean = strip_ansi(line.rstrip("\n"))
        if not saw_footer:
            plan.append(line)
            if PLAN_FOOTER_RE.search(clean):
                saw_footer = True
            continue

        if split_at is None:
            if is_build_marker(clean):
                split_at = idx
                build.append(line)
            else:
                plan.append(line)
            continue

        build.append(line)

    return plan, build, split_at


def write_timing(
    path: Path,
    *,
    started: float,
    ended: float,
    rc: int,
) -> None:
    wall_ms = max(0, int((ended - started) * 1000))
    path.write_text(
        f"started={int(started)}\n"
        f"ended={int(ended)}\n"
        f"wall_time_ms={wall_ms}\n"
        f"rc={rc}\n"
    )


def plan_end_epoch(
    *,
    started: float,
    ended: float,
    total_lines: int,
    split_at: int | None,
    plan_end: float | None,
) -> float:
    """Prefer a caller-supplied plan/build boundary timestamp; otherwise
    estimate. Line-fraction alone is misleading when the build log is
    mostly binpkg index scrolling."""
    if plan_end is not None and started <= plan_end <= ended:
        return plan_end
    if split_at is None:
        return ended
    frac = split_at / (total_lines or 1)
    if frac < 0.15:
        # Empirically ~35-40% of a --build pass is planner + SWI cold
        # start; binpkg scroll dominates line count but not wall time.
        return started + (ended - started) * 0.38
    return started + (ended - started) * frac


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--combined", type=Path, required=True)
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--build", type=Path, required=True)
    ap.add_argument("--started", type=float, required=True, help="epoch seconds")
    ap.add_argument("--ended", type=float, required=True, help="epoch seconds")
    ap.add_argument("--rc", type=int, required=True)
    ap.add_argument("--meta", type=Path, required=True)
    ap.add_argument(
        "--plan-end",
        type=float,
        default=None,
        help="epoch seconds when build phase started (optional)",
    )
    args = ap.parse_args()

    text = args.combined.read_text(errors="replace")
    lines = text.splitlines(keepends=True)
    plan_lines, build_lines, split_at = split_lines(lines)

    args.plan.write_text("".join(plan_lines))
    if build_lines:
        args.build.write_text("".join(build_lines))

    total = len(lines) or 1
    plan_end = plan_end_epoch(
        started=args.started,
        ended=args.ended,
        total_lines=len(lines),
        split_at=split_at,
        plan_end=args.plan_end,
    )

    write_timing(
        Path(str(args.plan) + ".timing"),
        started=args.started,
        ended=plan_end,
        rc=args.rc,
    )
    if build_lines:
        write_timing(
            Path(str(args.build) + ".timing"),
            started=plan_end,
            ended=args.ended,
            rc=args.rc,
        )

    printf_exit = Path(str(args.plan) + ".exit")
    printf_exit.write_text(f"{args.rc}\n")
    if build_lines:
        Path(str(args.build) + ".exit").write_text(f"{args.rc}\n")

    meta = {
        "total_lines": len(lines),
        "plan_lines": len(plan_lines),
        "build_lines": len(build_lines),
        "split_at": split_at,
        "build_started": bool(build_lines),
    }
    args.meta.write_text(json.dumps(meta, indent=2) + "\n")


if __name__ == "__main__":
    main()
