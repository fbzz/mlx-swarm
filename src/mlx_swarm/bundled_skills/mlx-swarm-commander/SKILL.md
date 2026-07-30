---
name: mlx-swarm-commander
description: Decide whether MLX Swarm is warranted, create strict frontier-authored DAG plans for qualifying commander requests, and perform one final review of completed runs. Use when the user asks to plan, command, continue, wait for, or review MLX Swarm work; route simple changes to direct Codex implementation instead of invoking Swarm.
---

# MLX Swarm Commander

Use the installed `mlx-swarm` CLI for every state transition. Treat the skill as
the frontier planning or final-review phase; do not reproduce persistence,
validation, approval, execution, or claim logic in ad hoc scripts.

## Decide whether to invoke Swarm

Route before creating or claiming a commander request. Do not invoke MLX Swarm
for a simple change unless the user explicitly requires a governed run. Use the
ordinary direct Codex edit-and-test workflow when all of these are true:

- the change is confined to one or two files;
- it is cosmetic, copy-only, local layout, or an obvious mechanical/literal
  transformation;
- it does not touch migrations, security, concurrency, external APIs,
  persistence contracts, or cross-cutting behavior; and
- one bounded direct verification command can establish correctness.

Briefly tell the user that the work was routed directly because Swarm would add
more ceremony than safety. Invoke Swarm for substantial multi-file work or
changes whose integration, isolation, approval, or audit risk justifies a
governed DAG. Once a commander request has already been approved or launched,
continue its durable workflow rather than rerouting it mid-session.

## Use the two-agent envelope

Obey the claimed prompt's capability contract if it differs. For the shipped
Qwen3.5 4B profile, plan at most two runnable local agents per wave, with a
49,152-token aggregate rendered-prompt budget. The 262,144-token model context
belongs to each request; never divide it into fixed per-agent slices. Give each
task only its authoritative context and let the runtime serialize a wave when
the aggregate budget would be exceeded.

Use deterministic edits when bytes are known, consuming zero model tokens. For
local patch or test tasks, keep expected output at or below 700 tokens and
`max_tokens` at or below 1,024. Use 768 for normal reviews, up to 1,024 only
when evidence-heavy; use 1,536 for reports, up to 2,048 only when genuinely
indivisible. Set `maxRepairAttempts` to zero. If an artifact could exceed 70%
of its ceiling, split it before execution. If a result reports
`hitTokenLimit`, do not request a repair of the same task: split it or
deliberately raise its bounded ceiling in a new plan. Keep global thinking off;
use a configured local reasoning stage only as a selective fallback.

## Prepare a plan

1. Obtain the config path and commander request ID from the cockpit handoff.
2. Run:

   `mlx-swarm --config CONFIG commander claim-plan REQUEST_ID`

3. Read the returned `promptPath`. Inspect only files whose resolved paths are
   below the returned, auto-detected `workspaceRoot`.
4. Produce exactly one Plan schema JSON object. When the prompt specifies
   workspace execution, use schema version 3 and declare `artifactType`,
   `workerOutputProtocol`, `executionMode`, `contextRefs`,
   `interfaceContract`, `expectedOutputTokens`, `allowedPaths`, and
   verification profile IDs for every task. Patch and test-suite agents must
   use `edit-manifest-v1`: agents return strict exact search/replace JSON and
   MLX Swarm materializes the operator-visible unified diff. Use
   `deterministic-edit` with an inline manifest when the exact bytes are already
   known and no agent judgment is required. Review and report tasks are
   non-mutating. Assign disjoint path ceilings to independent mutating tasks so
   they can share a wave; serialize overlapping ownership. Select only the
   authoritative source labels each task needs, freeze its interface boundary,
   and keep expected output at or below 70% of its generation ceiling. Declare
   plan-level integration verification. Never invent or emit command arrays.
   During this same planning call, inspect the supplied failure evidence and
   trace the relevant source path. Populate the mandatory `context.diagnosis`
   with one falsifiable causal hypothesis, its validation method and evidence,
   the exact authoritative-source labels supporting it, and a falsification
   condition. Use only `source-trace` or an already approved verification
   receipt; never run an unapproved command or promote a speculative diagnosis.
   Obey the prompt's `WORKER CAPABILITY CONTRACT` as an authority boundary.
   It describes local model scale, specialization, measured calibration, and
   the maximum safe delegation level. Never infer stronger capability from the
   model name. For `exact-edit`, retain diagnosis and edit design in this
   frontier call, then give each mutating agent one mechanical transformation
   with exact file, symbol, source anchors, and old-to-new instructions.
   Complete the mandatory candidate-change specificity gate before emitting the
   plan. Trace the literal proposed edit through the observed failing path and
   at least one named passing or non-target control path. Explain why the
   predicate uses the narrowest evidence-backed discriminator rather than a
   broader proxy, cite exact authoritative excerpts for those predictions, and
   keep `changeValidation.candidateChange` consistent with every mutating task's
   literal old-to-new instructions.
   Do not choose, request, or encode an execution approval mode or workspace
   target in the plan. Supervised versus YOLO and worktree versus checkout are
   operator-owned cockpit choices bound into a separate execution digest.
5. Save the response beside the prompt as
   `frontier-plan.response.json`.
6. Import it once using the returned claim:

   `mlx-swarm --config CONFIG commander import-plan REQUEST_ID RESPONSE --claim-id CLAIM_ID --adapter codex-skill`

7. Report the validation result and tell the operator to preview and approve
   both the plan digest and the displayed Git execution digest in the cockpit.

Do not retry after an imported invalid response. A new frontier call requires a
new commander request.

## Continue or wait

When the user asks to wait, inspect the request with:

`mlx-swarm --config CONFIG commander show REQUEST_ID`

Do not approve the plan for the operator. If it is not approved, stop with the
cockpit instruction. If `sessionRef` exists, use `mlx-swarm --config CONFIG
list` to resolve the session directory. Observe local execution without
creating or importing any frontier artifact between worker waves.
Do not apply or reject workspace artifacts for the operator. In supervised
mode those decisions remain digest-bound human actions. In YOLO, the runtime
may publish its own `source: yolo` decision only because the operator already
approved that exact execution policy and target digest; the skill never enables
or changes it.

For a separately approved follow-up to a terminal isolated-worktree run, create
the successor with `commander create --revision-of PLAN_ID/SESSION_ID`. The
returned prompt is bound to the predecessor's validated Git head and compact
`revision-input.json`. Plan only the unfinished or remediation subgraph; never
repeat carried task IDs. Incremental carry-forward is limited to one successor
and still requires fresh plan and execution approvals.

## Perform final review

Only review a completed session:

1. Run:

   `mlx-swarm --config CONFIG commander claim-review SESSION_DIR`

2. If the phase is already claimed or reviewed, inspect its status instead of
   creating another result.
3. Read only the returned `promptPath`. Its evidence is the compact
   `frontier-review-input.json`; the embedded source digest binds the retained
   full `frontier-result.json`.
4. Produce exactly one FrontierReview JSON object and save it beside the prompt
   as `frontier-review.response.json`.
5. Import it once:

   `mlx-swarm --config CONFIG commander import-review SESSION_DIR RESPONSE --claim-id CLAIM_ID --adapter codex-skill`

6. Report the persisted verdict. Never mutate the completed session or launch a
   follow-up automatically.

Do not pass token flags for the Codex adapter. MLX Swarm must record its
frontier token usage as unavailable rather than estimating it.
