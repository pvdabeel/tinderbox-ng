#!/usr/bin/env python3
"""
portage-ng-exit-label.py - map portage-ng process exit codes to tinderbox-ng labels

Reads interface:exit_code/3 facts from portage-ng's exitcodes.pl (the single
source of truth in the portage-ng tree) and returns the short status string
compare-matrix / progress use instead of bare FAIL(N).

Success (plan produced):
  0 -> OK
  1 -> OK(cycles)
  2 -> OK(assumed)

Failure (FAIL(*) matches emerge's FAIL(N) shape):
  execution_failed / rc 3 -> FAIL(exec) or FAIL(target) via log heuristics
  invalid_targets      -> FAIL(target)
  cli_error            -> FAIL(cli)
  *                    -> FAIL(N)

Special (handled by tinderbox-ng after this helper returns):
  RESTRICT(fetch), INFRA(overlay-inode-flicker) — log-based reclassifiers in
  _compare_summarize overwrite FAIL(exec) when applicable.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

EXIT_CODE_RE = re.compile(
    r"interface:exit_code\(\s*(\w+)\s*,\s*(\d+)\s*,"
)

# tinderbox-ng display labels keyed by portage-ng symbolic exit-code name.
NAME_LABEL: dict[str, str] = {
    "clean": "OK",
    "cycle_breaks": "OK(cycles)",
    "domain_assumptions": "OK(assumed)",
    "execution_failed": "FAIL(exec)",
    "invalid_targets": "FAIL(target)",
    "cli_error": "FAIL(cli)",
}

# Numeric fallbacks for codes used in portage-ng before they land in exitcodes.pl
# (see Source/Application/Interface/Action/build.pl).
FALLBACK_CODE_NAME: dict[int, str] = {
    0: "clean",
    1: "cycle_breaks",
    2: "domain_assumptions",
    3: "execution_failed",
}

PLAN_PRODUCED_RE = re.compile(r"Total:\s*\d+\s+actions?", re.IGNORECASE)
INVALID_TARGET_RE = re.compile(
    r"No valid targets found|No targets specified for --build",
    re.IGNORECASE,
)
BUILD_FAILED_RE = re.compile(
    r"Failed:\s*[1-9]|Total:\s*\d+\s+completed|\[builder\] exception",
    re.IGNORECASE,
)


def parse_exitcodes(path: Path) -> dict[int, str]:
    table: dict[int, str] = {}
    if not path.is_file():
        return table
    for name, code_s in EXIT_CODE_RE.findall(path.read_text(errors="replace")):
        table[int(code_s)] = name
    return table


def name_to_label(name: str) -> str:
    if name in NAME_LABEL:
        return NAME_LABEL[name]
    return f"FAIL({name.replace('_', '-')})"


def _read(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    return path.read_text(errors="replace")


def plan_produced(plan_log: Path | None, build_log: Path | None) -> bool:
    if PLAN_PRODUCED_RE.search(_read(plan_log)):
        return True
    # Build pass only runs after a successful plan gate (rc <= 2).
    if build_log and build_log.is_file() and build_log.stat().st_size > 0:
        return True
    return False


def refine_execution_failed(
    mode: str, plan_log: Path | None, build_log: Path | None
) -> str:
    blob = "\n".join((_read(plan_log), _read(build_log)))
    if INVALID_TARGET_RE.search(blob):
        return "FAIL(target)"
    if BUILD_FAILED_RE.search(blob):
        return "FAIL(exec)"
    if mode == "--pretend":
        return "FAIL(target)"
    return "FAIL(exec)"


def label_for(
    rc: int | str,
    *,
    mode: str = "--build",
    exitcodes: Path | None = None,
    plan_log: Path | None = None,
    build_log: Path | None = None,
) -> str:
    if rc in ("?", "", None):
        return "FAIL(?)"

    rc_i = int(rc)

    if rc_i == 124:
        return "TIMEOUT"
    if rc_i == 137:
        return "KILLED(SIGKILL)"
    if rc_i == 143:
        return "KILLED(SIGTERM)"

    table = parse_exitcodes(exitcodes) if exitcodes else {}
    name = table.get(rc_i) or FALLBACK_CODE_NAME.get(rc_i)

    if name is None:
        return f"FAIL({rc_i})"

    if rc_i in (0, 1, 2):
        if rc_i == 0 or plan_produced(plan_log, build_log):
            return name_to_label(name)
        if rc_i == 1:
            return "FAIL(cli)"
        return f"FAIL({rc_i})"

    label = name_to_label(name)
    if name == "execution_failed" or (rc_i == 3 and label == "FAIL(exec)"):
        return refine_execution_failed(mode, plan_log, build_log)
    if name == "invalid_targets":
        return "FAIL(target)"
    return label


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rc", required=True, help="Numeric exit code (or ?)")
    ap.add_argument("--mode", default="--build", help="compare mode (--pretend|--build)")
    ap.add_argument(
        "--exitcodes",
        type=Path,
        default=None,
        help="Path to portage-ng Source/Application/Interface/exitcodes.pl",
    )
    ap.add_argument("--plan-log", type=Path, default=None)
    ap.add_argument("--build-log", type=Path, default=None)
    args = ap.parse_args()
    print(
        label_for(
            args.rc,
            mode=args.mode,
            exitcodes=args.exitcodes,
            plan_log=args.plan_log,
            build_log=args.build_log,
        )
    )


if __name__ == "__main__":
    main()
