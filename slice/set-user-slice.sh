#!/bin/bash
# set-user-slice.sh -- CPU limits for users, via systemd user slices.
#
# For each user this updates /etc/systemd/system/user-<uid>.slice:
#
#     [Slice]
#     CPUQuota=<CORES * 100>%
#
# (keeping any other settings already in the file), then runs
# "systemctl daemon-reload" so the quota also applies to running sessions.
#
# Usage (run as root):
#   sudo set-user-slice.sh 32               # 32 cores  -> CPUQuota=3200%  (default users)
#   sudo set-user-slice.sh 128              # 128 cores -> CPUQuota=12800%
#   sudo set-user-slice.sh 32 azaaz mrz     # explicit user list instead of the default
#   sudo set-user-slice.sh --clear          # remove the quota files (unlimited again)
#
# Read-only helpers (no root needed):
#   set-user-slice.sh --status              # show the current values for the users
#   set-user-slice.sh --dry-run 32          # print what would change
#
# Default users: azaaz zsh huangwz mrz ljc
set -euo pipefail

DEFAULT_USERS=(azaaz zsh huangwz mrz ljc)
UNIT_DIR="${DESTDIR:-}/etc/systemd/system"

usage() {
  awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
}

mode=set
dry=0
rest=()

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    -n|--dry-run) dry=1 ;;
    -c|--clear) mode=clear ;;
    -s|--status) mode=status ;;
    -*) echo "error: unknown option: $arg" >&2; usage >&2; exit 2 ;;
    *) rest+=("$arg") ;;
  esac
done

cores=""
users=()
if [ "$mode" = set ]; then
  if [ "${#rest[@]}" -gt 0 ]; then
    cores="${rest[0]}"
    users=("${rest[@]:1}")
  elif [ "$dry" = 0 ] && [ -t 0 ]; then
    read -r -p "CPU limit in cores (32 = 3200%, 128 = 12800%): " cores
  else
    echo "error: CORES is required" >&2
    usage >&2
    exit 2
  fi
  case "$cores" in
    ''|*[!0-9]*)
      echo "error: CORES must be a positive integer (e.g. 32 or 128)" >&2
      exit 2
      ;;
  esac
  if [ "$cores" -le 0 ]; then
    echo "error: CORES must be greater than 0" >&2
    exit 2
  fi
else
  users=("${rest[@]}")
fi

if [ "${#users[@]}" -eq 0 ]; then
  users=("${DEFAULT_USERS[@]}")
fi

if [ "$mode" != status ] && [ "$dry" = 0 ] && [ -z "${DESTDIR:-}" ] && [ "$(id -u)" -ne 0 ]; then
  echo "error: writing slice files requires root:  sudo $0 ..." >&2
  exit 1
fi

names=()
uids=()
missing=()
for u in "${users[@]}"; do
  if uid="$(id -u "$u" 2>/dev/null)"; then
    names+=("$u")
    uids+=("$uid")
  else
    missing+=("$u")
  fi
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "error: no such user(s): ${missing[*]}" >&2
  exit 1
fi

show_status() {
  local i uid file value
  for i in "${!uids[@]}"; do
    uid="${uids[$i]}"
    file="$UNIT_DIR/user-$uid.slice"
    echo "user ${names[$i]} (uid $uid)"
    if [ -f "$file" ]; then
      echo "  file: $file"
      sed 's/^/    /' "$file"
    else
      echo "  file: $file (not present -- no quota)"
    fi
    if [ -z "${DESTDIR:-}" ] && command -v systemctl >/dev/null 2>&1; then
      value="$(systemctl show "user-$uid.slice" -p CPUQuotaPerSecUSec --value 2>/dev/null || echo unknown)"
      echo "  systemd value: $value"
    fi
  done
}

if [ "$mode" = status ]; then
  show_status
  exit 0
fi

pct=""
if [ "$mode" = set ]; then
  pct=$(( cores * 100 ))
fi

changed=0
for i in "${!uids[@]}"; do
  name="${names[$i]}"
  uid="${uids[$i]}"
  file="$UNIT_DIR/user-$uid.slice"

  if [ "$mode" = clear ]; then
    if [ -f "$file" ]; then
      if [ "$dry" = 1 ]; then
        echo "would remove: $file"
      else
        echo "removing: $file  (had: $(tr '\n' ' ' <"$file"))"
        rm -f "$file"
        changed=1
      fi
    else
      echo "nothing to remove for $name (uid $uid)"
    fi
    continue
  fi

  new_line="CPUQuota=${pct}%"
  if [ "$dry" = 1 ]; then
    echo "would set: $file -> $new_line"
    continue
  fi

  if [ ! -e "$file" ]; then
    mkdir -p "$(dirname "$file")"
    printf '[Slice]\n%s\n' "$new_line" >"$file"
  elif grep -qE '^[[:space:]]*CPUQuota=' "$file"; then
    sed -E -i "s|^[[:space:]]*CPUQuota=.*|${new_line}|" "$file"
  elif grep -qE '^[[:space:]]*\[Slice\][[:space:]]*$' "$file"; then
    awk -v line="$new_line" 'BEGIN { done=0 } { print } !done && /^[[:space:]]*\[Slice\][[:space:]]*$/ { print line; done=1 }' "$file" >"$file.tmp.$$"
    mv "$file.tmp.$$" "$file"
  else
    echo "warning: $file exists but has no [Slice] section; leaving it alone" >&2
    continue
  fi
  echo "set: $file -> $new_line"
  changed=1
done

if [ "$dry" = 0 ] && [ "$changed" = 1 ] && [ -z "${DESTDIR:-}" ]; then
  systemctl daemon-reload
  echo
  echo "systemd reloaded -- the quota applies to running sessions and new logins."
  echo
  show_status
fi
