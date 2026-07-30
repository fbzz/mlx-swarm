# Commander

Frontier Commander is the provider-neutral boundary around one planning
artifact and one final-review artifact. It is implemented in
[[src/mlx_swarm/commander.py]].

## Request Lifecycle

The cockpit or CLI creates a request below
`<artifacts>/_commander/requests/<requestId>`.

For schema v1, the config directory is the approved inspection root. For
schema v2, MLX Swarm resolves and displays the nearest Git top-level above the
config directory. A deterministic prompt describes the objective, constraints,
Plan schema v3, typed artifact fields, configured write roots/profile IDs, and
local runtime limits. It also contains a strict worker capability contract:
model parameter scale, context window, prompt and generation ceilings,
specialization, execution mode, observed strengths/limitations, calibration
receipt, and maximum safe delegation level. This is explicitly distinct from
the batch worker count.

At `exact-edit`, the frontier retains causal diagnosis and edit design. Each
mutating task names an exact file and symbol, supplies exact source anchors,
and specifies one mechanical old-to-new transformation. The local model is
not asked to discover APIs, select a repair, or recover missing context.
The frontier also assigns task-owned `contextRefs`, freezes an
`interfaceContract`, predicts `expectedOutputTokens`, selects local-agent or
deterministic execution, separates disjoint path ownership into parallel
siblings, and declares final integration verification.

The planning response must include `context.diagnosis`. During that same
frontier call, the commander traces the observed failure through exact
authoritative source excerpts or an already-approved verification receipt,
states one falsifiable causal hypothesis, records its concrete validation
evidence, and names the condition that would disprove it. Unsupported
speculation is rejected at import and seals that request as invalid.

Commander imports additionally require
`context.diagnosis.changeValidation`. It records the literal candidate
behavior, its predicted effect on the failing path, at least one named passing
or non-target control that must remain correct, the minimality evidence for the
chosen discriminator, and the authoritative source labels supporting those
claims. A source trace of the current implementation alone is insufficient:
the same planning call must simulate the proposed change on both paths. For
`exact-edit`, the candidate description must agree with the old-to-new
transformation delegated to the worker. The base [[Plans]] loader keeps this
field optional so historical non-commander plans remain readable.

Planning transitions through `open → claimed → accepted|invalid`. Claim files
use exclusive creation so concurrent skill or file-import invocations cannot
both occupy the response slot. Claims and immutable response artifacts publish
atomically only after their complete contents are durable. One optional outer
JSON fence is normalized; the result must then satisfy the existing strict
[[Plans]] contract.

An accepted plan receives a canonical JSON SHA-256. A requested correction is
created as a linked revision: the predecessor is marked superseded, but its
plan, claims, artifacts, receipts, and logs remain immutable. A main-checkout
lease is released only when doing so cannot strand an in-flight apply,
verification, or unresolved commit.

An incremental carry-forward is narrower than ordinary revision lineage. It is
available for exactly one successor of a terminal `completed`, `partial`, or
`failed` session whose retained isolated worktree is clean and whose branch
still points to the validated predecessor head. MLX Swarm revalidates the
predecessor execution snapshot, completed artifact manifests, apply and
verification receipts, commit ancestry, and non-mutating artifact digests. It
then freezes `revision-input.json` with that head, the completed task evidence,
and the unfinished/remediation subgraph. The planning claim exposes that clean
retained predecessor worktree as its inspection root, rather than the possibly
different main checkout, and revalidates its branch, head, and cleanliness at
claim and import. Carried mutating tasks include a bounded digest-bound diff
excerpt, while carried review/report tasks include a bounded output excerpt, so
both the successor planner and final reviewer can inspect the semantics rather
than hashes alone. A successor plan must use new task IDs and may contain only
that unfinished or remediation work; importing a plan that repeats a carried
task ID seals the request as invalid. Generation-only and main-checkout
predecessors retain lineage-only revision behavior. A
nonterminal or cleaned worktree, unresolved applied commit, dirty retained
worktree, changed predecessor branch, invalid evidence, or second
carry-forward successor is refused.

The cockpit submits the canonical plan digest when the operator chooses
**Approve and run**. A workspace plan also requires the displayed
[[workspace-execution|execution digest]], binding its Git root, base HEAD,
paths, verification profiles, and any incremental revision authority. The
successor receives fresh plan and execution approvals; predecessor approval is
never replayed. Its session snapshots the validated plan, approvals, planning
receipt, execution contract, `revisionOf` lineage, and compact carried-task
evidence.

## Final Review

Only a locally `completed` session may enter `awaiting_review`. Its compact
`frontier-review-input.json` is the final-review surface; the full
`frontier-result.json` remains the audit artifact.

The retained full result is a v2 generation packet or v3 completed-workspace
packet. The review input is a separate deterministic schema-v1 projection.

The compact projection keeps session and plan identity, task status and
artifact evidence, applicable review/report outputs, concise apply and
verification receipts, workspace base/head/diff evidence, approvals, revision
evidence, and local usage. A successor's revision evidence retains the bounded
unfinished subgraph, predecessor integration failures, and prior frontier
findings so the final reviewer can assess whether the reason for revision was
resolved. It omits the full plan contract, repeated mutating payloads, and
verification logs. Its `sourceArtifact.sha256` binds the exact full result.
Claim and import rederive the projection and require exact structural equality;
the claim and review receipt bind the compact input's canonical digest.
Evidence-changing tampering with either artifact is therefore detected before a
verdict is accepted. Unclaimed historical sessions without the compact artifact
keep the legacy full-packet review path.

Review transitions through `awaiting_review → review_claimed →
approved|changes_requested|rejected|review_error`. The strict review contract
contains a verdict, summary, and structured findings. A verdict never mutates
the completed run; follow-up work requires a separately approved request.

## Usage Accounting

`frontier-usage.json` keeps planning and review receipts separate from
`localUsage`.

Reported adapters must supply internally consistent prompt, completion, and
total tokens. The bundled Codex adapter records nullable token fields with
`usageStatus: unavailable`; it never estimates usage.

The accepted-response ledger is auditable, but MLX Swarm cannot observe hidden
model calls or token accounting inside the Codex host.

## Codex Skill

The bundled `mlx-swarm-commander` skill is installed explicitly by
`mlx-swarm skill install`.

Before claiming a phase, the skill routes a one- or two-file cosmetic,
copy/layout-only, or literal mechanical change directly when it crosses no
behavioral, security, data, concurrency, public-API, or migration boundary.
That direct path performs the edit and one relevant verification without
invoking Swarm. Explicitly governed Swarm requests still use the commander.

For eligible work the skill remains thin: it claims a phase, reads the
deterministic prompt, inspects only the approved root, writes one strict JSON
response, and imports it through the CLI. Persistence and validation remain in
the Python core.
