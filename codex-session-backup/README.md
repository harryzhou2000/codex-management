# codex-session-backup

Catalog Codex sessions, pick what to back up and prune, and back rollouts up with compression.
Stdlib only, driven through a small CLI named "csb".

## Safety model

- Read-only on Codex data: databases are opened with SQLite mode=ro, and nothing under the
  Codex home is ever written, moved, or chmod-ed by this tool.
- The Codex databases are never edited. Deletion is delegated to "codex delete <session_id>",
  the only component that knows how to remove a session and keep state_*.sqlite consistent.
  Deleting rollout files by hand leaves threads.rollout_path and the thread_history byte
  offsets pointing at nothing.
- Verification precedes deletion: sha256(source) must equal sha256(decompressed archive)
  before a session becomes eligible for removal.
- Dry run by default: "backup" writes nothing without --apply, and removes nothing without
  --delete as well.
- ledger.jsonl in the archive root records every verified backup, so re-running a plan is
  idempotent (a session is skipped while its recorded source size still matches).

## Usage

    uv run csb catalog                      # -> catalog/catalog.csv + catalog/catalog.json
    uv run csb select                       # filtered table + plans/plan.{csv,json}
    uv run csb backup --plan plans/plan.json                    # dry run
    uv run csb backup --plan plans/plan.json --apply            # compress + verify, keep source
    uv run csb backup --plan plans/plan.json --apply --delete    # also prune via codex delete
    uv run csb verify --limit 5                                  # recheck archives vs the ledger

### Catalog columns

session_id, display_name, name_source, name, title, archived, tree (sessions|archived_sessions),
exists, orphan, size_bytes, size_human, last_activity, recently_active, rollout_path,
project_path, cwd, created, updated, turns, tokens_used, cli_version, git_branch, model,
first_user_message

display_name resolves in a fixed order and name_source records which step answered, so an
untitled session is never silently blank:

1. name - the session name set by the user (only 15 of 1545 sessions here).
2. title - the generated title, accepted when it is not a placeholder such as ".".
3. first_message - the first user message as stored in the database.
4. rollout_head - the first substantial prompt inside the first --scan-bytes of the rollout
   (default 1MiB), skipping injected context blocks (AGENTS.md, permissions, environment,
   internal goal context) and nudges such as "." or "continue".
5. none - nothing usable; the CLI shows (untitled) and the project path plus date identify it.

project_path is the session working directory with the home prefix shortened to "~"; cwd keeps
the raw path. Identical headlines still happen for forked sessions, which is why the session id
stays in every view.

last_activity is the rollout file mtime, falling back to the database updated_at when the file
is gone. recently_active marks rollouts touched inside --recent-minutes (default 15); those are
skipped by default so a live session is never selected.

turns comes from thread_turns and is 0 for sessions whose history has not been materialized in
thread_history_*.sqlite, so read it as a signal rather than a hard count.

### Criteria

select ANDs its filters; the defaults encode the current retention rule:

    uv run csb select --older-than 2026-08-31 --min-size 1GiB

- --older-than: last activity strictly before that local date (YYYY-MM-DD or YYYY/MM/DD).
- --min-size: rollout size strictly greater than the given size; SI and IEC units both accepted
  (1GB = 1000^3, 1GiB = 1024^3).
- --min-turns, --exclude-archived, --exclude-orphans, --include-active

The select table reports session id, name/title, size, last activity, project path, and tokens;
the plan JSON/CSV carry every catalog column.

## Archive layout

    <archive-root>/                  # default ~/ssd1/bkp/codex_sessions
      YYYY-MM/<session_id>.jsonl.zst
      ledger.jsonl

Each ledger record holds the source path, size, mtime, source sha256, archive path and size,
the decoded sha256, and the metadata needed to identify the session later (title, cwd, activity).

## Restore

    zstd -dc <archive-root>/YYYY-MM/<session_id>.jsonl.zst \
      > ~/.codex/sessions/YYYY/MM/DD/rollout-<stamp>-<session_id>.jsonl

The archived file is byte-identical to what was removed; that is what the hash check proves.
Putting the file back restores the transcript. Whether Codex re-registers it in state_*.sqlite
automatically is unverified, so confirm with "codex resume" and fall back to
"codex migrate-rollouts" if the session does not appear.
