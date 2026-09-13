# codex-management

Local tooling for keeping Codex data manageable.

- codex-session-backup/ - catalog Codex sessions, choose what to back up or prune, compress
  rollouts into an archive, verify hashes, then remove sessions through the Codex CLI. Its
  README documents the safety model, criteria semantics, and restore steps.
- .codex/skills/codex-session-backup/SKILL.md - project-local skill that tells a Codex agent how
  to run that workflow without corrupting Codex state.

## Quick start

    cd codex-session-backup
    uv sync
    uv run csb catalog                                                  # catalog/catalog.{csv,json}
    uv run csb select --older-than 2026-08-31 --min-size 1GiB           # plans/plan.{csv,json}
    uv run csb backup --plan plans/plan-2026-08-31-1gib.json            # dry run
    uv run csb backup --plan plans/plan-2026-08-31-1gib.json --apply    # compress + verify

Standard library only. zstd is the one external binary the backup step needs.

Tree-resolution guards (unique parentage, no double attachment, excluded ambiguous rows):

    cd codex-session-backup && uv run --no-sync python -m unittest discover -s tests -t .

## Layout notes

The skill exists twice: .codex/skills/codex-session-backup/SKILL.md is the discoverable copy
that Codex loads, and codex-session-backup/skill/SKILL.md is the source of truth kept next to
the tool. Keep them in sync when editing.

Generated catalogs and plans are gitignored: they contain session titles, project paths, and
excerpts of user messages.
