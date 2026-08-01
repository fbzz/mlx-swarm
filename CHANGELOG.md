# Changelog

All notable changes to MLX Swarm are documented in this file.

## [0.5.1] - 2026-08-01

### Added

- `claude-cli` evaluation frontier adapter: a packaged one-call bridge over
  the authenticated Claude Code CLI (single turn, all workspace tools
  disallowed, strict usage receipts mapped from the headless JSON envelope
  with full cache accounting, keychain identity derived under the stripped
  harness environment, error envelopes surfaced on failure).
- Frontier transport cache (fair-evaluation protocol 16): every
  receipt-valid completion freezes its response and receipt, keyed by
  adapter/model pins and a timing-normalized prompt digest; identical calls
  replay at zero cost, and `eval seed-cache` freezes a prior evaluation's
  already-purchased responses. Completion ceilings are deliberately outside
  the key: completed responses are ceiling-independent by construction.
- Bounded contract-repair retry: a receipt-valid frontier response that
  fails schema or materialization validation earns exactly one corrective
  call carrying the validator's errors, with both receipts reported as
  separate usage phases; infrastructure failures never retry.
- `mlx-swarm stats`: aggregate operational statistics (session and task
  status, execution-mode mix, first-pass gate acceptance, blocked share,
  repairs, escalations, local token totals) from durable session ledgers.
- GLM 5.2 planner benchmark protocol, results, and session-evidence audit
  under `benchmarks/`; hermes-completion GLM evaluation adapter merged from
  its development branch (runtime-witness task packets, delegation
  blueprints, protocol lines 13-16).
- Sonnet evaluation profile (`benchmarks/bugsinpy-sonnet/`) with hard-case
  limits (64k completion ceiling; 1500s planning, 3900s arm budgets).

### Changed

- Gate-sizing guidance raises `maxCharacters` to at least five characters
  per expected output token with explicit headroom language (a knife-edge
  gate rejected a correct artifact by 3% in the GLM benchmark).
- Prose-wrapped delegation blueprints extract their first complete embedded
  JSON object deterministically; materialized worker tasks clamp to the
  800-token paired-arm bound (protocol 15).
- Evaluation synthetic approvals bind to the snapshot's sealed execution
  policy (contract-v2 fields), unblocking local arms.

## [0.5.0] - 2026-07-31

### Added

- Claude Code installation for the shared `mlx-swarm-commander` Agent Skill
  through `mlx-swarm skill install --host claude`, including
  `CLAUDE_CONFIG_DIR` discovery.
- Provider-neutral structured planning and review handoffs with host-specific
  receipt provenance for Claude Code, Codex, and compatible Agent Skills hosts.
- Plan-import preflight rejecting a deterministic-edit task whose serialized
  `deterministicEdits` exceed its own `gate.maxCharacters`, turning a
  guaranteed mid-run cascade failure into an immediate validation error.
- Smart repair: a truncated local generation with remaining repair budget
  retries once with a doubled bounded ceiling (recorded as
  `escalatedMaxTokens`), and a repair dispatch that would deterministically
  replay a recorded prior attempt is skipped without spending the call.
- `PlanValidationError` accumulating every plan validation error into one
  report instead of stopping at the first.
- Bounded commander re-import: an invalid plan import leaves the claim open
  for up to three total attempts with per-attempt receipts and raw evidence;
  identical invalid replays do not spend an attempt; the request seals only
  when the budget is exhausted.
- `run PLAN --approve-preview`: one-step CLI approval that computes the
  execution preview in-process, prints the bound contract, and records both
  digests with an `approvalShortcut` provenance marker.
- Skill guidance for DAG shape (wide and shallow, dependency blast radius),
  delegation upper bounds, content-based output sizing, and
  `gate.maxCharacters` selection, mirrored in the commander plan prompt.
- Sharded safetensors checkpoints (`model-*-of-*.safetensors` with an index)
  are accepted by model resolution, unblocking checkpoints above ~10 GB.
- New reference worker profile: `mlx-community/Qwen3.6-35B-A3B-4bit`,
  maintainer-calibrated 4/4 at first pass including two autonomous
  single-file bug diagnoses, measured at 78.5 tok/s single-worker,
  126.5 tok/s aggregate at width two, and 160 tok/s at width four within
  20.2 GB peak memory. The shipped example stays `exact-edit` with
  `unmeasured` calibration; reproducing calibration is the gate for
  `bounded-implementation` delegation.
- GLM 5.2 planner benchmark protocol under
  `benchmarks/glm52-planner-benchmark.md`.

### Changed

- Skill installation now requires `--host claude` or `--host codex`.
- Cockpit and public documentation use frontier-agent language instead of
  presenting Codex as the product boundary.
- Planning and review imports can no longer override the host adapter sealed by
  their claim.
- Skill replacement refuses symlink destinations before resolving or removing
  the leaf path.
- `--max-repair` (CLI run/resume) and the cockpit repair cap now default to 1;
  resumed sessions keep their stored cap, and plan tasks that omit
  `maxRepairAttempts` still default to zero repair.
- Token-limit detection reports an exact `hitTokenLimit` plus a
  `suspectedTokenLimit` within a 16-token margin; the executor treats the
  margin-based suspicion as truncation only for gate-failing output, so a
  complete artifact near its ceiling is never failed by the margin.
- A recorded `escalatedMaxTokens` now survives resume and plan-derived task
  reconstruction, keeping repair escalation monotonic across restarts.
- A successful corrected re-import clears the stale `plan.error.json`, so an
  accepted plan no longer surfaces the previous attempt's validation error in
  the cockpit; a locally unreadable response file spends no import attempt
  and leaves the claim releasable; replay idempotency matches bytes against
  every recorded invalid attempt, not only the latest.
- Deterministic-edit runtime gate failures name the actual violations instead
  of a generic structural-validation message.
- Fair-evaluation protocol version is now 5; summaries recorded under earlier
  executor repair semantics are flagged for rerun by the protocol audit.
- Previously loadable plans that embedded deterministic edits larger than
  their own gate now fail at import/approval; such plans were guaranteed to
  fail at runtime.

## [0.4.0] - 2026-07-30

### Added

- Direct routing for simple one- or two-file cosmetic and mechanical changes,
  avoiding Swarm overhead when decomposition and governance add no value.
- Compact, digest-bound `frontier-review-input.json` packets for final frontier
  review while preserving the full `frontier-result.json` audit record.
- Incremental commander revisions that carry validated completed work from one
  retained, clean isolated worktree and replan only the unfinished subgraph.
- Cockpit and CLI support for selecting a predecessor run with `revisionOf` /
  `--revision-of`.
- Durable one-successor enforcement, fresh approval receipts, predecessor
  evidence, and retry-ancestry checks for revision runs.

### Changed

- The default local execution profile now uses two concurrent agents, a
  49,152-token aggregate prompt ceiling, and a 2,048-token model generation
  ceiling.
- Workspace execution contracts advance to schema version 3 for revision
  authority and inherited-base binding.
- Exact-edit tasks default to zero autonomous repair attempts and are split on
  truncation instead of consuming repeated local generations.
- Final review receives bounded patch and report excerpts instead of the
  complete planning and execution payload.

### Security and reliability

- Final-review claims bind both the compact review packet and its source result
  digest, detecting changes to either artifact.
- Revision creation excludes active or partially reviewed predecessors and
  validates retained branch ancestry, worktree cleanliness, carried artifacts,
  and execution authority.
- Failed commander-evidence attachment is cleaned up and cannot be resumed as a
  valid launched session.

[0.4.0]: https://github.com/fbzz/mlx-swarm/releases/tag/v0.4.0
