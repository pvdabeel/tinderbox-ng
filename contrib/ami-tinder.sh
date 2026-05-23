#!/bin/bash
# contrib/ami-tinder.sh — ephemeral tinderbox-ng matrix driver for AWS Gentoo AMIs.
#
# Bake only this script into the AMI (as /root/bin/tinder) plus a login-shell
# alias in ~/.bash_profile:
#   alias tinder='/root/bin/tinder'
#
# myaws invokes:  ssh root@host "bash -icl tinder"
#
# Everything else is cloned at run time from GitHub (pvdabeel/tinderbox-ng,
# pvdabeel/portage-ng), installed under /usr/local/share/, and exercised under
# a tmpfs-backed $TINDERBOX_ROOT (no extra EBS).  The instance is terminated
# afterward by myaws — nothing on tmpfs survives.
#
# Environment overrides (all optional):
#   TINDERBOX_NG_REF / PORTAGE_NG_REF   git refs (default: main, then master)
#   TINDER_MANIFEST                     manifest path under share/tinderbox-ng/
#   TINDER_JOBS                         compare-matrix --jobs (auto if unset)
#   TINDER_MATRIX_MODE                  --pretend (default) or --build
#   TINDER_SKIP_BOOTSTRAP=1             skip bootstrap (baseline must exist)
#   TINDER_SKIP_MATRIX=1                bootstrap/doctor only
#   TINDER_TMPFS_SIZE                   override tmpfs cap (e.g. 1200G)
#   TINDERBOX_REBOOTSTRAP=1             replace an existing baseline
#   NPROC                               inner MAKEOPTS / emerge --jobs pin

set -euo pipefail

TINDERBOX_NG_URL="${TINDERBOX_NG_URL:-https://github.com/pvdabeel/tinderbox-ng.git}"
PORTAGE_NG_URL="${PORTAGE_NG_URL:-https://github.com/pvdabeel/portage-ng.git}"
TINDERBOX_NG_REF="${TINDERBOX_NG_REF:-main}"
PORTAGE_NG_REF="${PORTAGE_NG_REF:-master}"
TINDERBOX_ROOT="${TINDERBOX_ROOT:-/srv/tinderbox-ng}"
TINDERBOX_INSTALL="${TINDERBOX_INSTALL:-/usr/local/share/tinderbox-ng}"
PORTAGE_NG_CLONE="${PORTAGE_NG_CLONE:-/usr/local/share/portage-ng}"
TINDERBOX_ENTRY="${TINDERBOX_ENTRY:-/usr/local/sbin/tinderbox-ng}"
COMPARE_MATRIX="${COMPARE_MATRIX:-/usr/local/bin/compare-matrix}"
SHARE_DIR="${TINDERBOX_INSTALL}/share/tinderbox-ng"

_log() { printf '[tinder] %s\n' "$*" >&2; }
_die() { _log "ERROR: $*"; exit 1; }

_usage() {
  cat <<'EOF'
Usage: tinder [--help]

Ephemeral tinderbox-ng run: clone repos, tmpfs $TINDERBOX_ROOT, doctor,
bootstrap, compare-matrix, print progress stats.

See script header for environment overrides.
EOF
}

_mem_gib() {
  local kib
  kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
  echo $(( (kib + 1048575) / 1048576 ))
}

