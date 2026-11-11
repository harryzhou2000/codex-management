#!/usr/bin/env bash

set -euo pipefail

CODEX_BIN="${CODEX_BIN:-codex}"
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
PROXY_ENV_FILE="${PROXY_ENV_FILE:-$HOME/.setproxy.sh}"

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

note() {
    printf '%s\n' "$*"
}

pid_cmdline() {
    local pid="$1"
    tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true
}

is_codex_executable() {
    local pid="$1" exe
    exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
    [[ "${exe##*/}" == codex ]]
}

app_server_role() {
    local pid="$1" cmd
    is_codex_executable "$pid" || return 1
    cmd=" $(pid_cmdline "$pid") "

    case "$cmd" in
        *" app-server daemon pid-update-loop "*) printf '%s\n' updater ;;
        *" app-server proxy "*) printf '%s\n' ssh-proxy ;;
        *" app-server "*" --managed-daemon "*) printf '%s\n' managed-daemon ;;
        *" app-server "*) printf '%s\n' standalone ;;
        *) return 1 ;;
    esac
}

app_server_pids() {
    local pid
    while IFS= read -r pid; do
        [[ -n "$pid" ]] || continue
        app_server_role "$pid" >/dev/null 2>&1 || continue
        printf '%s\n' "$pid"
    done < <(pgrep -u "$(id -u)" -x codex 2>/dev/null || true)
}

show_app_servers() {
    local pid role cmd proxy_names
    while IFS= read -r pid; do
        [[ -n "$pid" ]] || continue
        role="$(app_server_role "$pid")" || continue
        cmd="$(pid_cmdline "$pid")"
        proxy_names="$(
            tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null |
                cut -d= -f1 |
                grep -E '^(HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY|http_proxy|https_proxy|all_proxy|no_proxy)$' |
                paste -sd, - || true
        )"
        [[ -n "$proxy_names" ]] || proxy_names=none
        printf 'pid=%s role=%s proxy_env=%s\n  %s\n' "$pid" "$role" "$proxy_names" "$cmd"
    done < <(app_server_pids)
}

assert_not_inside_app_server() {
    local pid="$$" parent role
    while [[ "$pid" -gt 1 ]]; do
        parent="$(awk '/^PPid:/{print $2}' "/proc/$pid/status" 2>/dev/null || true)"
        [[ -n "$parent" && "$parent" -gt 0 ]] || break
        if role="$(app_server_role "$parent" 2>/dev/null)"; then
            die "refusing to replace app-server from inside $role pid $parent; run this script from an independent terminal or SSH shell"
        fi
        pid="$parent"
    done
}

set_network_mode() {
    local mode="$1"
    case "$mode" in
        proxy)
            [[ -r "$PROXY_ENV_FILE" ]] || die "proxy environment file is not readable: $PROXY_ENV_FILE"
            # shellcheck disable=SC1090
            source "$PROXY_ENV_FILE"
            export HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY
            export http_proxy https_proxy no_proxy
            ;;
        no-proxy)
            unset HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY
            unset http_proxy https_proxy all_proxy no_proxy
            ;;
        *) die "unknown network mode: $mode" ;;
    esac
}

managed_daemon_pid() {
    local pid role
    while IFS= read -r pid; do
        role="$(app_server_role "$pid" 2>/dev/null || true)"
        if [[ "$role" == managed-daemon ]]; then
            printf '%s\n' "$pid"
            return 0
        fi
    done < <(app_server_pids)
    return 1
}

verify_managed_daemon_network_mode() {
    local mode="$1" pid names
    pid="$(managed_daemon_pid)" || die "managed daemon did not appear"
    names="$(
        tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null |
            cut -d= -f1 |
            grep -E '^(HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|http_proxy|https_proxy|all_proxy)$' |
            paste -sd, - || true
    )"
    case "$mode" in
        proxy)
            [[ -n "$names" ]] || die "managed daemon pid $pid started without proxy variables"
            ;;
        no-proxy)
            [[ -z "$names" ]] || die "managed daemon pid $pid unexpectedly inherited proxy variables: $names"
            ;;
    esac
    note "managed daemon pid=$pid verified network_mode=$mode proxy_env=${names:-none}"
}
