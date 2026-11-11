"""Incremental SQLite cache of per-rollout segments.

A logical Codex session can span several rollout files. Each file records the thread's
lifetime token usage as well as the calls retained in that file, so segments stay separate in
the cache and are combined by the report layer.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .paths import archived_dir, session_id_from_path, sessions_dir
from .pricing import ModelMix, PriceBook
from .rollout import RolloutSummary, parse_rollout

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    kind TEXT NOT NULL,
    session_id TEXT,
    status TEXT NOT NULL,
    error TEXT,
    parsed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
    source_path TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_bytes INTEGER NOT NULL,
    source_mtime_ns INTEGER NOT NULL,
    parsed_at TEXT NOT NULL,
    calls INTEGER NOT NULL,
    retained_input_tokens INTEGER NOT NULL,
    retained_cached_input_tokens INTEGER NOT NULL,
    retained_cache_write_input_tokens INTEGER NOT NULL,
    retained_output_tokens INTEGER NOT NULL,
    retained_reasoning_output_tokens INTEGER NOT NULL,
    retained_tokens INTEGER NOT NULL,
    lifetime_input_tokens INTEGER NOT NULL,
    lifetime_cached_input_tokens INTEGER NOT NULL,
    lifetime_cache_write_input_tokens INTEGER NOT NULL,
    lifetime_output_tokens INTEGER NOT NULL,
    lifetime_reasoning_output_tokens INTEGER NOT NULL,
    lifetime_tokens INTEGER NOT NULL,
    retained_cost_usd REAL,
    retained_priced_tokens INTEGER NOT NULL,
    retained_unpriced_tokens INTEGER NOT NULL,
    estimated_tokens INTEGER NOT NULL,
    estimated_cost_usd REAL,
    compaction_count INTEGER NOT NULL,
    context_window INTEGER,
    first_ts TEXT,
    last_ts TEXT,
    cwd TEXT,
    session_model TEXT,
    thread_source TEXT,
    first_user_message TEXT,
    cli_version TEXT
);
CREATE TABLE IF NOT EXISTS segment_models (
    source_path TEXT NOT NULL,
    session_id TEXT NOT NULL,
    model TEXT NOT NULL,
    calls INTEGER NOT NULL,
    retained_input_tokens INTEGER NOT NULL,
    retained_cached_input_tokens INTEGER NOT NULL,
    retained_cache_write_input_tokens INTEGER NOT NULL,
    retained_output_tokens INTEGER NOT NULL,
    retained_reasoning_output_tokens INTEGER NOT NULL,
    retained_tokens INTEGER NOT NULL,
    retained_cost_usd REAL,
    retained_priced_tokens INTEGER NOT NULL,
    retained_unpriced_tokens INTEGER NOT NULL,
    estimated_tokens INTEGER NOT NULL,
    PRIMARY KEY (source_path, model)
);
CREATE INDEX IF NOT EXISTS idx_segments_session ON segments(session_id);
CREATE INDEX IF NOT EXISTS idx_segment_models_model ON segment_models(model);
CREATE INDEX IF NOT EXISTS idx_files_kind ON files(kind);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class IndexResult:
    indexed: int = 0
    skipped: int = 0
    errors: int = 0

    def add(self, status: str) -> None:
        if status == "indexed":
            self.indexed += 1
        elif status == "skipped":
            self.skipped += 1
        else:
            self.errors += 1


class AnalyticsIndex:
    def __init__(self, path: Path):
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        had_segments = self.connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'segments'"
        ).fetchone()
        self.connection.executescript(SCHEMA)
        segment_columns = {
            row["name"] for row in self.connection.execute("pragma table_info(segments)")
        }
        if "first_user_message" not in segment_columns:
            self.connection.execute("alter table segments add column first_user_message TEXT")
        if not had_segments:
            # Schema replaced the old session-keyed tables; force a reparse of cached files.
            self.connection.execute("delete from files")
        self.connection.execute("drop table if exists session_models")
        self.connection.execute("drop table if exists sessions")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "AnalyticsIndex":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _file_state(self, path: Path) -> sqlite3.Row | None:
        return self.connection.execute(
            "select size, mtime_ns, status from files where path = ?", (str(path),)
        ).fetchone()

    def _delete_segment(self, path: Path) -> None:
        self.connection.execute("delete from segment_models where source_path = ?", (str(path),))
        self.connection.execute("delete from segments where source_path = ?", (str(path),))

    def index_file(
        self,
        path: Path,
        pricebook: PriceBook,
        *,
        kind: str,
        source_bytes: int | None = None,
        force: bool = False,
    ) -> str:
        try:
            stat = path.stat()
        except OSError as exc:
            self.connection.execute(
                "insert or replace into files(path,size,mtime_ns,kind,session_id,status,error,parsed_at) "
                "values(?,?,?,?,?,?,?,?)",
                (str(path), 0, 0, kind, session_id_from_path(path), "error", str(exc), _now()),
            )
            self.connection.commit()
            return "error"
        previous = self._file_state(path)
        if (
            not force
            and previous is not None
            and previous["size"] == stat.st_size
            and previous["mtime_ns"] == stat.st_mtime_ns
            and previous["status"] == "ok"
        ):
            return "skipped"
        try:
            summary: RolloutSummary = parse_rollout(path, pricebook, collect_items=False)
        except Exception as exc:  # One malformed rollout must not poison the rest of the index.
            with self.connection:
                self._delete_segment(path)
                self.connection.execute(
                    "insert or replace into files(path,size,mtime_ns,kind,session_id,status,error,parsed_at) "
                    "values(?,?,?,?,?,?,?,?)",
                    (
                        str(path),
                        stat.st_size,
                        stat.st_mtime_ns,
                        kind,
                        session_id_from_path(path),
                        "error",
                        str(exc),
                        _now(),
                    ),
                )
            return "error"

        retained = summary.retained_usage
        lifetime = summary.thread_usage
        recorded_bytes = int(source_bytes or stat.st_size)
        parsed_at = _now()
        with self.connection:
            self._delete_segment(path)
            self.connection.execute(
                """insert or replace into segments(
                    source_path, session_id, source_kind, source_bytes, source_mtime_ns, parsed_at,
                    calls,
                    retained_input_tokens, retained_cached_input_tokens,
                    retained_cache_write_input_tokens, retained_output_tokens,
                    retained_reasoning_output_tokens, retained_tokens,
                    lifetime_input_tokens, lifetime_cached_input_tokens,
                    lifetime_cache_write_input_tokens, lifetime_output_tokens,
                    lifetime_reasoning_output_tokens, lifetime_tokens,
                    retained_cost_usd, retained_priced_tokens, retained_unpriced_tokens,
                    estimated_tokens, estimated_cost_usd, compaction_count, context_window,
                    first_ts, last_ts, cwd, session_model, thread_source,
                    first_user_message, cli_version
                ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(path),
                    summary.session_id,
                    kind,
                    recorded_bytes,
                    stat.st_mtime_ns,
                    parsed_at,
                    len(summary.calls),
                    retained.input,
                    retained.cached,
                    retained.cache_write,
                    retained.output,
                    retained.reasoning,
                    retained.total,
                    lifetime.input,
                    lifetime.cached,
                    lifetime.cache_write,
                    lifetime.output,
                    lifetime.reasoning,
                    lifetime.total,
                    summary.retained_cost_usd,
                    summary.retained_priced_tokens,
                    summary.retained_unpriced_tokens,
                    summary.estimated_tokens,
                    summary.estimated_cost_usd,
                    summary.compaction_count,
                    summary.context_window,
                    summary.first_ts,
                    summary.last_ts,
                    summary.cwd,
                    summary.session_model,
                    summary.thread_source,
                    summary.first_user_message,
                    summary.cli_version,
                ),
            )
            for model, model_usage in summary.models.items():
                self.connection.execute(
                    """insert or replace into segment_models(
                        source_path, session_id, model, calls,
                        retained_input_tokens, retained_cached_input_tokens,
                        retained_cache_write_input_tokens, retained_output_tokens,
                        retained_reasoning_output_tokens, retained_tokens,
                        retained_cost_usd, retained_priced_tokens, retained_unpriced_tokens,
                        estimated_tokens
                    ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(path),
                        summary.session_id,
                        model,
                        model_usage.calls,
                        model_usage.retained_usage.input,
                        model_usage.retained_usage.cached,
                        model_usage.retained_usage.cache_write,
                        model_usage.retained_usage.output,
                        model_usage.retained_usage.reasoning,
                        model_usage.retained_usage.total,
                        model_usage.retained_cost_usd,
                        model_usage.retained_priced_tokens,
                        model_usage.retained_unpriced_tokens,
                        model_usage.estimated_tokens,
                    ),
                )
            self.connection.execute(
                "insert or replace into files(path,size,mtime_ns,kind,session_id,status,error,parsed_at) "
                "values(?,?,?,?,?,?,?,?)",
                (
                    str(path),
                    stat.st_size,
                    stat.st_mtime_ns,
                    kind,
                    summary.session_id,
                    "ok",
                    None,
                    parsed_at,
                ),
            )
        return "indexed"

    def index_live(
        self,
        codex_home: Path,
        pricebook: PriceBook,
        *,
        force: bool = False,
        progress: Callable[[int, int, Path, str], None] | None = None,
    ) -> IndexResult:
        paths: list[Path] = []
        for root in (sessions_dir(codex_home), archived_dir(codex_home)):
            if root.exists():
                paths.extend(sorted(root.rglob("rollout-*.jsonl")))
        result = IndexResult()
        for number, path in enumerate(paths, start=1):
            status = self.index_file(path, pricebook, kind="live", force=force)
            result.add(status)
            if progress:
                progress(number, len(paths), path, status)
        return result

    def index_archives(
        self,
        ledger: Iterable[dict],
        pricebook: PriceBook,
        *,
        force: bool = False,
        progress: Callable[[int, int, Path, str], None] | None = None,
    ) -> IndexResult:
        selected: dict[str, dict] = {}
        for record in ledger:
            archive_path = record.get("archive_path")
            if not archive_path:
                continue
            path = Path(str(archive_path)).expanduser()
            if path.exists():
                selected[str(path)] = record
        records = list(selected.values())
        result = IndexResult()
        for number, record in enumerate(records, start=1):
            path = Path(str(record["archive_path"])).expanduser()
            status = self.index_file(
                path,
                pricebook,
                kind="archive",
                source_bytes=int(record.get("src_size") or 0),
                force=force,
            )
            result.add(status)
            if progress:
                progress(number, len(records), path, status)
        return result

    def segments_by_session(self) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {}
        for row in self.connection.execute(
            "select * from segments order by session_id, first_ts, source_path"
        ):
            result.setdefault(row["session_id"], []).append(dict(row))
        for row in self.connection.execute(
            "select * from segment_models order by retained_tokens desc"
        ):
            session_id = row["session_id"]
            source_path = row["source_path"]
            for segment in result.get(session_id, []):
                if segment["source_path"] == source_path:
                    segment.setdefault("models", []).append(dict(row))
                    break
        return result

    def segment_paths(self, session_id: str) -> list[Path]:
        rows = self.connection.execute(
            "select source_path from segments where session_id = ? order by first_ts, source_path",
            (session_id,),
        )
        return [Path(row["source_path"]) for row in rows]

    def model_mixes(self) -> dict[str, ModelMix]:
        mixes: dict[str, ModelMix] = {}
        for row in self.connection.execute(
            """select model,
                      sum(retained_input_tokens) as input_tokens,
                      sum(retained_cached_input_tokens) as cached_input_tokens,
                      sum(retained_output_tokens) as output_tokens
               from segment_models group by model"""
        ):
            input_tokens = int(row["input_tokens"] or 0)
            output_tokens = int(row["output_tokens"] or 0)
            total = input_tokens + output_tokens
            if total <= 0:
                continue
            mixes[row["model"]] = ModelMix(
                output_share=output_tokens / total,
                cached_share=(int(row["cached_input_tokens"] or 0) / input_tokens)
                if input_tokens
                else 0.0,
            )
        return mixes

    def live_files(self) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "select * from files where kind = 'live' and status = 'ok'"
            )
        ]

    def archive_files(self) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "select * from files where kind = 'archive' and status = 'ok'"
            )
        ]

    def errors(self) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute("select * from files where status = 'error'")
        ]
