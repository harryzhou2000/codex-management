#!/usr/bin/env bash

set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

DRY_RUN=false
FORCE=false
GRACE_SECONDS=8

usage() {
    cat <<'EOF'
Usage: stop.sh [--dry-run] [--force] [--grace-seconds N]

Gracefully stop and unregister the Antigravity remote-control daemon. The
default path asks the agy CLI to stop and unregister it, then terminates any
verified leftover `agy remote-control serve` process owned by this user.

--force skips the agy stop command, disables the systemd user unit, and
terminates verified processes. Session and configuration files are not deleted.
EOF
}

while (($#)); do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --force) FORCE=true; shift ;;
        --grace-seconds)
            [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || die "--grace-seconds requires a non-negative integer"
            GRACE_SECONDS="$2"
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

assert_not_inside_daemon

mapfile -t pids < <(daemon_pids)
if $DRY_RUN; then
    if $FORCE; then
        note "Would disable $AGY_UNIT and terminate these verified processes:"
    else
        note "Would run: $AGY_BIN remote-control stop"
        note "Would then terminate any verified leftovers:"
    fi
    if ((${#pids[@]})); then
        show_daemon
    else
        note "  none"
    fi
    exit 0
fi

if $FORCE; then
    if service_loaded; then
        systemctl --user disable --now "$AGY_UNIT" || warn "systemctl --user disable --now failed; continuing with process cleanup"
    fi
else
    if ((${#pids[@]})) || service_loaded; then
        if ! "$AGY_BIN" remote-control stop; then
            warn "agy remote-control stop failed; continuing with verified-process cleanup"
        fi
    else
        note "Remote-control daemon is already stopped"
    fi
fi

mapfile -t pids < <(daemon_pids)
if ((${#pids[@]})); then
    note "Sending TERM to remaining remote-control daemon processes: ${pids[*]}"
    for pid in "${pids[@]}"; do
        is_daemon_pid "$pid" && kill -TERM "$pid" 2>/dev/null || true
    done

    deadline=$((SECONDS + GRACE_SECONDS))
    while ((SECONDS < deadline)); do
        daemon_running || break
        sleep 0.2
    done
fi

mapfile -t pids < <(daemon_pids)
if ((${#pids[@]})); then
    note "Sending KILL to unresponsive remote-control daemon processes: ${pids[*]}" >&2
    for pid in "${pids[@]}"; do
        is_daemon_pid "$pid" && kill -KILL "$pid" 2>/dev/null || true
    done
fi

sleep 0.2
if service_active; then
    die "$AGY_UNIT is still active after process cleanup"
fi
if daemon_running; then
    mapfile -t pids < <(daemon_pids)
    die "remote-control daemon processes remain: ${pids[*]}"
fi

note "Remote-control daemon is stopped. Session and configuration files were not deleted."
