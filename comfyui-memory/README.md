# comfyui-memory

Periodic user service that asks the local ComfyUI server to unload cached
models and node outputs when nothing is queued.

The behavior follows ~/ssd1/projects/flux2/scripts/free_comfy_memory.py. The
copy here uses only the Python standard library, so the timer does not depend
on the flux2 checkout.

## Install

    cd ~/projects/management/comfyui-memory
    ./install.sh

Defaults are http://127.0.0.1:8188 and a 30min interval. The first run happens
one interval after the timer starts.

    ./install.sh --interval 45min
    ./install.sh --base http://127.0.0.1:8189
    ./install.sh --uninstall

The units are written to $XDG_CONFIG_HOME/systemd/user (or
~/.config/systemd/user when XDG_CONFIG_HOME is unset). Rerunning install.sh
updates the units and restarts the timer.

## Behavior

Each run:

1. Reads /system_stats for logging only.
2. Reads /queue.
3. If queue_running or queue_pending has any item, logs the counts and exits
   successfully without calling /free.
4. Otherwise POSTs /free with unload_models and free_memory set to true.

A stopped or unreachable ComfyUI is a successful skip, so a missing server does
not make the service fail. A malformed queue response is treated as busy; the
script never frees when it cannot prove the queue is empty.

ComfyUI has no atomic "free only if idle" endpoint. A prompt submitted between
the queue check and /free can still be affected, although the two calls are
adjacent. The queue check is the last read before the free request.

## Check it

    ./free_comfy_memory.py --dry-run
    systemctl --user list-timers comfyui-free-memory.timer
    systemctl --user start comfyui-free-memory.service
    journalctl --user -u comfyui-free-memory.service -n 50 --no-pager
    systemctl --user status comfyui-free-memory.timer

--dry-run performs the queue check and does not call /free. A manual
systemctl --user start ...service does free immediately when the queue is idle.

## Remove

    ./install.sh --uninstall

This stops and removes the two installed units; the files in this directory stay.
