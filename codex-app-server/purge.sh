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
Usage: purge.sh [--dry-run] [--force] [--grace-seconds N]

Gracefully stop the managed daemon, then terminate verified user-owned Codex
app-server daemons, updater loops, standalone servers, and SSH proxy bridges.
Session/configuration files are not deleted.

--force skips graceful shutdown and immediately sends KILL to every verified
Codex app-server process owned by the current user.
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

assert_not_inside_app_server

if $DRY_RUN; then
    if $FORCE; then
        note "Would immediately send KILL to these verified processes:"
    else
        note "Would run: $CODEX_BIN app-server daemon stop"
        note "Would terminate any remaining verified processes with TERM, then KILL after ${GRACE_SECONDS}s:"
    fi
    show_app_servers
    exit 0
fi

if $FORCE; then
    mapfile -t pids < <(app_server_pids)
    if ((${#pids[@]})); then
        note "Forcefully sending KILL to verified app-server processes: ${pids[*]}" >&2
        for pid in "${pids[@]}"; do
            app_server_role "$pid" >/dev/null 2>&1 && kill -KILL "$pid" 2>/dev/null || true
        done
    fi
    sleep 0.2
    mapfile -t pids < <(app_server_pids)
    ((${#pids[@]} == 0)) || die "app-server processes remain: ${pids[*]}"
    note "All Codex app-server processes for user $(id -un) were forcefully stopped. No session or config files were deleted."
    exit 0
fi

note "Stopping managed daemon gracefully..."
if ! "$CODEX_BIN" app-server daemon stop; then
    note "warning: managed daemon stop failed; continuing with verified-process cleanup" >&2
fi

mapfile -t pids < <(app_server_pids)
if ((${#pids[@]})); then
    note "Sending TERM to remaining app-server processes: ${pids[*]}"
    for pid in "${pids[@]}"; do
        app_server_role "$pid" >/dev/null 2>&1 && kill -TERM "$pid" 2>/dev/null || true
    done

    deadline=$((SECONDS + GRACE_SECONDS))
    while ((SECONDS < deadline)); do
        mapfile -t pids < <(app_server_pids)
        ((${#pids[@]} == 0)) && break
        sleep 0.2
    done
fi

mapfile -t pids < <(app_server_pids)
if ((${#pids[@]})); then
    note "Sending KILL to unresponsive verified processes: ${pids[*]}" >&2
    for pid in "${pids[@]}"; do
        app_server_role "$pid" >/dev/null 2>&1 && kill -KILL "$pid" 2>/dev/null || true
    done
fi

sleep 0.2
mapfile -t pids < <(app_server_pids)
((${#pids[@]} == 0)) || die "app-server processes remain: ${pids[*]}"
note "All Codex app-server processes for user $(id -un) are stopped. No session or config files were deleted."