# Scale tmpfs / jobs / ccache / default manifest to available RAM.
_auto_profile() {
  local gib="$1"
  if (( gib >= 1000 )); then
    TINDER_TMPFS_SIZE="${TINDER_TMPFS_SIZE:-1200G}"
    TINDER_JOBS="${TINDER_JOBS:-192}"
    TINDER_CCACHE="${TINDER_CCACHE:-80G}"
    TINDER_MANIFEST="${TINDER_MANIFEST:-manifest-1000.txt}"
    NPROC="${NPROC:-32}"
  elif (( gib >= 120 )); then
    TINDER_TMPFS_SIZE="${TINDER_TMPFS_SIZE:-$(( gib * 75 / 100 ))G}"
    TINDER_JOBS="${TINDER_JOBS:-$(( gib / 8 ))}"
    TINDER_CCACHE="${TINDER_CCACHE:-$(( gib / 4 ))G}"
    TINDER_MANIFEST="${TINDER_MANIFEST:-manifest-100.txt}"
    NPROC="${NPROC:-32}"
  else
    TINDER_TMPFS_SIZE="${TINDER_TMPFS_SIZE:-$(( gib * 70 / 100 ))G}"
    TINDER_JOBS="${TINDER_JOBS:-$(( gib / 8 ))}"
    (( TINDER_JOBS < 1 )) && TINDER_JOBS=1
    (( TINDER_JOBS > 16 )) && TINDER_JOBS=16
    TINDER_CCACHE="${TINDER_CCACHE:-2G}"
    TINDER_MANIFEST="${TINDER_MANIFEST:-manifest-100.txt}"
    NPROC="${NPROC:-$(nproc)}"
  fi
  TINDER_MATRIX_MODE="${TINDER_MATRIX_MODE:---pretend}"
  case "$TINDER_MATRIX_MODE" in
    --pretend|--build) ;;
    *) _die "TINDER_MATRIX_MODE must be --pretend or --build, got: $TINDER_MATRIX_MODE" ;;
  esac
}

# portage-ng config.pl requires Source/Config/Private/{passwords,api_key}.pl
# (gitignored; templates ship in-repo). Stub before bootstrap rsync.
_ensure_portage_ng_private_config() {
  local png_root="$1"
  local priv="$png_root/Source/Config/Private"
  local pair dst tpl

  [[ -d "$png_root" ]] || return 0
  install -d "$priv"
  for pair in api_key passwords; do
    dst="$priv/${pair}.pl"
    tpl="$priv/template_${pair}.pl"
    [[ -f "$dst" ]] && continue
    if [[ -f "$tpl" ]]; then
      cp "$tpl" "$dst"
      _log "stub $dst (from template)"
    else
      : >"$dst"
      _log "stub $dst (empty; template missing)"
    fi
  done
}

_clone_repo() {
  local url="$1" dest="$2" ref="$3"
  _log "clone $url -> $dest @ $ref"
  install -d "$(dirname "$dest")"
  if [[ -d "$dest/.git" ]]; then
    git -C "$dest" fetch --depth 1 origin "$ref" \
      || git -C "$dest" fetch --depth 1 origin "master" \
      || git -C "$dest" fetch --depth 1 origin "main"
    git -C "$dest" reset --hard FETCH_HEAD
    return 0
  fi
  rm -rf "$dest"
  if ! git clone --depth 1 --branch "$ref" "$url" "$dest" 2>/dev/null; then
    git clone --depth 1 "$url" "$dest"
    git -C "$dest" checkout "$ref" 2>/dev/null \
      || git -C "$dest" checkout master 2>/dev/null \
      || git -C "$dest" checkout main
  fi
}

_install_tinderbox_ng() {
  _clone_repo "$TINDERBOX_NG_URL" "$TINDERBOX_INSTALL" "$TINDERBOX_NG_REF"
  _clone_repo "$PORTAGE_NG_URL" "$PORTAGE_NG_CLONE" "$PORTAGE_NG_REF"
  _ensure_portage_ng_private_config "$PORTAGE_NG_CLONE"

  install -d /usr/local/sbin /usr/local/bin
  ln -sfn "$TINDERBOX_INSTALL/bin/tinderbox-ng" "$TINDERBOX_ENTRY"
  chmod +x "$TINDERBOX_INSTALL/bin/tinderbox-ng"
  ln -sfn "$TINDERBOX_INSTALL/libexec/tinderbox-ng/compare-matrix.sh" "$COMPARE_MATRIX"
  chmod +x "$TINDERBOX_INSTALL/libexec/tinderbox-ng/compare-matrix.sh"
  _log "entry: $TINDERBOX_ENTRY"
}

