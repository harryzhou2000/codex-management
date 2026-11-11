#!/usr/bin/env bash

set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

NETWORK_MODE=""
PURGE=false
FORCE_PURGE=false
DRY_RUN=false
REMOTE_CONTROL=preserve

usage() {
    cat <<'EOF'
Usage: start.sh (--proxy | --no-proxy) [--purge | --force-purge] [--dry-run]
                [--enable-remote-control | --disable-remote-control]

Start the managed app-server using the current $CODEX_HOME/config.toml.
--proxy sources $PROXY_ENV_FILE (default: ~/.setproxy.sh) before launch.
--no-proxy removes common proxy variables before launch.
--purge first stops all verified user-owned Codex app-server processes.
--force-purge immediately kills those processes instead of stopping gracefully.
Remote-control state is preserved unless explicitly changed.
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
        --purge) PURGE=true; shift ;;
        --force-purge) PURGE=true; FORCE_PURGE=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        --enable-remote-control)
            [[ "$REMOTE_CONTROL" != disable ]] || die "remote-control options conflict"
            REMOTE_CONTROL=enable
            shift
            ;;
        --disable-remote-control)
            [[ "$REMOTE_CONTROL" != enable ]] || die "remote-control options conflict"
            REMOTE_CONTROL=disable
            shift
            ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[[ -n "$NETWORK_MODE" ]] || die "choose exactly one of --proxy or --no-proxy"
assert_not_inside_app_server

if $DRY_RUN; then
    if $PURGE; then
        purge_args=(--dry-run)
        $FORCE_PURGE && purge_args+=(--force)
        "$SCRIPT_DIR/purge.sh" "${purge_args[@]}"
    fi
    note "Would set network mode: $NETWORK_MODE"
    [[ "$REMOTE_CONTROL" == preserve ]] || note "Would ${REMOTE_CONTROL} remote control"
    note "Would run: $CODEX_BIN app-server daemon start"
    exit 0
fi

if $PURGE; then
    purge_args=()
    $FORCE_PURGE && purge_args+=(--force)
    "$SCRIPT_DIR/purge.sh" "${purge_args[@]}"
elif [[ -n "$(app_server_pids)" ]]; then
    die "app-server processes already exist; use --purge to replace them"
fi

set_network_mode "$NETWORK_MODE"

case "$REMOTE_CONTROL" in
    enable) "$CODEX_BIN" app-server daemon enable-remote-control ;;
    disable) "$CODEX_BIN" app-server daemon disable-remote-control ;;
    preserve) ;;
esac

note "Starting managed daemon with config $CODEX_HOME_DIR/config.toml and network_mode=$NETWORK_MODE..."
"$CODEX_BIN" app-server daemon start
"$CODEX_BIN" app-server daemon version
verify_managed_daemon_network_mode "$NETWORK_MODE"
