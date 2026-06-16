#!/bin/bash
#
# contrib/deploy-host.sh - one-shot host install for tinderbox-ng
#
# Run from a tinderbox-ng repo checkout (the parent of bin/, libexec/ and
# share/). Installs on a remote VM, symlinks the entry point and the manpage
# onto $PATH / $MANPATH, runs `tinderbox-ng doctor` to confirm prerequisites,
# and (optionally) kicks off `bootstrap` followed by `selftest`.
#
# Everything is deployed straight from GitHub (same model as
# contrib/ami-tinder.sh on AWS): the tinderbox-ng tool is git-cloned/updated on
# the remote from TINDERBOX_NG_URL, and portage-ng is git-deployed into the
# baseline by `tinderbox-ng bootstrap` / `refresh-portage-ng` (PORTAGE_NG_URL @
# PORTAGE_NG_REF). There is no rsync and no on-host portage-ng checkout. Commit
# and push your work, then re-run this to roll it out.
#
# This script lives under contrib/ because it runs on the dev machine, never
# on the VM.
#
# Usage:
#   ./contrib/deploy-host.sh user@vm-host                    # git pull + doctor
#   ./contrib/deploy-host.sh --bootstrap user@vm-host        # + bootstrap + selftest
#   ./contrib/deploy-host.sh --refresh-portage-ng user@vm-host
#
# Idempotent. Safe to re-run after upstream git updates.

set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: deploy-host.sh [options] user@vm-host

Install: git only (like AWS ami-tinder). The tinderbox-ng tool is
cloned/updated on the remote from TINDERBOX_NG_URL; portage-ng is git-deployed
into the baseline by bootstrap/refresh-portage-ng (PORTAGE_NG_URL). No rsync,
no on-host portage-ng checkout. Commit and push, then re-run to roll out.

Options:
  --bootstrap            After install + doctor, run `tinderbox-ng bootstrap`
                         on the remote (long-running; ~hours). Then run
                         `tinderbox-ng selftest` to confirm the result.
  --refresh-portage-ng   After install, run `tinderbox-ng refresh-portage-ng`
                         on the remote (git fetch + reset --hard from
                         PORTAGE_NG_URL into the baseline; does NOT
                         regenerate kb.qlf).
  --refresh-baseline-config
                         Re-apply share/tinderbox-ng portage templates into
                         the existing baseline without a full rebootstrap.
  --selftest             Run `tinderbox-ng selftest` after install.
  --remote-prefix DIR    tinderbox-ng install dir on remote
                         (default: /usr/local/share/tinderbox-ng).
  --link DIR             Symlink for tinderbox-ng entry point
                         (default: /usr/local/sbin/tinderbox-ng).
  -h, --help             This message.

Environment (git mode defaults match ami-tinder.sh):
  TINDERBOX_NG_URL       default https://github.com/pvdabeel/tinderbox-ng.git
  TINDERBOX_NG_REF       default main
  PORTAGE_NG_URL         default https://github.com/pvdabeel/portage-ng.git
                         (cloned into the baseline, not the host)
  PORTAGE_NG_REF         default master

Also forwarded: TINDERBOX_CCACHE_MAX_SIZE, TINDERBOX_SESSIONS_TMPFS_SIZE,
STAGE3_*, PORTAGE_TREE_PIN, GENTOO_PROFILE, TINDERBOX_REBOOTSTRAP, ...

Examples:
  # VM install / update from GitHub (recommended):
  ./contrib/deploy-host.sh root@vm-linux.local
  ./contrib/deploy-host.sh --refresh-portage-ng root@vm-linux.local

  # First-time bootstrap:
  TINDERBOX_BOOTSTRAP_SELFTEST=1 \
    ./contrib/deploy-host.sh --bootstrap root@vm-linux.local
USAGE
}

