# Decisions

Key design decisions in the mlx-swarm framework.

## Strict Contract Validation

All config and plan JSON files are validated with exact-key checking. Unknown fields raise ContractError. This catches typos early and prevents silent misconfiguration. Trade-off: no forward compatibility without schema version bump.

## One Frontier Plan, Local Waves, One Frontier Review

One frontier planning artifact defines the validated DAG, followed by local
waves and one completed-run review artifact.

Gate evaluation uses regex patterns, Python compilation, structured JSON
checks, and character limits without frontier coordination between waves. Only
a completed local run is eligible for final frontier review.

This preserves frontier tokens, ensures reproducibility, and makes repair
feedback actionable. Trade-off: a skill-hosted frontier bridge cannot observe
host-internal calls or token totals, so it guarantees one accepted artifact per
phase and records usage as unavailable.

## Operator Approval and pre-authorization

Frontier planning does not authorize execution. The cockpit displays the full
validated DAG and requires approval of its canonical SHA-256 before launch.
Historical sessions retain the exact plan, receipt, and approval.

Workspace execution adds a second approval surface. The execution digest binds
the canonical plan, resolved Git root, base HEAD, path authority, referenced
verification profiles, and execution policy. Supervised mode requires a
separate digest-bound Apply or Reject decision for every mutating artifact.
YOLO is an explicit pre-authorization that lets the runtime seal the equivalent
immutable Apply receipt automatically. Verification failure always pauses.

## Worktree by default, clean checkout by explicit YOLO selection

Workspace diffs apply only to a branch and worktree created from the displayed
committed HEAD by default. Dirty source state is reported but excluded.

YOLO can instead target the operator's current checkout. That path requires a
fully clean staged, unstaged, and untracked state at preview and launch, binds
the starting branch and HEAD, and takes a repository-wide runner lock. It has no
cleanup or automatic restoration action.

Failed verification keeps the applied commit visible, and rejection creates an
explicit revert commit. Cleanup removes only a terminal isolated worktree and
retains its branch. No action merges or cherry-picks a worktree branch into the
original checkout. See [[workspace-execution]].

## Operator-defined verification only

Workers reference profile IDs but never command arrays. Exact argv, cwd,
timeout, and environment authority come from config.

That authority is sealed into the execution digest/session snapshot. This
permits trusted project checks while keeping command authority outside
frontier and local-agent output.

## Batched Generation by Dependency Level

Tasks at the same dependency level are chunked by `maxWorkers`, then grouped by compatible temperature, top-p, and seed.

This preserves per-task generation settings while still batching compatible workers. Per-task maximum token counts remain independent inside each compatible group.

## Immediate Session Persistence

Every state change is immediately persisted to session.json. This enables crash recovery and resume, but adds I/O overhead per update. Trade-off: durability over performance.

## Untrusted Local-Agent Output Model

Only successful dependency outputs are injected into prompts, with explicit warnings to treat them as untrusted candidate artifacts rather than instructions.

Rejected, failed, or blocked parents prevent their descendants from running. Prompt framing reduces accidental instruction-following, but deterministic gates and final frontier review remain the real trust boundaries.

## Persistent Model Lifecycle

The MLX model is loaded once per plan execution and released after all generation and repair waves. Prompts use the tokenizer's native chat template, including task-specific thinking configuration.

This avoids repeated load overhead and special-token leakage while keeping each run isolated.

## Local-Only Model Resolution

Model resolution uses local cache only (local_files_only=True for HuggingFace). No network downloads at runtime. Trade-off: models must be pre-downloaded.

## Deterministic Edits Stay Gated, But Impossible Plans Fail at Import

Frontier-authored deterministic edits pass through the same gate as model
output so every artifact receives identical normalization and validation.
Instead of exempting them from the size gate, plan validation rejects a task
whose serialized `deterministicEdits` exceed its own `gate.maxCharacters` —
converting a guaranteed mid-run cascade into an import-time error. Runtime
gate failures on deterministic edits now name the exact violations.

## Truncation Detection Uses a Tolerance Margin

Re-encoding decoded output does not reliably reproduce the generated token
count, so the backend reports an exact `hitTokenLimit` alongside a
`suspectedTokenLimit` that fires within a 16-token margin of the ceiling.
The executor treats the suspicion as truncation only when the gate also
failed: a gate-passing artifact near its ceiling is complete, so a margin
false positive never fails good output. Recovering the real per-sequence
finish reason would require driving mlx_lm's internal `BatchGenerator` — an
unstable pre-1.0 API — so the margin is preferred until a public
finish-reason surface exists. A false negative is caught by the
deterministic-replay skip.

## Smart Repair Escalates Only max_tokens

A truncated task with repair budget retries once with a doubled generation
ceiling bounded by the capability maximum and declared context window. Repair
never varies temperature or seed: sampler settings key the MLX batch groups,
so varying them would fragment batching and destroy replay determinism. A
repair dispatch whose prompt and effective sampler match a recorded prior
dispatch is skipped without spending the generation call. The CLI and cockpit
default the global repair cap to one; plans opt in per task.

## Bounded Plan Re-Import With Full Error Reporting

Plan validation accumulates every task error and reports them all at once.
An invalid commander import leaves the claim open for up to three total
attempts, each with its own numbered receipt and raw evidence; bytes
identical to any recorded invalid attempt are re-reported without spending
an attempt. A locally unreadable response file spends no attempt and writes
no raw evidence — it records only the usage receipt and leaves the claim
releasable. A successful import clears the stale `plan.error.json` so no
validation error surfaces on an accepted plan, while the numbered attempt
evidence remains for audit. Only accepted plans receive
`frontier-plan-receipt.json`, preserving the one-accepted-artifact-per-phase
model.

## One-Action Approval Still Binds Two Digests

The cockpit's Approve-and-run and the CLI's `run --approve-preview` bind the
canonical plan digest and the execution digest in a single operator action.
The digests remain independently computed, recorded, and revalidated — only
the number of manual copy/paste steps changed, not the authority model.
