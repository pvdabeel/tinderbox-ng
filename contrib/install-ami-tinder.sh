#!/bin/bash
# contrib/install-ami-tinder.sh — add the `tinder` login-shell command on a Gentoo AWS VM.
#
# Installs a profile function (like update/fullupdate) that curl's
# contrib/ami-tinder.sh from GitHub and pipes it to bash.  Nothing from
# tinderbox-ng is baked onto disk except this one-liner — driver updates
# ship via git main, no AMI rebake required.
#
# Usage:
#   ./contrib/install-ami-tinder.sh root@ec2-....compute.amazonaws.com

set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: install-ami-tinder.sh user@host

Adds tinder() to /root/.bash_profile and /root/.zshrc (idempotent).
Removes legacy /root/bin/tinder if present.
USAGE
}

REMOTE="${1:-}"
[[ -n "$REMOTE" ]] || { usage >&2; exit 2; }

SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile="${HOME}/.ssh/amazon-vms")

read -r -d '' PROFILE_BLOCK <<'BLOCK' || true
# --- tinderbox-ng tinder (begin) ---
tinder() {
  local ref="${TINDERBOX_NG_REF:-main}"
  curl -fsSL "https://raw.githubusercontent.com/pvdabeel/tinderbox-ng/${ref}/contrib/ami-tinder.sh" | bash
}
# --- tinderbox-ng tinder (end) ---
BLOCK

# Base64 the block so we can pass it safely through ssh.
BLOCK_B64="$(printf '%s' "$PROFILE_BLOCK" | base64 | tr -d '\n')"

ssh "${SSH_OPTS[@]}" "$REMOTE" "BLOCK_B64='${BLOCK_B64}' bash -s" <<'REMOTE'
set -euo pipefail

block="$(printf '%s' "$BLOCK_B64" | base64 -d)"

_install_block() {
  local rcfile="$1"
  [[ -f "$rcfile" ]] || install -m 0644 /dev/null "$rcfile"
  if grep -q 'tinderbox-ng tinder (begin)' "$rcfile" 2>/dev/null; then
    sed -i '/# --- tinderbox-ng tinder (begin) ---/,/# --- tinderbox-ng tinder (end) ---/d' "$rcfile"
  fi
  # Drop legacy baked-script alias if present.
  sed -i '\|^alias tinder=|d' "$rcfile" 2>/dev/null || true
  printf '\n%s\n' "$block" >> "$rcfile"
  echo "updated $rcfile"
}

rm -f /root/bin/tinder
_install_block /root/.bash_profile
[[ -f /root/.zshrc ]] && _install_block /root/.zshrc || true

echo "verify: $(type tinder 2>/dev/null || true)"
REMOTE

echo "[install-ami-tinder] done: ssh $REMOTE bash -icl tinder"
