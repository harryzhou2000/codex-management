"""Small helpers: unit parsing, timestamps, hashing."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path

SIZE_UNITS = {
    "b": 1,
    "k": 1000, "kb": 1000, "kib": 1024,
    "m": 1000**2, "mb": 1000**2, "mib": 1024**2,
    "g": 1000**3, "gb": 1000**3, "gib": 1024**3,
    "t": 1000**4, "tb": 1000**4, "tib": 1024**4,
}

LOCAL_TZ = dt.datetime.now().astimezone().tzinfo


def parse_size(text: str) -> int:
    """Accept 1G, 1GB, 1GiB, 1073741824, 200M. Bare numbers mean bytes."""

    raw = text.strip().lower().replace("_", "").replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([a-z]*)", raw)
    if not match:
        raise ValueError(f"cannot parse size: {text!r}")
    number, unit = match.group(1), match.group(2)
    if unit and unit not in SIZE_UNITS:
        raise ValueError(f"unknown size unit: {unit!r}")
    return int(float(number) * SIZE_UNITS.get(unit or "b", 1))


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


def parse_date(text: str) -> dt.datetime:
    """Midnight local time for YYYY-MM-DD or YYYY/MM/DD."""

    cleaned = text.strip().replace("/", "-")
    day = dt.date.fromisoformat(cleaned)
    return dt.datetime.combine(day, dt.time.min, tzinfo=LOCAL_TZ)


def local_iso(epoch_seconds: float | int | None) -> str:
    if not epoch_seconds:
        return ""
    return dt.datetime.fromtimestamp(float(epoch_seconds), LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def local_iso_ms(epoch_ms: float | int | None) -> str:
    if not epoch_ms:
        return ""
    return local_iso(float(epoch_ms) / 1000.0)


def sha256_file(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def size_or_zero(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def mtime_or_zero(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
