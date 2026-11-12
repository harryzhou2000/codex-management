#!/usr/bin/env bash

set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

note "agy binary: $AGY_BIN"
note "agy version: $("$AGY_BIN" --version 2>/dev/null | head -n 1 || true)"
note "Remote-control unit: $AGY_UNIT"

load_state="$(systemctl --user show -p LoadState --value "$AGY_UNIT" 2>/dev/null || true)"
active_state="$(systemctl --user show -p ActiveState --value "$AGY_UNIT" 2>/dev/null || true)"
unit_state="$(systemctl --user show -p UnitFileState --value "$AGY_UNIT" 2>/dev/null || true)"
fragment="$(systemctl --user show -p FragmentPath --value "$AGY_UNIT" 2>/dev/null || true)"
note "Systemd: load=${load_state:-unknown} active=${active_state:-unknown} unit_file=${unit_state:-unknown}"
[[ -n "$fragment" ]] && note "Unit file: $fragment"

network_mode=inherited
if [[ -f "$NETWORK_DROPIN_FILE" ]]; then
    if grep -q '^EnvironmentFile=' "$NETWORK_DROPIN_FILE"; then
        network_mode=proxy
    elif grep -q '^UnsetEnvironment=' "$NETWORK_DROPIN_FILE"; then
        network_mode=no-proxy
    fi
fi
note "Configured network mode: $network_mode"
note "Network drop-in: $NETWORK_DROPIN_FILE"
if [[ -e "$NETWORK_ENV_FILE" ]]; then
    mode_bits="$(stat -c '%a %U:%G' "$NETWORK_ENV_FILE" 2>/dev/null || true)"
    note "Proxy environment file: $NETWORK_ENV_FILE ($mode_bits)"
fi

note "Remote-control status:"
"$AGY_BIN" remote-control status 2>&1 || note "  not active"

note "Processes:"
if daemon_running; then
    show_daemon
else
    note "  none"
fi

note "Logs: journalctl --user -u $AGY_UNIT -n 50 --no-pager"