_mount_tmpfs() {
  local size="$1"
  install -d "$TINDERBOX_ROOT"
  if mountpoint -q "$TINDERBOX_ROOT" 2>/dev/null; then
    local fstype
    fstype="$(findmnt -no FSTYPE --target "$TINDERBOX_ROOT" 2>/dev/null || true)"
    _log "$TINDERBOX_ROOT already mounted ($fstype)"
    return 0
  fi
  _log "mount tmpfs size=$size on $TINDERBOX_ROOT"
  mount -t tmpfs -o "size=$size,mode=0755,nosuid,nodev" tmpfs "$TINDERBOX_ROOT"
  install -d "$TINDERBOX_ROOT/shared/distfiles" \
           "$TINDERBOX_ROOT/shared/binpkgs" \
           "$TINDERBOX_ROOT/shared/ccache" \
           "$TINDERBOX_ROOT/sessions" \
           "$TINDERBOX_ROOT/logs" \
           "$TINDERBOX_ROOT/reports"
}

_print_stats() {
  local run
  run="$(ls -td "$TINDERBOX_ROOT/reports/compare-matrix-"* 2>/dev/null | head -1 || true)"
  [[ -n "$run" ]] || { _log "no compare-matrix run dir under $TINDERBOX_ROOT/reports/"; return 0; }
  _log "matrix run: $run"
  echo ""
  echo "========== tinderbox-ng matrix statistics =========="
  "$TINDERBOX_ENTRY" progress --run "$run" 2>/dev/null || true
  if [[ -f "$run/results.tsv" ]]; then
    echo ""
    echo "results.tsv: $run/results.tsv"
    awk -F'\t' '
      NR == 1 { next }
      { n++ }
      END { if (n) printf "completed rows: %d\n", n }
    ' "$run/results.tsv"
  fi
  echo "===================================================="
}

main() {
  [[ "${1:-}" != "--help" && "${1:-}" != "-h" ]] || { _usage; exit 0; }

  [[ "$(id -u)" -eq 0 ]] || _die "must run as root"

  local gib
  gib="$(_mem_gib)"
  _auto_profile "$gib"
  _log "host: $(hostname)  mem=${gib}GiB  nproc=$(nproc)"
  _log "profile: tmpfs=$TINDER_TMPFS_SIZE jobs=$TINDER_JOBS ccache=$TINDER_CCACHE manifest=$TINDER_MANIFEST mode=$TINDER_MATRIX_MODE NPROC=$NPROC"

  export TINDERBOX_ROOT
  export TINDERBOX_SESSIONS_TMPFS_SIZE=0
  export TINDERBOX_CCACHE_MAX_SIZE="$TINDER_CCACHE"
  export TINDERBOX_REBOOTSTRAP="${TINDERBOX_REBOOTSTRAP:-1}"
  export NPROC

  # Overlay-friendly dentry cache (belt-and-braces; see tinderbox-ng doctor).
  if [[ -w /proc/sys/vm/vfs_cache_pressure ]]; then
    echo 50 > /proc/sys/vm/vfs_cache_pressure 2>/dev/null || true
  fi

  _install_tinderbox_ng
  # Bootstrap rsyncs from PORTAGE_NG_LOCAL; empty URL avoids git-clone path.
  export PORTAGE_NG_LOCAL="$PORTAGE_NG_CLONE"
  export PORTAGE_NG_URL=

  _mount_tmpfs "$TINDER_TMPFS_SIZE"

  _log "doctor"
  "$TINDERBOX_ENTRY" doctor

  if [[ -z "${TINDER_SKIP_BOOTSTRAP:-}" ]]; then
    _log "bootstrap (long-running)"
    "$TINDERBOX_ENTRY" bootstrap
  else
    _log "skip bootstrap (TINDER_SKIP_BOOTSTRAP=1)"
  fi

  local manifest="$SHARE_DIR/$TINDER_MANIFEST"
  [[ -f "$manifest" ]] || _die "manifest not found: $manifest"

  if [[ -n "${TINDER_SKIP_MATRIX:-}" ]]; then
    _log "skip matrix (TINDER_SKIP_MATRIX=1)"
    _print_stats
    exit 0
  fi

  _log "compare-matrix $TINDER_MATRIX_MODE --jobs $TINDER_JOBS --manifest $manifest"
  set +e
  "$COMPARE_MATRIX" "$TINDER_MATRIX_MODE" --jobs "$TINDER_JOBS" --manifest "$manifest"
  local rc=$?
  set -e

  _print_stats
  exit "$rc"
}

main "$@"
