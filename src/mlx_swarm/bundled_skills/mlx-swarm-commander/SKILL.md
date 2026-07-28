---
name: mlx-swarm-commander
description: Create strict frontier-authored DAG plans for MLX Swarm commander requests and perform one final review of completed frontier-result.json packets. Use when the user asks to plan, command, continue, wait for, or review an MLX Swarm run without adding frontier calls between local worker waves.
---

# MLX Swarm Commander

Use the installed `mlx-swarm` CLI for every state transition. Treat the skill as
the frontier planning or final-review phase; do not reproduce persistence,
validation, approval, execution, or claim logic in ad hoc scripts.

## Prepare a plan

1. Obtain the config path and commander request ID from the cockpit handoff.
2. Run:

   `mlx-swarm --config CONFIG commander claim-plan REQUEST_ID`

3. Read the returned `promptPath`. Inspect only files whose resolved paths are
   below the returned, auto-detected `workspaceRoot`.
4. Produce exactly one Plan schema JSON object. When the prompt specifies
   workspace execution, use schema version 2 and declare `artifactType`,
   `workerOutputProtocol`, `allowedPaths`, and verification profile IDs for
   every task. Prefer `edit-manifest-v1` for patch and test-suite workers:
   workers return strict exact search/replace JSON and MLX Swarm materializes
   the operator-visible unified diff. `artifact` retains direct unified-diff
   output. Review and report tasks are non-mutating. Never invent or emit
   command arrays.
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
   frontier call, then give each mutating worker one mechanical transformation
   with exact file, symbol, source anchors, and old-to-new instructions.
   Complete the mandatory candidate-change specificity gate before emitting the
   plan. Trace the literal proposed edit through the observed failing path and
   at least one named passing or non-target control path. Explain why the
   predicate uses the narrowest evidence-backed discriminator rather than a
   broader proxy, cite exact authoritative excerpts for those predictions, and
   keep `changeValidation.candidateChange` consistent with every mutating task's
   literal old-to-new instructions.
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
Do not apply or reject workspace artifacts for the operator. Those decisions
remain digest-bound human actions in the cockpit or deterministic CLI.

## Perform final review

Only review a completed session:

1. Run:

   `mlx-swarm --config CONFIG commander claim-review SESSION_DIR`

2. If the phase is already claimed or reviewed, inspect its status instead of
   creating another result.
3. Read only the returned `promptPath`, whose review evidence is the completed
   `frontier-result.json`.
4. Produce exactly one FrontierReview JSON object and save it beside the prompt
   as `frontier-review.response.json`.
5. Import it once:

   `mlx-swarm --config CONFIG commander import-review SESSION_DIR RESPONSE --claim-id CLAIM_ID --adapter codex-skill`

6. Report the persisted verdict. Never mutate the completed session or launch a
   follow-up automatically.

Do not pass token flags for the Codex adapter. MLX Swarm must record its
frontier token usage as unavailable rather than estimating it.
