# codex-session-analytics

Read-only analytics for local Codex sessions. The tool joins the Codex thread database, live
rollout files, and the optional compressed session archive to report per-session names, disk
footprint, token usage by model, and estimated API cost. It also extracts one session's context
history and renders a context-message plot.

```text
state_*.sqlite / thread metadata        sessions/**/rollout-*.jsonl
              \                       /
               csa stats / csa context
              /                       \
   ledger.jsonl + *.jsonl.zst       context.json / .csv / .md / .png
```

Codex data is never modified. The only writes are the analytics cache and requested reports.

## Quick start

    cd codex-session-analytics
    uv sync

    uv run csa stats --limit 20
    uv run csa stats --out analytics/summary --plot
    uv run csa context <session-id-or-name> --out analytics/context

The first `stats` run parses live rollouts into a cache and takes a few seconds. Compressed
archives are optional:

    uv run csa index --include-archives
    uv run csa stats --limit 20

Archive parsing is incremental. The first full pass can take several minutes for a large backup;
subsequent runs skip unchanged files. Without the archive pass, archived sessions are listed from
the backup ledger and their prices are marked as estimates.

## Commands

### `csa stats`

Per-session table, totals by thread kind and model, plus optional CSV/JSON and charts.

    uv run csa stats
    uv run csa stats --limit 0 --sort tokens
    uv run csa stats --thread-source user
    uv run csa stats --live-only --since 2026-11-04
    uv run csa stats --since 2026-10-01 --project DNDSR
    uv run csa stats --out analytics/summary --plot
    uv run csa stats --json analytics/sessions.json --csv analytics/sessions.csv

Default sort is estimated cost. `--limit 0` prints every row. `--include-archives` indexes the
zstd archive before reporting. `--refresh` reparses unchanged files.

`--out DIR` writes:

- `sessions.csv`, `sessions.json` - one row per logical session
- `session_models.csv` - one row per session/model pair
- `summary.json` - totals, kind and model breakdowns
- `top-sessions.png`, `cost-by-model.png` - with `--plot`

For live subagents, `agent_path` and `agent_nickname` are included in the CSV/JSON and the
terminal name is prefixed with the nickname.

### `csa context SESSION`

Resolves a session by full id, id prefix, name fragment, or cwd fragment. All rollout segments for
that logical session are merged before extraction.

    uv run csa context 01a1aa46 --out analytics/context-01a1aa46
    uv run csa context "Cauchy-DS-back" --no-plot
    uv run csa context <id> --full-text --max-points 50000
    uv run csa context <id> --granularity turn --last-turns 20
    uv run csa context <id> --last-days 2 --granularity turn
    uv run csa context <id> --last-messages 200 --since 2026-11-10

Outputs:

- `context.json` - session metadata, per-call usage, every normalized item, compactions
- `context.csv` - one row per context item
- `context_turns.csv` - one row per turn (items, calls, input, estimated, cumulative, cost)
- `context.md` - human-readable summary and largest items
- `context.png` - two-panel plot (skip with `--no-plot`)

`--granularity turn` snaps the view and plot to `turn_id` groups. `--last-turns N` keeps the
last N turns; `--last-messages N` keeps the last N items; `--last-days`/`--last-hours` use the
session's final timestamp as the reference. `--since`/`--until` accept `YYYY-MM-DD` or an ISO
timestamp. Filters also restrict the usage calls and cost totals in the payload and subtitle, so
the selected slice is internally consistent. The unfiltered lifetime totals remain available in
`context.json`.

The top panel plots actual input tokens and cached input per API call, with the context-window
line, model changes, and large context drops marked. The bottom panel plots estimated tokens per
context item, colored by kind, with a cumulative context estimate on the right axis. Compactions
reset the cumulative estimate to the replacement history's estimated size.

`--max-chars` caps each item's text in JSON/CSV (default 1000); `--full-text` removes the cap.
Token estimates are always computed before truncation. When more than `--max-points` items exist,
the lower panel bins them by item index so the figure stays readable.

### `csa index`

Builds or refreshes the parsed-usage cache.

    uv run csa index
    uv run csa index --include-archives

The cache defaults to `~/.cache/codex-session-analytics/index.sqlite`. Use `--cache PATH` to put
it elsewhere. A cache entry is reused only when the source path, size, and mtime match.

### `csa prices`

Shows the effective price table and its sources.

    uv run csa prices
    uv run csa prices --json analytics/prices.json

Pass `--prices custom.json` to any command to merge local overrides over the built-in table. A
minimal override for one model is:

```json
{
  "models": {
    "my-model": {
      "input": 1.0,
      "cached_input": 0.1,
      "cache_write": 1.25,
      "output": 5.0
    }
  }
}
```

## How the numbers are built

Codex records two different token scopes in a rollout:

- `usage` on each `token_usage_record` is the usage of that API call; summing these gives the
  **retained detail** for the rollout file.
- `thread_token_usage` on the same record is the thread's cumulative lifetime usage. The final
  record in a segment is the authoritative session total, including history that may no longer be
  in the current rollout file.

