# Economics Evaluation

The economics harness implements a paired, pass-at-one comparison between a
frontier-only repair and the Frontier Commander plus local MLX execution path.
See [[src/mlx_swarm/evaluation.py]].

## Frozen suite

`mlx-swarm eval prepare PROFILE` clones the pinned BugsInPy metadata revision,
filters unsupported cases, and freezes the calibration and measured cases.

The committed profile uses seed `20260728`, an eight-project allowlist, at
least six represented projects, no more than five measured cases per project,
and balanced reference patch-size strata. It selects six calibration cases
and thirty measured cases.

`eval prepare PROFILE --preliminary` derives the current-stage decision-gate
profile without duplicating pins: two calibration cases and six measured
cases, one per project, balanced 2/2/2 across patch strata. A partial full-study
ledger with enough existing evidence can be exported with
`eval report EVALUATION_ID --preliminary`; the subset is selected by frozen
suite order, project, stratum, usage validity, and executable-oracle validity,
never by score.

The frozen suite contains only metadata needed to reproduce the study. Fixed
patch text is never copied into an arm workspace or model prompt. Every arm
starts from a history-free repository containing only the buggy tree and the
fixed revision's designated tests.

Preparation requires a clean MLX Swarm checkout and records its source commit.
Profile schema v1 preserves the original Codex contract exactly. Profile
schema v2 is adapter-neutral and pins adapter, command, command version,
provider, model, context window, explicit response-only toolsets, and phase
timeouts. Preparation and every run phase fail before an arm starts if the
resolved command version or canonical profile digest differs from the frozen
environment.
Every selected case must prove buggy-fails and fixed-passes before freezing;
failed candidates are recorded, excluded, and deterministically replaced.
If preparation is interrupted before the suite is sealed,
`eval prepare PROFILE --resume EVALUATION_ID` verifies the frozen profile,
retains prior exclusions, reuses complete case runtimes, and rebuilds only
partial cases. A sealed suite refuses preparation resume.
After selection, the BugsInPy metadata clone and project mirrors are deleted.
Fixed-revision compilation bypasses the shared ccache so fixed objects are not
retained for model execution.

## Paired arms

Each case runs sequentially with seeded arm order and an independent oracle.

Frontier Alone receives one clean buggy repository and one end-to-end frontier
phase. MLX Swarm receives one frontier plan, local worker execution with at
most two repairs, and one final frontier review only after a completed local
run. The evaluation harness can approve typed artifacts only inside disposable
case workspaces; normal sessions retain their human approval boundary.

The `hermes-completion` adapter uses the pinned Hermes installation only to
resolve provider credentials and its OpenAI-compatible endpoint. A packaged
bridge bypasses the Hermes agent loop and makes exactly one completion request
per phase, with no tool schema, no automatic model retry, JSON-object response
mode, and the profile's explicit reasoning-effort and maximum
completion-token ceilings. The prompt is passed by file instead of appearing
in process arguments. The direct arm
receives the same frozen task packet as planning and returns
`edit-manifest-v1`; the harness materializes and validates the diff. Planning
and review consume stdout as strict JSON, with at most one complete outer JSON
fence removed. Each call must produce a complete, successful usage receipt
whose provider/model and token arithmetic match the profile.
The prepared environment also freezes the resolved Python executable, Python
version, and installed MLX/MLX-LM/Hugging Face package versions. Execution
fails closed if any local-runtime field drifts before a phase starts.

Protocol version 5 constructs one deterministic task packet for both arms. It
contains the objective, failing evidence, fixed acceptance argv, frozen
repository tree, requested test excerpts, and ranked line-numbered production
windows. The ranking uses only buggy-revision test text, failure evidence, and
an optional execution trace collected by rerunning the approved verifier argv;
fixed-revision content never participates. Both arms
receive the same production write roots. Every mutating plan task must preserve
that complete root set instead of narrowing local authority.
The harness writes this authority once as immutable `pair-contract.json`, then
both arm runners verify and consume the same packet and SHA-256.

Workspace-derived plan context is validated against the buggy checkout. A
source excerpt must be an exact contiguous substring, and a mutating task that
names a file cannot rely on a rewritten or elided version of that file. This
prevents source summaries from becoming non-applicable diff context.

