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