# Defaults
DO_BOOTSTRAP=0
DO_REFRESH_PNG=0
DO_REFRESH_BASELINE_CONFIG=0
DO_SELFTEST=0
REMOTE_PREFIX=/usr/local/share/tinderbox-ng
REMOTE_LINK=/usr/local/sbin/tinderbox-ng
REMOTE_COMPARE_MATRIX_LINK=/usr/local/sbin/compare-matrix
TINDERBOX_NG_URL="${TINDERBOX_NG_URL:-https://github.com/pvdabeel/tinderbox-ng.git}"
TINDERBOX_NG_REF="${TINDERBOX_NG_REF:-main}"
PORTAGE_NG_GIT_URL="${PORTAGE_NG_URL:-https://github.com/pvdabeel/portage-ng.git}"
PORTAGE_NG_REF="${PORTAGE_NG_REF:-master}"
REMOTE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bootstrap)          DO_BOOTSTRAP=1; shift ;;
    --refresh-portage-ng) DO_REFRESH_PNG=1; shift ;;
    --refresh-baseline-config) DO_REFRESH_BASELINE_CONFIG=1; shift ;;
    --selftest)           DO_SELFTEST=1; shift ;;
    --remote-prefix)      REMOTE_PREFIX="$2"; shift 2 ;;
    --link)               REMOTE_LINK="$2"; shift 2 ;;
    -h|--help)            usage; exit 0 ;;
    --)                   shift; break ;;
    -*)                   echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [[ -z "$REMOTE" ]]; then
        REMOTE="$1"; shift
      else
        echo "unexpected positional arg: $1" >&2; usage >&2; exit 2
      fi
      ;;
  esac
done

[[ -n "$REMOTE" ]] || { usage >&2; echo >&2; echo "error: missing user@vm-host" >&2; exit 2; }

# portage-ng is always git-deployed straight from GitHub into the baseline
# (tinderbox-ng `bootstrap` / `refresh-portage-ng` do the clone/fetch inside
# the chroot). There is no on-host portage-ng checkout and no rsync; just
# forward the URL + ref the remote should track.
export PORTAGE_NG_URL="$PORTAGE_NG_GIT_URL"
export PORTAGE_NG_REF

collect_env() {
  local v
  local out=""
  for v in TINDERBOX_CCACHE_MAX_SIZE TINDERBOX_SESSIONS_TMPFS_SIZE \
           TINDERBOX_BOOTSTRAP_SELFTEST TINDERBOX_REBOOTSTRAP \
           TINDERBOX_SELFTEST_TARGET TINDERBOX_MIN_FREE_MIB \
           TINDERBOX_MDNS_HOSTS TINDERBOX_SKIP_DOCTOR \
           STAGE3_VARIANT STAGE3_ARCH STAGE3_BASE_URL \
           GENTOO_PROFILE GENTOO_LOCALE GENTOO_LOCALE_NAME \
           PORTAGE_TREE_URL PORTAGE_TREE_PIN \
           TINDERBOX_NG_URL TINDERBOX_NG_REF \
           PORTAGE_NG_URL PORTAGE_NG_REF; do
    if [[ -n "${!v:-}" ]]; then
      printf -v esc '%q' "${!v}"
      out+="$v=$esc "
    fi
  done
  printf '%s' "$out"
}

ENV_PREFIX="$(collect_env)"

remote() {
  ssh -o BatchMode=yes "$REMOTE" "$@"
}

remote_root() {
  if [[ "${REMOTE%@*}" == "root" ]] || [[ "$REMOTE" != *@* ]]; then
    remote "$@"
  else
    remote "sudo -E bash -c $(printf '%q' "$*")"
  fi
}

log() { printf '[deploy-host] %s\n' "$*" >&2; }

