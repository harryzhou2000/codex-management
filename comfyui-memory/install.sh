#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
interval=30min
base=http://127.0.0.1:8188
action=install

usage() {
    cat <<'EOF'
Usage: ./install.sh [--interval SPAN] [--base URL] [--uninstall]

Install or update the comfyui-free-memory user service and timer.

  --interval SPAN  systemd time span between runs (default: 30min)
  --base URL       ComfyUI base URL (default: http://127.0.0.1:8188)
  --uninstall      remove the installed units and stop the timer
EOF
}

while (($# > 0)); do
    case "$1" in
        --interval)
            (($# >= 2)) || { usage >&2; exit 2; }
            interval=$2
            shift 2
            ;;
        --base)
            (($# >= 2)) || { usage >&2; exit 2; }
            base=$2
            shift 2
            ;;
        --uninstall)
            action=uninstall
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

config_home=${XDG_CONFIG_HOME:-$HOME/.config}
unit_dir=$config_home/systemd/user
service_unit=$unit_dir/comfyui-free-memory.service
timer_unit=$unit_dir/comfyui-free-memory.timer

if ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "the user systemd bus is unavailable" >&2
    exit 1
fi

if [[ $action == uninstall ]]; then
    systemctl --user disable --now comfyui-free-memory.timer 2>/dev/null || true
    systemctl --user stop comfyui-free-memory.service 2>/dev/null || true
    rm -f -- "$service_unit" "$timer_unit"
    systemctl --user daemon-reload
    echo "removed comfyui-free-memory.service and .timer from $unit_dir"
    exit 0
fi

if [[ ! -f $script_dir/free_comfy_memory.py ]]; then
    echo "missing script: $script_dir/free_comfy_memory.py" >&2
    exit 1
fi
if [[ ! -x /usr/bin/python3 ]]; then
    echo "missing /usr/bin/python3" >&2
    exit 1
fi
if [[ ! $interval =~ ^[1-9][0-9]*(us|ms|s|min|h|d)$ ]]; then
    echo "invalid interval '$interval'; use a positive systemd span such as 30min" >&2
    exit 2
fi
if [[ ! $base =~ ^https?://[^[:space:]]+$ ]]; then
    echo "invalid base URL '$base'" >&2
    exit 2
fi
base=${base%/}

mkdir -p -- "$unit_dir"
umask 022
cat >"$service_unit" <<EOF
[Unit]
Description=Free cached ComfyUI memory when the queue is idle
Documentation=file://$script_dir/README.md

[Service]
Type=oneshot
SyslogIdentifier=comfyui-free-memory
Environment=COMFYUI_BASE=$base
ExecStart=/usr/bin/python3 $script_dir/free_comfy_memory.py
TimeoutStartSec=120
Nice=10
EOF

cat >"$timer_unit" <<EOF
[Unit]
Description=Periodically free idle ComfyUI memory ($interval)

[Timer]
OnActiveSec=$interval
OnUnitActiveSec=$interval
AccuracySec=1min
Unit=comfyui-free-memory.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable comfyui-free-memory.timer >/dev/null
systemctl --user restart comfyui-free-memory.timer

systemctl --user --no-pager list-timers comfyui-free-memory.timer
echo "installed $service_unit and $timer_unit"
