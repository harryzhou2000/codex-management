"""Command line interface: csb catalog | select | backup | verify."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import backup, catalog, paths, selection, util

DEFAULT_OLDER_THAN = "2026-08-31"
DEFAULT_MIN_SIZE = "1GiB"
THREAD_SOURCES = ("user", "subagent", "guardian_review", "orphan", "any")
DISABLED = {"", "none", "off", "0"}


def _size_phrase(label: str, value: str) -> str:
    return f"{label} > {value}" if value.strip().lower() not in DISABLED else f"no {label} floor"


def _ellipsis(text: str, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: width - 2] + ".."


def _tail_ellipsis(text: str, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else ".." + text[-(width - 2):]


def _print_table(rows: list[dict], columns: list[tuple[str, str]], limit: int | None) -> None:
    shown = rows if limit is None else rows[:limit]
    widths = {}
    for key, title in columns:
        content = max((len(str(row.get(key, ""))) for row in shown), default=0)
        widths[key] = max(len(title), content)
    header = "  ".join(title.ljust(widths[key]) for key, title in columns)
    print(header)
    print("-" * len(header))
    for row in shown:
        print("  ".join(str(row.get(key, "")).ljust(widths[key]) for key, _ in columns))
    if limit is not None and len(rows) > limit:
        print(f"... {len(rows) - limit} more")


def cmd_catalog(args: argparse.Namespace) -> int:
    home = paths.codex_home(args.codex_home)
    rows = catalog.build_rows(
        home, recent_minutes=args.recent_minutes, scan_bytes=util.parse_size(args.scan_bytes)
    )
    out_dir = Path(args.out)
    csv_path, json_path = catalog.write_outputs(rows, out_dir)

    stats = selection.summarize(rows)
    sources = catalog.tree_stats(rows)
    names: dict[str, int] = {}
    for row in rows:
        names[row["name_source"]] = names.get(row["name_source"], 0) + 1
    print(f"codex home:  {home}")
    print(f"sessions:    {stats['sessions']} rows ({sum(1 for r in rows if r['orphan'])} orphan files)")
    print(f"total size:  {stats['total_human']}")
    print(
        "threads:     "
        f"main agents={sources.get('user', 0)}, subagents={sources.get('subagent', 0)}, "
        f"guardian_review={sources.get('guardian_review', 0)}, "
        f"parent issues={sources.get('issues', 0)}"
    )
    print("names from:  " + ", ".join(f"{key}={value}" for key, value in sorted(names.items())))
    print(f"written:     {csv_path}")
    print(f"             {json_path}")
    return 0


def _load_catalog(path: Path) -> list[dict]:
    if not path.exists():
        sibling = path.with_name("catalog.json")
        if sibling.exists():
            path = sibling
        else:
            raise SystemExit(f"no catalog at {path}")
    if path.suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            for key in ("size_bytes", "turns", "tokens_used", "archived", "thread_depth",
                        "subagent_count", "tree_size", "tree_bytes"):
                row[key] = int(row.get(key) or 0)
            row["last_activity_epoch"] = float(row.get("last_activity_epoch") or 0)
            for key in ("exists", "orphan", "recently_active"):
                row[key] = str(row.get(key)).lower() in {"true", "1", "yes"}
        return rows
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_select(args: argparse.Namespace) -> int:
    rows = _load_catalog(Path(args.catalog))
    sources = tuple(part.strip() for part in args.thread_source.split(",") if part.strip())
    unknown = [source for source in sources if source not in THREAD_SOURCES]
    if unknown:
        raise SystemExit(f"unknown thread source(s): {', '.join(unknown)}")

    selected, warnings = selection.apply_filters(
        rows,
        older_than=args.older_than,
        min_size=args.min_size,
        include_archived=not args.exclude_archived,
        include_orphans=not args.exclude_orphans,
        skip_recently_active=not args.include_active,
        min_turns=args.min_turns,
        min_tree_size=args.min_tree_size,
        thread_sources=sources,
        subagents=args.subagents,
    )
    stats = selection.summarize(selected)

    columns = [
        ("session_id", "session"), ("display_name", "name / title"), ("size_human", "size"),
        ("last_activity", "last activity"), ("project_path", "project path"),
        ("role_label", "role"), ("tree_size_human", "tree total"),
    ]
    for row in selected:
        row["display_name"] = _ellipsis(row.get("display_name") or "(untitled)", 36)
        row["project_path"] = _tail_ellipsis(row.get("project_path") or "", 36)
    _print_table(selected, columns, args.limit)

    print()
    tree_part = (
        f", {_size_phrase('tree size', args.min_tree_size)}" if args.min_tree_size else ""
    )
    print(
        f"criteria: last activity before {args.older_than}, "
        f"{_size_phrase('row size', args.min_size)}{tree_part}, "
        f"thread_source={','.join(sources)}, subagents={args.subagents}"
    )
    print(
        f"selected: {stats['sessions']} rollouts, {stats['total_human']} "
        f"({stats['main_agents']} main agents, {stats['subagents']} subagents, "
        f"{stats['archived_sessions']} archived)"
    )
    for warning in warnings:
        print(f"warning: {warning}")

    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = args.name or "plan"
        json_path = out_dir / f"{stem}.json"
        csv_path = out_dir / f"{stem}.csv"
        meta_path = out_dir / f"{stem}.meta.json"
        json_path.write_text(json.dumps(selected, indent=2), encoding="utf-8")
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            fields = catalog.CSV_COLUMNS + ["selection", "tree_root", "tree_depth", "reason"]
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(selected)
        meta_path.write_text(json.dumps({
            "generated_at": util.dt.datetime.now(util.LOCAL_TZ).isoformat(timespec="seconds"),
            "criteria": {
                "older_than": args.older_than,
                "min_size": args.min_size,
                "min_tree_size": args.min_tree_size,
                "thread_sources": list(sources),
                "subagents": args.subagents,
                "min_turns": args.min_turns,
                "exclude_archived": args.exclude_archived,
                "exclude_orphans": args.exclude_orphans,
                "include_active": args.include_active,
            },
            "counts": stats,
            "warnings": warnings,
        }, indent=2), encoding="utf-8")
        print(f"plan written: {json_path}")
        print(f"              {csv_path}")
        print(f"              {meta_path}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan)
    if not plan_path.exists():
        raise SystemExit(f"no plan at {plan_path}")
    rows = json.loads(plan_path.read_text(encoding="utf-8"))
    archive_root = Path(args.archive_root).expanduser()
    if not args.apply:
        print("DRY RUN - nothing is written. Pass --apply to compress, plus --delete to prune.")
    print(f"plan:         {plan_path} ({len(rows)} rollouts)")
    print(f"archive root: {archive_root}")
    results = backup.run(
        rows, archive_root=archive_root, apply=args.apply,
        delete=args.delete and args.apply, level=args.level, force=args.force,
    )
    summary: dict[str, int] = {}
    for result in results:
        summary[result["action"]] = summary.get(result["action"], 0) + 1
    print("summary:")
    for action, count in sorted(summary.items()):
        print(f"  {count:>4}  {action}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    archive_root = Path(args.archive_root).expanduser()
    ledger = backup.load_ledger(archive_root / backup.LEDGER_NAME)
    entries = list(ledger.values())
    if args.session:
        wanted = set(args.session)
        entries = [entry for entry in entries if entry["session_id"] in wanted]
    if args.limit:
        entries = entries[: args.limit]
    failures = 0
    for entry in entries:
        archive = Path(entry["archive_path"])
        if not archive.exists():
            print(f"MISSING {entry['session_id']} {archive}")
            failures += 1
            continue
        digest = backup.decoded_sha256(archive)
        ok = digest == entry["decoded_sha256"]
        failures += 0 if ok else 1
        state = "OK  " if ok else "FAIL"
        print(f"{state} {entry['session_id']} {util.human_size(archive.stat().st_size)}")
    print(f"checked {len(entries)} archives, {failures} problem(s)")
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="csb", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    cat = sub.add_parser("catalog", help="build a session catalog from Codex data")
    cat.add_argument("--codex-home", default=None)
    cat.add_argument("--out", default="catalog")
    cat.add_argument("--recent-minutes", type=int, default=15,
                     help="flag rollouts touched within this window as recently active")
    cat.add_argument("--scan-bytes", default="1MiB",
                     help="bytes of each rollout head to search for a first prompt when a session "
                          "has no name or title; 0 disables the scan")
    cat.set_defaults(func=cmd_catalog)

    sel = sub.add_parser("select", help="filter catalog rows into a backup plan")
    sel.add_argument("--catalog", default="catalog/catalog.json")
    sel.add_argument("--older-than", default=DEFAULT_OLDER_THAN,
                     help=f"last activity strictly before this date (default {DEFAULT_OLDER_THAN})")
    sel.add_argument("--min-size", default=DEFAULT_MIN_SIZE,
                     help=f"rollout size strictly greater than this (default {DEFAULT_MIN_SIZE})")
    sel.add_argument("--min-tree-size", default=None,
                     help="whole-tree size (own rollout plus every descendant) strictly greater "
                          "than this, to keep or drop a session as a unit")
    sel.add_argument("--min-turns", type=int, default=None)
    sel.add_argument("--thread-source", default="user",
                     help="comma separated subset of user,subagent,guardian_review,orphan,any "
                          "(default user: main agents only)")
    sel.add_argument("--subagents", choices=("tree", "none"), default="tree",
                     help="tree pulls every descendant of a selected main agent into the plan "
                          "(default); none selects only the matching rows")
    sel.add_argument("--exclude-archived", action="store_true")
    sel.add_argument("--exclude-orphans", action="store_true")
    sel.add_argument("--include-active", action="store_true",
                     help="also select rollouts touched in the last few minutes")
    sel.add_argument("--limit", type=int, default=40)
    sel.add_argument("--out", default="plans")
    sel.add_argument("--name", default=None)
    sel.set_defaults(func=cmd_select)

    bkp = sub.add_parser("backup", help="compress selected rollouts, verify, optionally delete")
    bkp.add_argument("--plan", required=True)
    bkp.add_argument("--archive-root", default=paths.DEFAULT_ARCHIVE_ROOT)
    bkp.add_argument("--apply", action="store_true", help="actually write archives")
    bkp.add_argument("--delete", action="store_true",
                     help="after verification, remove the session with codex delete")
    bkp.add_argument("--level", type=int, default=6)
    bkp.add_argument("--force", action="store_true", help="ignore ledger idempotency")
    bkp.set_defaults(func=cmd_backup)

    ver = sub.add_parser("verify", help="recheck archived files against the ledger")
    ver.add_argument("--archive-root", default=paths.DEFAULT_ARCHIVE_ROOT)
    ver.add_argument("--session", nargs="*")
    ver.add_argument("--limit", type=int, default=None)
    ver.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
