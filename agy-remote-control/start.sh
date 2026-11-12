#!/usr/bin/env bash

set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

NETWORK_MODE=""
REPLACE=false
FORCE_REPLACE=false
DRY_RUN=false
INSTANCE_NAME=""
SESSION=false

usage() {
    cat <<'EOF'
Usage: start.sh (--proxy | --no-proxy) [--replace | --force-replace]
                [--name NAME] [--session] [--dry-run]

Register and start the Antigravity remote-control daemon.

--proxy sources $PROXY_ENV_FILE (default: ~/.setproxy.sh), stores the common
proxy variables for the systemd user service, and launches agy with the same
environment. Values are kept in a mode-0600 file and are never printed.
--no-proxy removes the common proxy variables from the service environment.
--replace gracefully stops and unregisters an existing daemon first. The
current instance name is preserved unless --name is given.
--force-replace uses the forceful stop path if graceful unregistration hangs.
--session registers the new daemon for the current login session only.
EOF
}

while (($#)); do
    case "$1" in
        --proxy)
            [[ -z "$NETWORK_MODE" || "$NETWORK_MODE" == proxy ]] || die "--proxy conflicts with --no-proxy"
            NETWORK_MODE=proxy
            shift
            ;;
        --no-proxy)
            [[ -z "$NETWORK_MODE" || "$NETWORK_MODE" == no-proxy ]] || die "--no-proxy conflicts with --proxy"
            NETWORK_MODE=no-proxy
            shift
            ;;
        --replace) REPLACE=true; shift ;;
        --force-replace) REPLACE=true; FORCE_REPLACE=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        --name)
            [[ $# -ge 2 && -n "$2" ]] || die "--name requires a non-empty value"
            INSTANCE_NAME="$2"
            shift 2
            ;;
        --session) SESSION=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[[ -n "$NETWORK_MODE" ]] || die "choose exactly one of --proxy or --no-proxy"
assert_not_inside_daemon

PRESERVED_NAME=""
if $REPLACE && [[ -z "$INSTANCE_NAME" ]] && daemon_present; then
    PRESERVED_NAME="$(current_instance_name)"
fi
NAME_TO_USE="${INSTANCE_NAME:-$PRESERVED_NAME}"

start_args=(remote-control start)
[[ -n "$NAME_TO_USE" ]] && start_args+=(--name "$NAME_TO_USE")
$SESSION && start_args+=(--session)

if $DRY_RUN; then
    if $REPLACE; then
        purge_args=(--dry-run)
        $FORCE_REPLACE && purge_args+=(--force)
        "$SCRIPT_DIR/stop.sh" "${purge_args[@]}"
    elif daemon_present; then
        die "remote-control daemon is already running; use --replace to replace it"
    fi
    note "Would set network_mode=$NETWORK_MODE"
    if [[ "$NETWORK_MODE" == proxy ]]; then
        note "Would write proxy environment to $NETWORK_ENV_FILE"
    else
        note "Would unset proxy variables in the service environment"
    fi
    note "Would write systemd drop-in $NETWORK_DROPIN_FILE"
    note "Would run: $AGY_BIN ${start_args[*]}"
    exit 0
fi

if $REPLACE; then
    stop_args=()
    $FORCE_REPLACE && stop_args+=(--force)
    "$SCRIPT_DIR/stop.sh" "${stop_args[@]}"
elif daemon_present; then
    die "remote-control daemon is already running; use --replace to replace it"
fi

set_network_mode "$NETWORK_MODE"
write_network_mode "$NETWORK_MODE"

note "Starting remote-control daemon with network_mode=$NETWORK_MODE..."
if ! "$AGY_BIN" "${start_args[@]}"; then
    warn "agy remote-control start failed"
    systemctl --user status "$AGY_UNIT" --no-pager -l 2>&1 | tail -n 40 || true
    journalctl --user -u "$AGY_UNIT" -n 40 --no-pager 2>&1 || true
    exit 1
fi

if ! wait_for_daemon 25 >/dev/null; then
    warn "remote-control daemon did not become active"
    systemctl --user status "$AGY_UNIT" --no-pager -l 2>&1 | tail -n 40 || true
    journalctl --user -u "$AGY_UNIT" -n 40 --no-pager 2>&1 || true
    exit 1
fi

if ! daemon_network_matches "$NETWORK_MODE"; then
    warn "daemon started without the requested network environment; reloading the unit and restarting once"
    write_network_mode "$NETWORK_MODE"
    systemctl --user restart "$AGY_UNIT" || die "systemctl --user restart $AGY_UNIT failed"
    wait_for_daemon 25 >/dev/null || die "remote-control daemon did not become active after restart"
fi

verify_daemon_network_mode "$NETWORK_MODE"
"$AGY_BIN" remote-control status 2>&1 || true
note "Remote-control daemon is registered and running."
