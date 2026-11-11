"""Command line interface for Codex session analytics."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .catalog import ThreadMeta, display_name, load_ledger, load_threads, ledger_index, meaningful
from .context import build_payload, render_preview, render_turn_preview, write_context_outputs
from .index import AnalyticsIndex
from .paths import (
    archived_dir,
    default_archive_root,
    default_cache_path,
    session_id_from_path,
    sessions_dir,
    state_db,
)
from .pricing import PriceBook
from .rollout import merge_summaries, parse_rollout
from .stats import (
    SESSIONS_COLUMNS,
    SessionRow,
    collect_rows,
    human_tokens,
    human_usd,
    plot_overview,
    render_table,
    summarize,
    summary_text,
    write_reports,
)


def _home(args: argparse.Namespace) -> Path:
    return Path(args.codex_home).expanduser() if args.codex_home else Path.home() / ".codex"


def _archive_root(args: argparse.Namespace) -> Path | None:
    if args.archive_root:
        return Path(args.archive_root).expanduser()
    return default_archive_root()


def _pricebook(args: argparse.Namespace) -> PriceBook:
    return PriceBook.load(args.prices)


def _cache(args: argparse.Namespace) -> Path:
    return Path(args.cache).expanduser() if args.cache else default_cache_path()


def _progress(label: str):
    def report(number: int, total: int, path: Path, status: str) -> None:
        if number == 1 or number == total or number % 25 == 0:
            print(
                f"\r{label}: {number:,}/{total:,} {status:<8} {path.name[:60]}",
                end="",
                file=sys.stderr,
                flush=True,
            )
            if number == total:
                print(file=sys.stderr)

    return report


def _write_rows_json(rows: list[SessionRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([row.serialize() for row in rows], indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _write_rows_csv(rows: list[SessionRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SESSIONS_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(row.serialize() for row in rows)


def _sort_rows(rows: list[SessionRow], key: str) -> list[SessionRow]:
    if key == "tokens":
        return sorted(rows, key=lambda row: (row.tokens, row.updated), reverse=True)
    if key == "size":
        return sorted(rows, key=lambda row: (row.disk_bytes, row.updated), reverse=True)
    if key == "updated":
        return sorted(rows, key=lambda row: row.updated, reverse=True)
    if key == "name":
        return sorted(rows, key=lambda row: row.name.lower())
    return sorted(rows, key=lambda row: (row.cost_usd or 0.0, row.tokens), reverse=True)


def cmd_stats(args: argparse.Namespace) -> int:
    home = _home(args)
    archive_root = _archive_root(args)
    book = _pricebook(args)
    started = time.time()
    with AnalyticsIndex(_cache(args)) as index:
        live_result = index.index_live(
            home,
            book,
            force=args.refresh,
            progress=None if args.quiet else _progress("live rollouts"),
        )
        archive_result = None
        if args.include_archives:
            ledger = load_ledger(archive_root)
            archive_result = index.index_archives(
                ledger,
                book,
                force=args.refresh,
                progress=None if args.quiet else _progress("archives"),
            )
        rows = collect_rows(
            codex_home=home,
            archive_root=archive_root,
            index=index,
            pricebook=book,
            thread_source=args.thread_source,
            live_only=args.live_only,
        )
        if args.since:
            try:
                cutoff = datetime.strptime(args.since, "%Y-%m-%d").timestamp()
            except ValueError:
                print("--since must be YYYY-MM-DD", file=sys.stderr)
                return 2
            rows = [row for row in rows if row.updated >= cutoff]
        if args.project:
            needle = args.project.lower()
            rows = [row for row in rows if needle in (row.cwd + " " + row.project).lower()]
        rows = _sort_rows(rows, args.sort)
        summary = summarize(rows)
        print(render_table(rows, 0 if args.all else args.limit))
        print()
        print(summary_text(summary))
        if archive_result is not None:
            print(
                f"archive index: indexed={archive_result.indexed:,} skipped={archive_result.skipped:,} "
                f"errors={archive_result.errors:,}"
            )
        elif archive_root is not None:
            ledger_count = len(ledger_index(load_ledger(archive_root)))
            indexed_archives = len(index.archive_files())
            if ledger_count and indexed_archives < ledger_count:
                print(
                    f"note: {indexed_archives:,}/{ledger_count:,} archived sessions have parsed detail; "
                    "run `csa stats --include-archives` to index the rest."
                )
        print(
            f"timing: {time.time() - started:.1f}s  live indexed={live_result.indexed:,} "
            f"skipped={live_result.skipped:,} errors={live_result.errors:,}  cache={_cache(args)}"
        )

        report_out = args.out or ("analytics/summary" if args.plot else None)
        if report_out:
            out_dir = Path(report_out).expanduser()
            paths = write_reports(rows, summary, out_dir)
            if args.plot:
                for key, path in enumerate(plot_overview(rows, out_dir)):
                    paths[f"overview_{key}"] = path
            for name, path in paths.items():
                if path is not None:
                    print(f"wrote {name}: {path}")
        if args.json:
            _write_rows_json(rows, Path(args.json).expanduser())
        if args.csv:
            _write_rows_csv(rows, Path(args.csv).expanduser())
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    home = _home(args)
    archive_root = _archive_root(args)
    book = _pricebook(args)
    started = time.time()
    with AnalyticsIndex(_cache(args)) as index:
        live_result = index.index_live(
            home,
            book,
            force=args.refresh,
            progress=None if args.quiet else _progress("live rollouts"),
        )
        print(
            f"live: indexed={live_result.indexed:,} skipped={live_result.skipped:,} errors={live_result.errors:,}"
        )
        if args.include_archives:
            ledger = load_ledger(archive_root)
            archive_result = index.index_archives(
                ledger,
                book,
                force=args.refresh,
                progress=None if args.quiet else _progress("archives"),
            )
            print(
                f"archives: indexed={archive_result.indexed:,} skipped={archive_result.skipped:,} "
                f"errors={archive_result.errors:,} ledger={len(ledger_index(ledger)):,}"
            )
        errors = index.errors()
        if errors:
            print(f"errors ({len(errors)}):", file=sys.stderr)
            for error in errors[:10]:
                print(f"  {error['path']}: {error['error']}", file=sys.stderr)
        print(f"cache: {_cache(args)}")
        print(f"elapsed: {time.time() - started:.1f}s")
    return 0


def _live_ids(home: Path) -> set[str]:
    ids: set[str] = set()
    for root in (sessions_dir(home), archived_dir(home)):
        if not root.exists():
            continue
        for path in root.rglob("rollout-*.jsonl"):
            session_id = _first_session_id(path) or session_id_from_path(path)
            if session_id:
                ids.add(session_id)
    return ids


def _first_session_id(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"session_meta"' not in line[:200]:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload") or {}
                value = payload.get("id") or payload.get("session_id")
                return str(value) if value else None
    except OSError:
        return None
    return None


def _live_paths(home: Path, session_id: str, meta: ThreadMeta | None) -> list[Path]:
    paths: list[Path] = []
    if meta and meta.rollout_path:
        path = Path(meta.rollout_path).expanduser()
        if path.exists():
            paths.append(path)
    for root in (sessions_dir(home), archived_dir(home)):
        if not root.exists():
            continue
        for path in sorted(root.rglob("rollout-*.jsonl")):
            if path in paths:
                continue
            if session_id in path.name or _first_session_id(path) == session_id:
                paths.append(path)
    return paths


def _resolve_session(
    query: str, home: Path, archive_root: Path | None
) -> tuple[str, ThreadMeta | None, dict | None, list[Path], str]:
    threads = load_threads(state_db(home))
    ledger_records = load_ledger(archive_root)
    ledger = ledger_index(ledger_records)
    ids = set(threads) | set(ledger) | _live_ids(home)

    def name_for(session_id: str) -> str:
        return display_name(threads.get(session_id), ledger.get(session_id))[0]

    if query in ids:
        matches = [query]
    else:
        matches = sorted(session_id for session_id in ids if session_id.startswith(query))
        if not matches:
            lowered = query.lower()
            matches = sorted(
                session_id for session_id in ids if lowered in name_for(session_id).lower()
            )
        if not matches:
            matches = sorted(
                session_id
                for session_id in ids
                if lowered in (
                    (threads.get(session_id).cwd if threads.get(session_id) else "")
                    + " "
                    + str(ledger.get(session_id, {}).get("cwd") or "")
                ).lower()
            )
    if not matches:
        raise SystemExit(f"no session matched {query!r}")
    if len(matches) > 1:
        lines = [f"ambiguous session {query!r}; candidates:"]
        for session_id in matches[:30]:
            lines.append(f"  {session_id}  {name_for(session_id)}")
        raise SystemExit("\n".join(lines))

    session_id = matches[0]
    meta = threads.get(session_id)
    record = ledger.get(session_id)
    live_paths = _live_paths(home, session_id, meta)
    if live_paths:
        return session_id, meta, record, live_paths, "live"
    archive_paths = sorted(
        {
            str(Path(str(item["archive_path"])).expanduser())
            for item in ledger_records
            if str(item.get("session_id") or "") == session_id
            and Path(str(item.get("archive_path") or "")).expanduser().exists()
        }
    )
    if archive_paths:
        return session_id, meta, record, [Path(path) for path in archive_paths], "archive"
    raise SystemExit(f"session {session_id} has no rollout or archive on this host")


def cmd_context(args: argparse.Namespace) -> int:
    home = _home(args)
    archive_root = _archive_root(args)
    book = _pricebook(args)
    session_id, meta, record, paths, source = _resolve_session(args.session, home, archive_root)
    summaries = [parse_rollout(path, book, collect_items=True) for path in paths]
    summary = merge_summaries(summaries, book)
    name, _ = display_name(meta, record)
    cwd = (meta.cwd if meta else "") or str(record.get("cwd") if record else "") or summary.cwd
    thread_source = (meta.thread_source if meta else "") or summary.thread_source
    if name == "(untitled)":
        if meaningful(summary.first_user_message, 12):
            name = " ".join(summary.first_user_message.split())[:160]
        else:
            project = Path(cwd).name if cwd else "unknown-project"
            name = f"(untitled) · {project} · {session_id[:8]}"
    payload = build_payload(
        summary,
        name=name,
        thread_source=thread_source,
        cwd=cwd,
        source=source,
        max_chars=None if args.full_text else args.max_chars,
        granularity=args.granularity,
        last_turns=args.last_turns,
        last_messages=args.last_messages,
        since=args.since,
        until=args.until,
        last_days=args.last_days,
        last_hours=args.last_hours,
    )
    out_dir = Path(args.out).expanduser() if args.out else Path.cwd() / "analytics" / session_id
    paths = write_context_outputs(
        payload, out_dir, plot=not args.no_plot, max_points=args.max_points
    )
    info = payload["summary"]
    print(f"session: {name} [{session_id}]")
    print(f"source: {source} {', '.join(str(path) for path in summary.paths)}")
    print(f"context: {cwd}")
    print(
        f"calls: {info['calls']:,}  items: {info['items']:,}  compactions: {info['compactions']:,}  "
        f"turns: {info.get('turns', 0):,}  context window: {info['context_window'] or '?'}"
    )
    print(
        f"selected: input {info['input_tokens']:,} (cached {info['cached_input_tokens']:,})  "
        f"output {info['output_tokens']:,}  cost {human_usd(info['cost_usd'])}  "
        f"lifetime {human_tokens(info.get('lifetime_tokens', 0))} tokens / {human_usd(info.get('lifetime_cost_usd', 0))}"
    )
    raw_counter = payload["session"].get("raw_lifetime_tokens") or 0
    if raw_counter and raw_counter != info.get("lifetime_tokens"):
        initial_counter = payload["session"].get("initial_counter_tokens") or 0
        print(
            f"counter adjustment: raw final {raw_counter:,}, inherited baseline {initial_counter:,}, "
            f"own {info.get('lifetime_tokens', 0):,}"
        )
    for label, output_path in paths.items():
        print(f"wrote {label}: {output_path}")
    print()
    if args.granularity == "turn":
        print(render_turn_preview(payload, head=args.preview, tail=args.preview))
    else:
        print(render_preview(payload, head=args.preview, tail=args.preview))
    return 0


def cmd_prices(args: argparse.Namespace) -> int:
    book = _pricebook(args)
    rows = book.rows()
    print(f"prices as of {book.data.get('as_of', '?')} ({book.data.get('unit', 'per_1m_tokens')})")
    print(
        f"{'model':<28} {'input':>9} {'cached':>9} {'cache write':>12} {'output':>9}  notes"
    )
    print("-" * 92)
    for row in rows:
        notes = "peak-aware" if row.get("peak") else ""
        if row.get("long_context") is None:
            notes = (notes + " flat-context").strip()
        print(
            f"{row['model']:<28} {human_usd(row['input']):>9} {human_usd(row['cached_input']):>9} "
            f"{human_usd(row['cache_write']):>12} {human_usd(row['output']):>9}  {notes}"
        )
    sources = book.data.get("sources") or {}
    if sources:
        print("sources:")
        for name, url in sources.items():
            print(f"  {name}: {url}")
    if args.json:
        path = Path(args.json).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(book.data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {path}")
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--codex-home", help="Codex home (default: $CODEX_HOME or ~/.codex)")
    parser.add_argument("--cache", help=f"index cache (default: {default_cache_path()})")
    parser.add_argument("--prices", help="JSON file merged over the built-in price table")
    parser.add_argument("--archive-root", help="backup archive root containing ledger.jsonl")
    parser.add_argument("--quiet", action="store_true", help="suppress indexing progress")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csa",
        description="Per-session Codex token, cost, disk, and context analytics (read-only).",
    )
    parser.add_argument("--version", action="version", version="codex-session-analytics 0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    stats = subparsers.add_parser("stats", help="per-session table, totals, CSV/JSON, charts")
    _add_common(stats)
    stats.add_argument("--limit", type=int, default=30, help="rows to print; 0 prints all")
    stats.add_argument("--all", action="store_true", help="print every row")
    stats.add_argument(
        "--sort", choices=["cost", "tokens", "size", "updated", "name"], default="cost"
    )
    stats.add_argument(
        "--thread-source",
        choices=["any", "user", "subagent", "guardian_review"],
        default="any",
    )
    stats.add_argument("--since", help="only sessions updated on/after YYYY-MM-DD")
    stats.add_argument("--project", help="only sessions whose cwd contains this text")
    stats.add_argument("--live-only", action="store_true", help="only sessions with a live rollout")
    stats.add_argument("--include-archives", action="store_true", help="parse zstd archives")
    stats.add_argument("--refresh", action="store_true", help="reparse unchanged files")
    stats.add_argument("--out", help="write sessions.csv/json, session_models.csv, summary.json")
    stats.add_argument(
        "--plot",
        action="store_true",
        help="write aggregate charts (default output: analytics/summary)",
    )
    stats.add_argument("--json", help="write the session list to this JSON path")
    stats.add_argument("--csv", help="write the session list to this CSV path")
    stats.set_defaults(func=cmd_stats)

    index = subparsers.add_parser("index", help="build or refresh the parsed-usage cache")
    _add_common(index)
    index.add_argument("--include-archives", action="store_true", help="parse zstd archives")
    index.add_argument("--refresh", action="store_true", help="reparse unchanged files")
    index.set_defaults(func=cmd_index)

    context = subparsers.add_parser("context", help="extract one session's context history and plot")
    _add_common(context)
    context.add_argument("session", help="session id, id prefix, name fragment, or cwd fragment")
    context.add_argument("--out", help="output directory (default: ./analytics/<session-id>)")
    context.add_argument("--max-chars", type=int, default=1000, help="per-item text cap; 0 = full")
    context.add_argument("--full-text", action="store_true", help="keep full item text in outputs")
    context.add_argument(
        "--granularity",
        choices=["message", "turn"],
        default="message",
        help="message-level or turn-snapped context view",
    )
    context.add_argument("--last-turns", type=int, help="keep only the last N turns")
    context.add_argument("--last-messages", type=int, help="keep only the last N context items")
    context.add_argument("--last-days", type=float, help="keep items from the last N days of the session")
    context.add_argument("--last-hours", type=float, help="keep items from the last N hours of the session")
    context.add_argument("--since", help="keep items at/after YYYY-MM-DD or ISO timestamp")
    context.add_argument("--until", help="keep items at/before YYYY-MM-DD or ISO timestamp")
    context.add_argument("--no-plot", action="store_true", help="skip the context.png figure")
    context.add_argument("--max-points", type=int, default=20000, help="bins before plotting aggregation")
    context.add_argument("--preview", type=int, default=6, help="head/tail rows to print")
    context.set_defaults(func=cmd_context)

    prices = subparsers.add_parser("prices", help="show the effective model price table")
    _add_common(prices)
    prices.add_argument("--json", help="write the effective price table as JSON")
    prices.set_defaults(func=cmd_prices)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        return int(exc.code or 0)
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
