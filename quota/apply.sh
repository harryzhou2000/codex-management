#!/bin/sh
# Apply the quota login warning to /etc/profile.d on this host.
#
#   sudo sh apply.sh              install (re-runs are no-ops)
#   DESTDIR=/tmp/x sh apply.sh    test without root
#
# Uninstall:  sudo rm /etc/profile.d/quota-warning.sh
set -e

SRC_DIR=$(cd "$(dirname "$0")" && pwd)
SRC="$SRC_DIR/quota-warning.sh"
DEST="${DESTDIR:-}/etc/profile.d/quota-warning.sh"

[ -f "$SRC" ] || { echo "ERROR: $SRC not found" >&2; exit 1; }

if [ -z "${DESTDIR:-}" ] && [ "$(id -u)" != 0 ]; then
  echo "ERROR: run as root:  sudo sh $0" >&2
  exit 1
fi

mkdir -p "$(dirname "$DEST")"
if [ -f "$DEST" ] && cmp -s "$SRC" "$DEST"; then
  echo "already applied (identical): $DEST"
  exit 0
fi
install -m 644 "$SRC" "$DEST"
echo "installed: $DEST"
echo 'New interactive logins will print a "Quota status:" block.'
