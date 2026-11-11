#!/usr/bin/env python3
"""Free ComfyUI's cached models and node outputs only while its queue is idle.

This is the scheduled equivalent of the web UI's "Free model and node cache"
button.  It checks GET /queue first and skips POST /free when any prompt is
running or pending.  A stopped ComfyUI is a normal skip, not a failure.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request

GIB = 1024 ** 3
DEFAULT_BASE = "http://127.0.0.1:8188"
NEAR_EMPTY_VRAM = 2 * 1024 ** 3
SETTLE_STEP = 0.5


def log(message: str) -> None:
    print(message, flush=True)


def make_client() -> urllib.request.OpenerDirector:
    """Return an opener that ignores proxy settings; the target is local."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch_json(client: urllib.request.OpenerDirector, url: str, timeout: float):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with client.open(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body) if body.strip() else {}


def free_models(client: urllib.request.OpenerDirector, url: str, timeout: float) -> None:
    payload = json.dumps({"unload_models": True, "free_memory": True}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with client.open(request, timeout=timeout) as response:
        response.read()


def snapshot(client: urllib.request.OpenerDirector, base: str, timeout: float):
    """Read audit numbers; stats are optional and never decide whether to free."""
    try:
        stats = fetch_json(client, base + "/system_stats", timeout)
        system = stats.get("system") or {}
        devices = stats.get("devices") or []
        device = devices[0] if devices else {}
        return {
            "version": system.get("comfyui_version") or "?",
            "name": device.get("name") or "GPU",
            "vram_total": int(device.get("vram_total") or 0),
            "vram_free": int(device.get("vram_free") or 0),
            "ram_total": int(system.get("ram_total") or 0),
            "ram_free": int(system.get("ram_free") or 0),
        }
    except Exception:
        return None


def vram_used(stats) -> int:
    return max(0, stats["vram_total"] - stats["vram_free"])


def wait_for_release(client, base, timeout, before, settle_timeout):
    if not before or before["vram_total"] == 0:
        return before
    if vram_used(before) <= NEAR_EMPTY_VRAM:
        return snapshot(client, base, timeout) or before

    deadline = time.monotonic() + settle_timeout
    after = before
    while time.monotonic() < deadline:
        time.sleep(SETTLE_STEP)
        current = snapshot(client, base, timeout)
        if current is None:
            continue
        after = current
        if vram_used(current) <= NEAR_EMPTY_VRAM:
            break
    return after


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--base",
        default=os.environ.get("COMFYUI_BASE", DEFAULT_BASE),
        help=f"ComfyUI base URL (default: $COMFYUI_BASE or {DEFAULT_BASE})",
    )
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="HTTP timeout in seconds (default: 30)")
    parser.add_argument("--settle-timeout", type=float, default=20.0,
                        help="seconds to wait for VRAM to come back (default: 20)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the queue state without calling /free")
    args = parser.parse_args()
    if args.timeout <= 0 or args.settle_timeout < 0:
        parser.error("timeouts must be positive")

    base = args.base.rstrip("/")
    client = make_client()

    # Sample before the queue check so the idle check is the last call before /free.
    before = snapshot(client, base, args.timeout)

    try:
        queue = fetch_json(client, base + "/queue", args.timeout)
    except urllib.error.HTTPError as exc:
        log(f"queue check failed: HTTP {exc.code}; not freeing")
        return 1
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        log(f"ComfyUI unavailable at {base}; skipping ({exc})")
        return 0
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log(f"cannot parse queue response; not freeing ({exc})")
        return 1

    running = queue.get("queue_running") if isinstance(queue, dict) else None
    pending = queue.get("queue_pending") if isinstance(queue, dict) else None
    if not isinstance(running, list) or not isinstance(pending, list):
        log("queue response lacks queue_running/queue_pending; not freeing")
        return 1

    if running or pending:
        log(f"queue busy ({len(running)} running, {len(pending)} pending); skipping free")
        return 0

    if args.dry_run:
        log(f"queue idle at {base}; dry run, nothing freed")
        return 0

    if before:
        log(f"ComfyUI {before['version']} at {base}; queue idle")
    else:
        log(f"queue idle at {base}")

    try:
        free_models(client, base + "/free", args.timeout)
    except urllib.error.HTTPError as exc:
        log(f"free request failed: HTTP {exc.code}")
        return 1
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        log(f"ComfyUI became unavailable during free; completion unconfirmed ({exc})")
        return 1

    after = wait_for_release(client, base, args.timeout, before, args.settle_timeout)
    if after is None:
        after = snapshot(client, base, args.timeout)

    if before and after and before["vram_total"]:
        before_used = vram_used(before)
        after_used = vram_used(after)
        freed = max(0, before_used - after_used)
        log(f"freed cached models and node outputs: VRAM used "
            f"{before_used / GIB:.2f} -> {after_used / GIB:.2f} GiB "
            f"({freed / GIB:.2f} GiB released); RAM free "
            f"{before['ram_free'] / GIB:.2f} -> {after['ram_free'] / GIB:.2f} GiB")
    elif before:
        log(f"freed cached models and node outputs; RAM free "
            f"{before['ram_free'] / GIB:.2f} GiB")
    else:
        log("freed cached models and node outputs (system stats unavailable)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
