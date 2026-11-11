#!/usr/bin/env bash

set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

note "Codex home: $CODEX_HOME_DIR"
note "Config: $CODEX_HOME_DIR/config.toml"
if [[ -e "$CODEX_HOME_DIR/config.toml" ]]; then
    stat -c 'Config modified: %y' "$CODEX_HOME_DIR/config.toml"
fi

if ! "$CODEX_BIN" app-server daemon version 2>/dev/null; then
    note "Managed daemon probe: not running"
fi

note "Processes:"
if [[ -n "$(app_server_pids)" ]]; then
    show_app_servers
else
    note "  none"
fi