# Remote: clone or update a git repo (same logic as contrib/ami-tinder.sh).
remote_clone_repos() {
  local -a env_args=(
    "TINDERBOX_NG_URL=$(printf '%q' "$TINDERBOX_NG_URL")"
    "TINDERBOX_NG_REF=$(printf '%q' "$TINDERBOX_NG_REF")"
    "REMOTE_PREFIX=$(printf '%q' "$REMOTE_PREFIX")"
  )
  local ssh_base=(ssh -o BatchMode=yes "$REMOTE")
  local run_clone
  if [[ "${REMOTE%@*}" == "root" ]] || [[ "$REMOTE" != *@* ]]; then
    run_clone=("${ssh_base[@]}" env "${env_args[@]}" bash -s)
  else
    run_clone=("${ssh_base[@]}" sudo env "${env_args[@]}" bash -s)
  fi

  "${run_clone[@]}" <<'REMOTE_SCRIPT'
set -euo pipefail

log() { printf '[deploy-host] %s\n' "$*" >&2; }

_clone_repo() {
  local url="$1" dest="$2" ref="$3"
  log "git $url -> $dest @ $ref"
  install -d "$(dirname "$dest")"
  if [[ -d "$dest/.git" ]]; then
    git -C "$dest" fetch --depth 1 origin "$ref" \
      || git -C "$dest" fetch --depth 1 origin master \
      || git -C "$dest" fetch --depth 1 origin main
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

command -v git >/dev/null 2>&1 || { log "ERROR: git not found on remote"; exit 1; }

# Repo may be owned by a different uid after a prior install; avoid
# "dubious ownership" when root updates an existing checkout.
git config --global --add safe.directory "$REMOTE_PREFIX" 2>/dev/null || true

# Only the tinderbox-ng tool is installed on the host. portage-ng is
# git-deployed straight into the baseline by `tinderbox-ng bootstrap` /
# `refresh-portage-ng`, so there is no on-host portage-ng checkout here.
_clone_repo "$TINDERBOX_NG_URL" "$REMOTE_PREFIX" "$TINDERBOX_NG_REF"
chown -R root:root "$REMOTE_PREFIX"

log "tinderbox-ng @ $(git -C "$REMOTE_PREFIX" rev-parse --short HEAD 2>/dev/null || echo '?')"
REMOTE_SCRIPT
}

log "remote: $REMOTE"
log "prefix: $REMOTE_PREFIX  link: $REMOTE_LINK"
log "tinderbox-ng: git $TINDERBOX_NG_URL @ $TINDERBOX_NG_REF"
log "portage-ng: git $PORTAGE_NG_URL @ $PORTAGE_NG_REF (deployed into baseline)"

log "step 1/4: git clone/update tinderbox-ng on remote"
remote_clone_repos

log "step 2/4: symlink $REMOTE_LINK -> $REMOTE_PREFIX/bin/tinderbox-ng"
remote_root "install -d $(dirname $(printf '%q' "$REMOTE_LINK")) && \
             ln -sfn $(printf '%q' "$REMOTE_PREFIX/bin/tinderbox-ng") $(printf '%q' "$REMOTE_LINK") && \
             chmod +x $(printf '%q' "$REMOTE_PREFIX/bin/tinderbox-ng")"

log "step 2/4: symlink $REMOTE_COMPARE_MATRIX_LINK -> compare-matrix.sh"
remote_root "install -d $(dirname $(printf '%q' "$REMOTE_COMPARE_MATRIX_LINK")) && \
             ln -sfn $(printf '%q' "$REMOTE_PREFIX/libexec/tinderbox-ng/compare-matrix.sh") \
                     $(printf '%q' "$REMOTE_COMPARE_MATRIX_LINK") && \
             chmod +x $(printf '%q' "$REMOTE_PREFIX/libexec/tinderbox-ng/compare-matrix.sh")"

remote_root "if [[ -f $(printf '%q' "$REMOTE_PREFIX/share/man/man8/tinderbox-ng.8") ]]; then \
               install -d /usr/local/share/man/man8 && \
               ln -sfn $(printf '%q' "$REMOTE_PREFIX/share/man/man8/tinderbox-ng.8") \
                       /usr/local/share/man/man8/tinderbox-ng.8 && \
               command -v mandb >/dev/null 2>&1 && mandb -q /usr/local/share/man >/dev/null 2>&1 || true; \
             fi"

log "step 3/4: $REMOTE_LINK doctor"
if ! remote_root "$ENV_PREFIX $(printf '%q' "$REMOTE_LINK") doctor"; then
  echo "[deploy-host] doctor reported errors; fix them and re-run." >&2
  exit 1
fi

if (( DO_REFRESH_PNG )); then
  log "step 4/4: $REMOTE_LINK refresh-portage-ng"
  log "  (git fetch + reset --hard $PORTAGE_NG_URL into baseline /opt/portage-ng)"
  remote_root "$ENV_PREFIX $(printf '%q' "$REMOTE_LINK") refresh-portage-ng" || \
    echo "[deploy-host] refresh-portage-ng failed (baseline may not exist yet)" >&2
fi

if (( DO_REFRESH_BASELINE_CONFIG )); then
  log "step 4/4: $REMOTE_LINK refresh-baseline-config"
  remote_root "$ENV_PREFIX $(printf '%q' "$REMOTE_LINK") refresh-baseline-config" || \
    echo "[deploy-host] refresh-baseline-config failed (baseline may not exist yet)" >&2
fi

if (( DO_BOOTSTRAP )); then
  log "step 4/4: $REMOTE_LINK bootstrap (long-running; ~hours)"
  remote_root "$ENV_PREFIX $(printf '%q' "$REMOTE_LINK") bootstrap"
fi

if (( DO_SELFTEST )) || (( DO_BOOTSTRAP )) && [[ -n "${TINDERBOX_BOOTSTRAP_SELFTEST:-}" ]]; then
  :
elif (( DO_SELFTEST )); then
  log "step 4/4: $REMOTE_LINK selftest"
  remote_root "$ENV_PREFIX $(printf '%q' "$REMOTE_LINK") selftest"
fi

log "done."
