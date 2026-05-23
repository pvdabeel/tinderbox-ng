#!/bin/bash
# contrib/install-ami-tinder.sh — bake /root/bin/tinder onto a Gentoo AWS AMI.
#
# Only the tinder driver is installed on the AMI.  The script clones
# tinderbox-ng + portage-ng from GitHub when invoked.
#
# Usage:
#   ./contrib/install-ami-tinder.sh root@ec2-....compute.amazonaws.com

set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: install-ami-tinder.sh user@host

Installs contrib/ami-tinder.sh as /root/bin/tinder and adds a bash login alias.
USAGE
}

REMOTE="${1:-}"
[[ -n "$REMOTE" ]] || { usage >&2; exit 2; }

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/ami-tinder.sh"
[[ -f "$SRC" ]] || { echo "missing $SRC" >&2; exit 2; }

SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile="${HOME}/.ssh/amazon-vms")

ssh "${SSH_OPTS[@]}" "$REMOTE" 'install -d /root/bin'
scp "${SSH_OPTS[@]}" "$SRC" "$REMOTE:/root/bin/tinder"
ssh "${SSH_OPTS[@]}" "$REMOTE" 'chmod 755 /root/bin/tinder
grep -qE "^alias tinder=" /root/.bash_profile 2>/dev/null || \
  printf "%s\n" "alias tinder=\"/root/bin/tinder\"" >> /root/.bash_profile
if [[ -f /root/.zshrc ]]; then
  grep -qE "^alias tinder=" /root/.zshrc 2>/dev/null || \
    printf "%s\n" "alias tinder=\"/root/bin/tinder\"" >> /root/.zshrc
fi
echo "installed: /root/bin/tinder"
/root/bin/tinder --help | head -5'

echo "[install-ami-tinder] done: ssh $REMOTE bash -icl tinder"
