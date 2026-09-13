---
name: codex-session-backup
description: Back up and prune Codex session rollouts in the Codex home when reclaiming disk space or preserving transcripts before deletion. Use for cataloging sessions, choosing retention candidates, compressing rollouts into an archive, verifying archived copies, and removing sessions safely.
metadata:
  short-description: Back up and prune Codex session rollouts
---

# Codex Session Backup

Codex stores one append-only JSONL rollout per session under
~/.codex/sessions/YYYY/MM/DD/rollout-<stamp>-<uuid>.jsonl, and archived sessions live in
~/.codex/archived_sessions/. Rollouts are the transcripts; state_*.sqlite (threads.rollout_path,
archived, archived_at) and thread_history_*.sqlite (thread_turns.rollout_byte_offset) only index
them, which is why the Codex CLI must own every move or deletion.

## Tool

codex-session-backup/ in this repository: a uv project, standard library only. Run from that
directory:

    uv run --no-sync csb catalog        # writes catalog/catalog.{csv,json}
    uv run --no-sync csb select --older-than 2026-08-31 --min-size 1GiB
    uv run --no-sync csb backup --plan plans/<name>.json [--apply] [--delete]
    uv run --no-sync csb verify --limit 5

Criteria semantics, column meanings, archive layout, and restore steps: see
codex-session-backup/README.md.

Every view identifies a session by its resolved name and project path, not just an id:
display_name comes from the user-set name, else the generated title, else the stored first
message, else a bounded scan of the rollout head for the first substantial prompt
(--scan-bytes, default 1MiB). name_source records which step answered; sessions with nothing
usable show as (untitled). project_path is the session working directory with ~ for the home
prefix. Sessions forked from one another can share a headline, so quote the session id too.

## Invariants

- Never write inside the Codex home. Do not edit state_*.sqlite, and do not move or remove
  rollout files directly: a dangling threads.rollout_path and stale byte offsets break resume.
  Prune with "codex delete <session_id>". Note that "codex archive" only relocates a rollout
  into archived_sessions/ and frees no space.
- Back up before deleting, and delete only after the archive's decoded sha256 matches the
  source sha256. The tool enforces this in "backup --apply"; a mismatch must stop the run.
- Keep the dry-run default: "backup" writes nothing without --apply, and removes nothing
  without --delete as well.
- Recently active rollouts are flagged and skipped by default. Long-running Codex sessions
  append continuously, so a copy taken mid-write can end in a truncated final line; re-run the
  backup to capture the finished file.
- Archives and mirrors must include archived_sessions/ as well as sessions/, otherwise archived
  sessions look deleted.
- A selected main agent brings its whole subagent tree into the plan; --subagents tree is the
  default. Never prune a subagent on its own: it is driven by its parent, and a half-moved tree
  leaves dangling rollouts. Deepest threads are processed first, so children go before parents.
- Parentage has to be unique. thread_spawn_edges keys the child, so its edge parent is unique; a
  row whose parent is ambiguous, conflicting with agent_path, unresolved, or part of a cycle is
  excluded from tree resolution and reported as a warning. Treat any such warning as a blocker:
  the excluded subtree is not in the plan. Only main agents (thread_source user) are selected by
  default; subagents and guardian_review threads require --thread-source.
- Size criteria differ: --min-size tests one rollout file, --min-tree-size tests the whole tree
  (own rollout plus descendants), which is the one that reflects subagent sprawl. A tree
  containing a rollout touched within --recent-minutes is dropped in full with a warning, so a
  live tree is never moved or deleted.

## Authorization

Cataloging, selecting, and dry runs are read-only and safe on request. Deletion is irreversible
once a rollout is gone: show the plan (criteria, session ids, total size) and get explicit
approval immediately before running --apply --delete. Afterwards report the freed space, the
archive path, and the ledger location.

## Restore

    zstd -dc <archive-root>/YYYY-MM/<session_id>.jsonl.zst > \
      ~/.codex/sessions/YYYY/MM/DD/rollout-<stamp>-<session_id>.jsonl

The archive is byte-identical to the removed file, which is what the hash check proves.
Automatic re-registration in state_*.sqlite after a manual restore is unverified: confirm with
"codex resume", and try "codex migrate-rollouts" if the session does not appear.