For the stateless Hermes adapter, protocol v5 asks the frontier for a compact
delegation blueprint instead of making it reproduce the full Plan schema and
large source excerpts. The strict blueprint cites only frozen `SOURCE` labels,
contains the evidence-backed diagnosis and exact `path`/`old`/`new` edits, and
rejects unknown labels, non-unique anchors, unsafe paths, or a manifest larger
than the local generation budget. The harness deterministically materializes
the accepted blueprint into Plan v2 with typed artifacts, gates, path authority,
verification profile, and authoritative excerpts.

Protocol-v5 evaluation plans require `edit-manifest-v1` mutating workers
with deterministic sampling, thinking disabled, and at most 800 generation
tokens. The worker returns bounded exact old/new anchors; the runtime derives
the candidate unified diff and runs the same workspace checks. This keeps the
frontier and local arms symmetric at the task-evidence and write-authority
levels while avoiding a known failure mode where the 4B model understood a
repair but hallucinated full-file diff syntax.

Both candidate diffs are scored in fresh oracle workspaces using the same
frozen verifier. A score of one means the clean executable oracle passed.
Review verdicts are retained separately and never change this score.

## Zero-frontier local replay gate

After the paired calibration phase, measured work remains locked. The operator
runs `eval replay-local EVALUATION_ID`, which reuses the accepted calibration
plans and saved prompts without a frontier call.

The replay copies the exact saved initial local prompts into fresh worktrees.
Every prompt is SHA-256 checked before execution. The replay ledger records
`frontierCalls: 0`; no planning or review adapter is reachable from this path.

The default replay strategy is `reasoning-edit`: a local reasoning pass is
stored as non-authoritative evidence, then a local editing pass emits the
strict artifact. Direct mode remains available as a control. Every frozen
calibration case must pass the independent oracle before the measured phase
can start. Invalid verifier infrastructure, a failed patch, or any missing case
keeps the measured phase locked.

The calibration gate also distinguishes worker rendering failures from
frontier plan-quality failures. Exact prompts, raw local responses, materialized
diffs, and oracle evidence make it possible to show whether the worker departed
from the task or faithfully applied a behaviorally incorrect frontier edit.
When the latter occurs, measured execution remains locked while the Commander
candidate-change specificity contract is improved; the harness does not spend
six measured frontier pairs merely to reproduce that defect.

`eval replay-local --adapted-plan-dir DIR` is a separate diagnostic path for
testing a capability-aware delegation strategy with the same local model and
zero frontier calls. It composes fresh prompts from the supplied validated
plans rather than replaying the frozen prompt. Its ledger is marked
`diagnosticOnly`; even a two-case pass always reports
`measuredEligible: false` and never mutates the frozen evaluation gate.

Verification profiles freeze the active Docker endpoint into `DOCKER_HOST`
before the executor switches to its isolated runtime `HOME`. Docker context,
daemon, pinned-container, or verifier-root failures classify the arm as
`invalid`; candidate assertion/import/test failures remain score zero.

## Evidence and economics

Raw evidence lives below `.swarm/evaluations/<evaluationId>/`. Per-arm results
are immutable and retain the timing, usage, patch, and oracle record.

Each result includes wall-clock phase timing, adapter-reported frontier usage,
local tokens, repair and model-load counts, patch digest, changed-file count,
and oracle evidence. Codex aggregates every `turn.completed` event; Hermes
validates its single JSON receipt and records `api_calls` as turns. Missing
frontier usage makes an arm measurement invalid; it is never converted to zero.

Every local generation and repair also writes an immutable attempt record with
the exact prompt, raw response, normalized response, gate result, statistics,
and digests. Historical studies without the current protocol version are
rendered as `protocol_invalid`: their rows remain useful diagnostics but cannot
support paired acceptance or token-economics conclusions.

`mlx-swarm eval report` writes a sanitized immutable export below
`benchmarks/results/<evaluationId>/` and deterministically renders the README
tables. The token-saving claim is emitted only when all thirty usage pairs are
valid, swarm completion and executable score are not lower, and the seeded 95%
bootstrap lower bound for paired frontier-token savings is positive.
Preliminary reports are explicitly labeled, always disable that claim, and
emit a decision gate that stops expansion when swarm acceptance is lower.
