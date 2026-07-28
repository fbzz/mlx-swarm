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
Plan schema, typed artifact fields, configured write roots/profile IDs, and
local runtime limits.

The planning response must include `context.diagnosis`. During that same
frontier call, the commander traces the observed failure through exact
authoritative source excerpts or an already-approved verification receipt,
states one falsifiable causal hypothesis, records its concrete validation
evidence, and names the condition that would disprove it. Unsupported
speculation is rejected at import and seals that request as invalid.

Planning transitions through `open → claimed → accepted|invalid`. Claim files
use exclusive creation so concurrent skill or file-import invocations cannot
both occupy the response slot. Claims and immutable response artifacts publish
atomically only after their complete contents are durable. One optional outer
JSON fence is normalized; the result must then satisfy the existing strict
[[Plans]] contract.

An accepted plan receives a canonical JSON SHA-256. The cockpit submits that
digest when the operator chooses **Approve and run**. A workspace plan also
requires the displayed [[workspace-execution|execution digest]], binding its
Git root, base HEAD, paths, and verification profiles. The session snapshots
the validated plan, approval, planning receipt, execution contract, and any
`revisionOf` lineage.

## Final Review

Only a locally `completed` session may enter `awaiting_review`. The review
surface is its `frontier-result.json` v2 generation packet or v3
completed-workspace packet.

The packet contains the plan contract, completed outputs, gate/verification
evidence, Git lineage where applicable, and local usage required by the
reviewer.

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

It remains thin: the skill claims a phase, reads the deterministic prompt,
inspects only the approved root, writes one strict JSON response, and imports
it through the CLI. Persistence and validation remain in the Python core.
