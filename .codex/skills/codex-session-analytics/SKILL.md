---
name: codex-session-analytics
description: Extract per-session Codex token, cost, disk, and context-history analytics from local rollouts and the optional compressed archive. Use for session inventories, model/price breakdowns, and context-message plots.
metadata:
  short-description: Analyze Codex session tokens, cost, and context
---

# Codex Session Analytics

`codex-session-analytics/` in this repository is a uv project with a `csa` CLI. It reads
`state_*.sqlite`, live rollouts under `~/.codex/sessions/`, and optionally the zstd backup under
`~/ssd1/bkp/codex_sessions/`. It never writes inside the Codex home.

    cd codex-session-analytics
    uv sync
    uv run csa stats --limit 20
    uv run csa stats --out analytics/summary --plot
    uv run csa context <session-id-or-name> --out analytics/context
    uv run csa context <session-id-or-name> --granularity turn --last-turns 20
    uv run csa economics <session-id-or-name> --last-hours 24
    uv run csa index --include-archives

## Reading the output

- A logical session can span several rollout files. Group by `session_meta.id`, not
  `session_meta.session_id`: subagent rollouts carry the root/owner id in `session_id` and their
  own thread id in `id`. The filename UUID may differ from both. Sum disk bytes across segments,
  but use the maximum `thread_token_usage` lifetime total; summing lifetime totals double-counts
  history.
- `retained_tokens` is the sum of per-call `usage` in the files that still exist.
  `tokens` is the thread's lifetime own-usage after subtracting any inherited baseline. The
  difference is costed at the dominant model's rates and marked `~`.
- Subagent and forked rollouts inherit the parent's cumulative counter. Subtract the first
  `thread_token_usage` baseline (and add the first call back) before calling it the subagent's
  usage; otherwise a 13.8B final counter looks like 13.8B of new work when only about 0.5B is.
- Tokens are cumulative prompt usage across calls, not unique text; the same context is billed
  again on every call. A 1B-token mechanical subagent can have a transcript of only a few MB.
- Sessions with no rollout use `threads.tokens_used` and a model-average split; label that
  `model_average`, not recorded usage.
- A model with no price entry leaves its tokens unpriced. Do not invent a rate. Local
  `ollama/`/`vllm/` providers are zero API charge by policy in the price table.
- The context plot's item tokens are a character/CJK heuristic. The top panel's context sizes
  come from recorded usage and are the authoritative part.
- `--granularity turn` snaps to turn ids; `--last-turns`, `--last-messages`, `--last-days`,
  `--last-hours`, `--since`, and `--until` restrict the extracted slice and its plot.
- `--no-long-context-surcharge` on `stats`/`context`/`prices` uses GPT base rates. `economics`
  reports both actual modes and ranks compact lengths from average post-compaction context,
  per-call growth, and the observed summary call immediately before each `compacted` event; it
  disables the GPT surcharge. Default candidates are 272k, 300k, 380k, 500k, 800k, 1m.

## Price table

The built-in rates are dated 2026-11-11. OpenAI long-context pricing applies above 272K input
tokens; DeepSeek Flash/V4 Pro have UTC weekday peak hours. Pass `--prices custom.json` for
current or account-specific rates. See `codex-session-analytics/README.md` for the formula and
sources.

## Validation

Run tests with:

    uv run --no-sync python -m unittest discover -s tests -t .

For a spot check, compare one session's `tokens` with the final `thread_token_usage` in its
rollout, and verify `retained_tokens` against the sum of `usage.total_tokens` fields. If a
continuation file lacks the original session id in its filename, inspect `session_meta` before
concluding it is a separate session.
