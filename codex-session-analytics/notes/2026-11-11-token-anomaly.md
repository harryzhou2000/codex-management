# Token anomaly investigation — 2026-11-11

Scope: local live rollouts plus the compressed archive, filtering activity from **2026-11-04**
(the last seven days). The evidence files are `analytics/recent-7d-trees.csv` and
`analytics/recent-7d-subagents.csv`, generated with `csa stats --since 2026-11-04` semantics.

## Finding 1: subagent counters inherit the parent baseline

Subagent and forked rollouts do not start their cumulative counter at zero. The first token event
already contains the parent's total. Treating the final `thread_token_usage` as the child's own
usage inflated subagents by orders of magnitude.

Example `01a1aa46-ce2b-71a2-9c00-c9a25cb6d8a3` (user session with inherited history):

- raw final counter: 8,153,503,072
- inherited baseline: 6,249,678,411
- own usage after correction: 1,904,105,207

The corrected total is confirmed by the retained per-call usage. The parser now subtracts the
initial baseline, adds the first call back, and clamps to retained usage. See
`src/codex_session_analytics/rollout.py`.

## Finding 2: the 13B rows are archived August call-volume outliers

The overall table is dominated by `019f74a2-de58-7542-abb0-31c63b12915e` (14.29B tokens,
100,649 calls) and several subagents under that root with roughly 13B tokens and 91k-92.5k calls,
for example:

- `019ffcd2-1ff2-7240-94ff-37ba91452e2e` — Harvey the 2nd, `/root/digest_layout_audit`,
  13.19B tokens, 92,551 calls
- `019ffcd3-058b-7413-9467-d8010d414710` — Ramanujan the 2nd, `/root/digest_test_audit`,
  13.19B tokens, 92,549 calls
- `019ffcb9-ae18-7d72-84be-1aa9ffa0b357` — Nietzsche the 2nd, `/root/registration_audit`,
  13.18B tokens, 92,439 calls

These are August 2026 records, not last-seven-day activity. Their timestamps collapse into a few
minutes (for example 2026-08-13 20:30:58–20:35:03), while token usage increases across 92k calls.
Treat the token sequence as recorded cumulative prompt usage, but do not use those timestamps as a
call-rate measurement. They are the reason the unfiltered overall top looks extreme.

## Finding 3: recent activity, after correction

- 55 sessions, 17.12B tokens, about $3,625 estimated API cost.
- 22 user-started sessions: 13.7B tokens, about $3,314.
- 33 subagents: 3.4B tokens, about $311.

Top recent root trees:

| root | tree tokens | tree cost | top subagent |
|---|---:|---:|---|
| `01a186d4` DNDSR Cauchy-DS-back | 9.09B | $1,851 | Locke, `/root/pmg_mechanical`, 1.14B |
| `01a1aa46` DNDSR Cauchy-DS-back-audit-fix | 2.51B | $1,142 | Aristotle, `/root/plan56_build`, 213M |
| `01a1974f` RRI: continue-DS | 2.43B | $12 | Helmholtz, `/root/rri_cs_runner_deepseek`, 312M |
| `01a142f4` Limited VR experiments: Orion Case | 1.79B | $308 | Harvey, `/root/orion_refresh_1030`, 437M |

Top recent subagents by own usage: Locke 1.139B (4,645 calls), Epicurus 667M (4,285), Harvey 437M
(2,951), Leibniz 359M (1,565), Helmholtz 312M. These are cumulative prompt tokens; the same
context is re-billed on every call. Their costs are small because most ran cheap gpt-6.1-terra or
gpt-6-luna models with high cache reuse.

## Limits

- Database-only rows (`threads.tokens_used` with no rollout) cannot be baseline-corrected; they
  remain marked as DB estimates.
- Prices are the built-in 2026-11-11 API-rate table; archived DeepSeek timestamps may be collapsed,
  and Chinese holidays are not modeled.
- The archived 92k-call subagents should be reviewed separately for a retry/polling loop before
  being treated as a normal workload.
