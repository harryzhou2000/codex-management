"""Compress rollouts into an archive root, verify them, and optionally hand deletion to the CLI.

Nothing here writes to the Codex home. Deletion is delegated to the codex delete command, which
is the only component that keeps state_*.sqlite consistent with the rollout tree.

An existing archive never blocks a requested deletion: when the ledger already holds a verified
copy, that copy is re-hashed and the session is deleted without recompressing. A deletion is
refused outright if the archive is missing or its hash no longer matches.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import util

LEDGER_NAME = "ledger.jsonl"


def require_zstd() -> str:
    binary = shutil.which("zstd")
    if not binary:
        raise SystemExit("zstd not found on PATH")
    return binary


def load_ledger(path: Path) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    if not path.exists():
        return entries
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("session_id"):
                entries[record["session_id"]] = record
    return entries


def append_ledger(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def compress(src: Path, dst: Path, level: int = 6) -> None:
    binary = require_zstd()
    dst.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [binary, "-T0", f"-{level}", "-q", "--check", "-f", "-o", str(dst), str(src)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"zstd failed for {src}: {result.stderr.strip()}")


def decoded_sha256(archive: Path, chunk: int = 4 * 1024 * 1024) -> str:
    binary = require_zstd()
    digest = hashlib.sha256()
    with subprocess.Popen([binary, "-dc", str(archive)], stdout=subprocess.PIPE) as proc:
        assert proc.stdout is not None
        for block in iter(lambda: proc.stdout.read(chunk), b""):
            digest.update(block)
    if proc.returncode != 0:
        raise SystemExit(f"zstd -dc failed for {archive}")
    return digest.hexdigest()


def _target(archive_root: Path, row: dict) -> Path:
    month = (row.get("last_activity") or "unknown")[:7] or "unknown"
    return archive_root / month / f"{row['session_id']}.jsonl.zst"


def _delete_session(session_id: str, src: Path) -> tuple[str, dict]:
    """Run codex delete and report what actually happened on disk."""

    # Outside an interactive terminal the CLI demands --force plus a session UUID; plans always
    # carry UUIDs, so this is the non-interactive form of the same command.
    command = ["codex", "delete", "--force", session_id]
    result = subprocess.run(command, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()[:400]
    source_gone = not src.exists()
    info = {
        "delete_command": " ".join(command),
        "delete_returncode": result.returncode,
        "delete_output": output,
        "deleted_source": bool(result.returncode == 0 and source_gone),
        "source_still_present": not source_gone,
    }
    if result.returncode != 0:
        return "delete-failed", info
    return ("deleted", info) if source_gone else ("delete-incomplete:file-remains", info)


def backup_one(
    row: dict,
    *,
    archive_root: Path,
    ledger: dict[str, dict],
    apply: bool = False,
    delete: bool = False,
    level: int = 6,
    force: bool = False,
) -> dict:
    src = Path(row["rollout_path"])
    dst = _target(archive_root, row)
    outcome = {
        "session_id": row["session_id"],
        "src": str(src),
        "dst": str(dst),
        "size_bytes": row.get("size_bytes", 0),
        "action": "dry-run",
    }

    if not src.exists():
        outcome["action"] = "skipped:missing"
        return outcome
    size = src.stat().st_size
    if row.get("size_bytes") and size != row["size_bytes"]:
        outcome["action"] = "skipped:size-changed"
        return outcome

    previous = ledger.get(row["session_id"])
    reused = bool(
        previous and previous.get("verified") and previous.get("src_size") == size and not force
    )

    if reused and not delete:
        outcome["action"] = "skipped:already-backed-up"
        outcome["archive"] = previous.get("archive_path", "")
        return outcome
    if reused and previous.get("deleted_source"):
        outcome["action"] = "skipped:already-deleted"
        return outcome

    archive: Path | None = None
    if reused:
        archive = Path(previous.get("archive_path", ""))
        if not archive.exists():
            # Deleting without the copy is the one thing that must never happen.
            outcome["action"] = "failed:archive-missing"
            return outcome
    if not apply:
        return outcome

    if reused:
        if decoded_sha256(archive) != previous.get("decoded_sha256"):
            outcome["action"] = "failed:archive-hash-mismatch"
            return outcome
        record = dict(previous)
        record["reused_archive"] = True
        outcome["action"] = "verified-existing-archive"
    else:
        src_hash = util.sha256_file(src)
        compress(src, dst, level=level)
        decoded = decoded_sha256(dst)
        if decoded != src_hash:
            outcome["action"] = "failed:hash-mismatch"
            outcome["src_sha256"] = src_hash
            outcome["decoded_sha256"] = decoded
            return outcome
        record = {
            "session_id": row["session_id"],
            "backed_up_at": util.dt.datetime.now(util.LOCAL_TZ).isoformat(timespec="seconds"),
            "src_path": str(src),
            "src_size": size,
            "src_sha256": src_hash,
            "src_mtime": util.local_iso(src.stat().st_mtime),
            "archive_path": str(dst),
            "archive_size": dst.stat().st_size,
            "decoded_sha256": decoded,
            "verified": True,
            "title": row.get("title", ""),
            "cwd": row.get("cwd", ""),
            "last_activity": row.get("last_activity", ""),
            "role_label": row.get("role_label", ""),
            "tree_root": row.get("tree_root", ""),
            "deleted_source": False,
        }
        outcome["action"] = "backed-up"

    if delete:
        action, info = _delete_session(row["session_id"], src)
        record.update(info)
        outcome["action"] = (
            f"{action}:archive-reused" if reused else f"{action}:just-archived"
        )
        archive_path = record.get("archive_path")
        if archive_path:
            outcome["archive"] = archive_path

    if not reused or delete:
        append_ledger(archive_root / LEDGER_NAME, record)
    ledger[row["session_id"]] = record
    outcome.setdefault("archive", str(dst))
    return outcome


def run(
    rows: list[dict],
    *,
    archive_root: Path,
    apply: bool = False,
    delete: bool = False,
    level: int = 6,
    force: bool = False,
) -> list[dict]:
    archive_root = archive_root.expanduser()
    ledger = load_ledger(archive_root / LEDGER_NAME)
    # Deepest threads first, so a subagent is archived and pruned before its parent.
    ordered = sorted(rows, key=lambda row: row.get("thread_depth", 0), reverse=True)
    results = []
    for index, row in enumerate(ordered, start=1):
        result = backup_one(
            row, archive_root=archive_root, ledger=ledger,
            apply=apply, delete=delete, level=level, force=force,
        )
        results.append(result)
        print(
            f"  [{index:>4}/{len(ordered)}] {result['action']:<32} {result['session_id']}",
            file=sys.stderr,
        )
    return results
