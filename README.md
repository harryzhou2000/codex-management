# codex-management

Local tooling for keeping Codex data manageable.

- codex-app-server/ - inspect, purge, start, or replace the managed Codex app-server with an
  explicit proxied or direct network environment while preserving session and configuration data.
- codex-session-backup/ - catalog Codex sessions, choose what to back up or prune, compress
  rollouts into an archive, verify hashes, then remove sessions through the Codex CLI. Its
  README documents the safety model, criteria semantics, and restore steps.
- codex-session-analytics/ - per-session names, disk footprint, token usage by model, estimated
  API cost, and context-history extraction with a context-message plot. Reads live rollouts and
  the optional compressed archive without modifying Codex data.
- .codex/skills/codex-session-backup/SKILL.md - project-local skill that tells a Codex agent how
  to run that workflow without corrupting Codex state.
- .codex/skills/codex-session-analytics/SKILL.md - project-local skill for reading the analytics
  output and validating token/lifetime totals.

## Quick start

    cd codex-session-backup
    uv sync
    uv run csb catalog                                                  # catalog/catalog.{csv,json}
    uv run csb select --older-than 2026-08-31 --min-size 1GiB           # plans/plan.{csv,json}
    uv run csb backup --plan plans/plan-2026-08-31-1gib.json            # dry run
    uv run csb backup --plan plans/plan-2026-08-31-1gib.json --apply    # compress + verify

Standard library only. zstd is the one external binary the backup step needs.

Session analytics:

    cd codex-session-analytics
    uv sync
    uv run csa stats --limit 20
    uv run csa stats --out analytics/summary --plot
    uv run csa context <session-id-or-name> --out analytics/context

Tree-resolution guards (unique parentage, no double attachment, excluded ambiguous rows):

    cd codex-session-backup && uv run --no-sync python -m unittest discover -s tests -t .

## Layout notes

Each project skill exists twice: `.codex/skills/<name>/SKILL.md` is the discoverable copy that
Codex loads, and `<project>/skill/SKILL.md` is the source of truth kept next to the tool. Keep
each pair in sync when editing.

Generated catalogs and plans are gitignored: they contain session titles, project paths, and
excerpts of user messages.