These are cumulative **prompt** tokens across API calls, not unique conversation text. A long
agent loop resends a large context on every call, so billions of tokens can be real while the
transcript is only tens of megabytes.

`csa` sums the own-usage deltas across a session's segments as the session total. It costs the
retained calls exactly, then costs the difference between lifetime and retained usage at the
dominant model's rates. The table marks those costs with `~`. This matters for resumed or
compacted sessions: the retained detail can be a small fraction of the lifetime total.

Subagent and forked rollouts do **not** start their counter at zero. The first
`total_token_usage`/`thread_token_usage` contains the parent's cumulative baseline. `csa`
subtracts that baseline and adds the first call back, so a subagent that starts at 13.31B and ends
at 13.83B is reported as roughly 0.52B of its own usage, not 13.83B. The raw final value is kept as
`raw_thread_usage` in the parser for diagnosis. This is why `threads.tokens_used` can look much
larger than the tool's lifetime figure for a subagent.

For sessions with no token records at all, `threads.tokens_used` is shown as the lifetime total and
costed with a model-average input/output/cache mix learned from the parsed sessions. This is an
estimate, not a recorded usage breakdown.

Cost per call uses the documented formula:

```text
uncached_input = input_tokens - cached_input_tokens - cache_write_input_tokens

cost = uncached_input * input_rate
     + cached_input * cached_rate
     + cache_write_input * cache_write_rate
     + output_tokens * output_rate
```

Rates are USD per 1M tokens. `output_tokens` already contains reasoning tokens, so
`reasoning_output_tokens` is reported but not charged again.

### Price assumptions

The built-in table is dated **2026-11-11** and comes from:

- OpenAI: <https://developers.openai.com/api/docs/pricing>
- DeepSeek: <https://api-docs.deepseek.com/quick_start/pricing>

OpenAI requests above 272K input tokens use the documented long-context policy: 2x input/cache
rates and 1.5x output rates for the full request. GPT-5.6 Sol/Terra/Luna, GPT-6 Astra/Sol/Luna,
and GPT-6.1 Sol/Terra are included. Cache writes use the published rate where available.

DeepSeek Flash and V4 Pro use peak/off-peak rates. Peak hours in the table are 01:00-04:00 and
06:00-10:00 UTC on weekdays. Chinese public holidays are not modeled, so a holiday request will be
priced as peak when it was actually off-peak. The per-call timestamp decides the rate; estimated
remainders use the latest retained timestamp.

`ollama/`, `vllm/`, `llama.cpp/`, and `local/` models are treated as zero API charge. Other
unknown models are reported with their tokens unpriced rather than assigned a guessed rate.

Prices change. Use `--prices` for current account/provider rates instead of editing the package.

## Data sources and scope

- `state_*.sqlite` - thread name, title, first message, cwd, model, lifetime `tokens_used`, kind,
  timestamps, and rollout path.
- `sessions/**/rollout-*.jsonl` and `archived_sessions/**` - the live transcript segments.
- `<archive-root>/ledger.jsonl` plus `*.jsonl.zst` - optional compressed backups. The default root
  is `~/ssd1/bkp/codex_sessions`; override with `--archive-root` or
  `CODEX_SESSION_ARCHIVE`.
- `--codex-home` overrides `$CODEX_HOME` / `~/.codex`.

The identity rule needs care: Codex puts the owning/root session id in `session_meta.session_id`,
while `session_meta.id` is the thread's own id. A subagent rollout therefore has the root in
`session_id` and its own thread in `id`. The filename UUID can differ from both. `csa` groups by
`session_meta.id`, falling back to `session_id` only when `id` is absent, then sums disk usage
across segments and uses the maximum lifetime usage rather than summing overlapping lifetime
totals.

Missing rollout files are still listed when the thread database has the session. Archive-only
sessions appear from the backup ledger. Sessions with neither transcript nor archive show their
database token total but no disk bytes. When an archive has no useful ledger title, the first
substantial user message in the rollout head supplies the display name. Rows with no usable
headline show `(untitled) · <project> · <short-id>` so they remain distinguishable.

## Limitations

- Item token counts in the context plot are a deterministic character heuristic (CJK ~1 token,
  other text ~4 characters/token), not a tokenizer. Actual call context comes from recorded usage.
- Encrypted reasoning has no readable text; its item is marked and estimated at zero, while its
  billed output stays in the recorded output-token total.
- Images are counted as a fixed 1024-token estimate because the rollout does not carry the
  provider's vision-token accounting.
- The database-only estimate cannot recover a model switch or a real input/output split. Prefer
  `csa index --include-archives` for archived sessions.
- `threads.tokens_used` is a lifetime aggregate and can exceed the usage retained in a rollout;
  do not compare it directly with the sum of per-call `usage` records.

The investigation of the 2026-11-11 token anomaly, including the inherited-counter correction
and the old archived call-volume outliers, is recorded in `notes/2026-11-11-token-anomaly.md`.

## Development

    uv sync
    uv run --no-sync python -m unittest discover -s tests -t .
    uv run python -m compileall -q src

The package uses only the Python standard library plus matplotlib for figures. `zstd` is required
only when reading compressed archives.
