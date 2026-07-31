# Workspace Execution

Workspace execution uses config schema v2 with plan schema v2 or v3 to convert
local model output and frontier-known deterministic edits into typed,
auditable artifacts. It is implemented in
[[src/mlx_swarm/workspace.py]] and coordinated by [[Executor]], [[Session]],
[[Commander]], and [[UI]].

Schema-v1 configs and plans remain generation-only. They cannot create a
worktree, apply a diff, or execute a verification profile.

## Approved authority

A schema-v2 config declares:

- `workspace.writeRoots`: relative path ceilings below the detected Git root.
- `workspace.verificationProfiles`: named, operator-authored profiles with
  fixed `argv`, worktree-relative `cwd`, timeout, inherited environment names,
  and explicit environment values.

MLX Swarm discovers the nearest Git top-level above the config directory.
Workspace readiness fails if no repository exists, the artifacts/worktree root
is tracked and unignored, or repository-local Git config declares external
clean/smudge/process filters, diff commands, or text conversion commands.
Internal Git subprocesses ignore global/system Git configuration and inherited
`GIT_*` overrides.

The execution digest covers the canonical plan SHA-256, resolved Git root, base
HEAD SHA, configured write-root snapshot, referenced profile definitions, and
worktree runtime root. Contract version 2 also binds the operator-owned
execution policy: `supervised | yolo`, `worktree | checkout`, and the
verification-failure action. Isolated-worktree YOLO uses `repair-once`;
supervised and checkout execution use `pause`. Checkout is valid only with YOLO.
Approval submits this digest and the independent canonical plan digest. The
cockpit binds both in one Approve-and-run action; the CLI equivalent is
`run PLAN --approve-preview`, which computes the preview in-process, prints
the bound contract, and records both digests plus an `approvalShortcut`
provenance marker in the session's execution approval. Explicit
`--approve-plan-digest`/`--approve-execution-digest` flags remain available
and mutually exclusive with the shortcut. See
[[src/mlx_swarm/workspace.py#execution_preview]].

Contract version 3 is used only for an incremental revision successor. It adds
the frozen `revisionAuthority`: predecessor lineage, predecessor execution
digest and branch, `revision-input.json` digest, and validated predecessor head.
That head replaces current checkout `HEAD` as the new `baseSha`, and all of
these fields are covered by the successor execution digest. Preview additionally
requires the predecessor branch still to point to that head. The successor is
worktree-only and receives fresh plan and execution approval.

## Typed task artifacts

Workspace tasks require:

- `artifactType`: `patch`, `test-suite`, `review`, or `report`.
- `allowedPaths`: task-specific relative ceilings, each below a configured
  write root.
- `verification`: configured profile IDs only.
- `workerOutputProtocol`: `artifact` (legacy schema-v2 direct artifact text) or
  `edit-manifest-v1` for deterministic exact-anchor patch materialization.

`patch` and `test-suite` are mutating unified Git diffs. `review` is a JSON
object. `report` is non-mutating text or Markdown. Review and report tasks use
empty path/profile arrays. Plan schema v3 also requires `executionMode`,
`contextRefs`, `interfaceContract`, and `expectedOutputTokens` for each task,
plus top-level `integrationVerification`.

For small local models, `edit-manifest-v1` accepts one strict JSON object with
an `edits` array. Every entry contains exactly `path`, exact `old` text, and
replacement `new` text. A non-empty old anchor must occur exactly once. An
empty old value creates a previously absent text file from the complete,
non-empty new content. Paths pass both allowlists and symlink/runtime checks.
The runtime applies edits in memory, derives a unified diff, and then uses the
normal immutable artifact, digest approval, `git apply --check`, and
verification lifecycle. The worktree is not changed during materialization.

Schema-v3 local mutations must use edit manifests. If the frontier already
knows the exact bytes, `executionMode: deterministic-edit` stores the manifest
in the plan and materializes it with zero model calls or repair attempts.
Otherwise `executionMode: local-agent` permits bounded agent repair only when
the task and CLI both opt in with positive budgets, and preflights expected
output at no more than 70% of the generation ceiling.
`contextRefs` limits each prompt to its owned authoritative sources and
`interfaceContract` freezes the boundary it must preserve.

Schema-v3 mutating siblings may share a wave when their path ceilings are
pairwise disjoint. Overlapping directories are rejected during plan loading.
Artifacts generated from one wave base are applied sequentially only if prior
commits have not changed their affected paths. Schema-v2 keeps the legacy
one-mutation-per-level restriction.

Workers never define a command. Workspace prompts state the output type,
approved paths, and profile IDs, while treating every dependency artifact as
untrusted data. See [[Prompting]].

## Worktree lifecycle

Approval records the current branch/HEAD and any staged, unstaged, or untracked
source warning. The default session starts at that committed SHA in an isolated
worktree, so dirty source state is excluded.

An authorized incremental successor instead starts its isolated worktree from
the validated retained predecessor head. The predecessor commits are therefore
present through Git ancestry; completed predecessor tasks are not rerun or
inserted into the successor DAG.

MLX Swarm creates:

- branch `mlx-swarm/<planId>/<sessionId>`;
- worktree `<artifacts>/_worktrees/<planId>/<sessionId>`;
- immutable `workspace.snapshot.json` beside `session.json`.

The snapshot fixes session authority even if the live config later changes.
YOLO may instead target the current checkout. That target requires a completely
clean repository, holds an artifacts-root checkout lock for the full runner,
rechecks HEAD and cleanliness before every Apply, refuses cleanup, and commits
successful artifacts directly to the displayed current branch.

## Artifact validation and decisions

Each normalized artifact is stored below
`<session>/artifacts/<taskId>/` with an immutable manifest, payload, digest,
affected paths, allowed paths, verification IDs, and base commit.

Before a mutating artifact becomes visible, validation rejects:

- absolute paths, traversal, backslashes, NUL, and `.git`;
- paths outside both task and workspace ceilings;
- runtime artifact/worktree targets and symlink traversal;
- binary patches, duplicate sections, rename/copy metadata;
- symlink and submodule Git modes;
- patches that fail fixed `git apply --check --index --recount`.
- edit manifests with unknown keys, ambiguous/missing anchors, no-op edits,
  non-text files, or more than 64 edits.

Structural failures become deterministic workspace gate violations and may use
only the existing bounded local repair budget.

`--recount` ignores worker-authored hunk line counts and recomputes them from
the actual hunk body. It does not relax path, context, content, binary,
symlink, or metadata checks and does not invent an edit. Apply uses the same
mode, so a diff accepted during preview cannot fail merely because a small
model guessed the hunk line numbers incorrectly.

A valid diff becomes `awaiting_approval`. Supervised Apply/Reject or a
pre-authorized `source: yolo` Apply writes an immutable artifact- and
execution-policy-digest-bound decision. Evidence is fsynced to a temporary inode and
atomically hard-linked to its final path, so readers never observe partial JSON
and concurrent decisions cannot overwrite one another.

Apply rechecks worktree HEAD and cleanliness, validates the diff again, runs
`git apply --index --recount`, and creates one unsigned, hook-free commit with
a fixed local identity. Reject before apply seals the task as
`rejected_by_operator` and blocks descendants.

## Verification

Only snapshotted profiles run. Each subprocess receives the exact configured
argument array with `shell=False`, closed stdin, a worktree-confined resolved
cwd, and a sanitized environment.

It also receives a new process group, a hard timeout, and a bounded combined
log. Verification-created tracked changes in an isolated worktree are restored
and make the attempt fail. In the main checkout they are never automatically
restored, because that could erase concurrent operator work; all detected
changes remain visible and the attempt fails.

A failed profile sets `verification_failed`. Supervised execution and checkout
YOLO keep the applied commit visible and pause; the operator may enqueue
another run of the same profiles or Reject, which creates an explicit revert
commit after cleanliness/HEAD checks. In isolated-worktree YOLO, the first
failure may consume the remaining repair budget: the runtime creates a revert
commit, archives the complete artifact attempt and receipts, and requeues the
worker with bounded failed-verification evidence. Repeated or budget-exhausted
failure pauses. No frontier call occurs during recovery.

Descendants run only after the artifact is applied and every referenced
profile passes. Once all tasks complete, schema-v3
`integrationVerification` profiles run against the combined head; failure
keeps the session partial and records a plan-level receipt.

## Recovery, final packet, and cleanup

The executor owns an exclusive session runner lock while it waits, so the
resident backend remains loaded.

On resume, an interrupted apply is reconciled against the manifest, commit
parent, affected paths, and receipt; it is never blindly applied twice,
including when disjoint sibling artifacts were based on an earlier wave head.
An interrupted verification returns to an operator-controlled verify/reject
pause.

Commander revisions are linked rather than overwriting a failed request. A new
request records `revisionOf`; its predecessor records `supersededByRequestId`
and retains every plan, artifact, receipt, and log. A held main-checkout lease
is released only when no task is applying/verifying and no applied commit
remains unresolved.

Carry-forward is allowed for one successor only, and only from a terminal,
clean, retained isolated worktree whose branch matches the validated workspace
head. Before creating `revision-input.json`, MLX Swarm revalidates the
predecessor execution snapshot and approval, every completed artifact and
receipt, commit ancestry, and the absence of an unresolved applied commit.
The packet records compact completed-task evidence and the unfinished subgraph.
The frontier must plan only unfinished/remediation tasks with new IDs. A
nonterminal, cleaned, dirty, moved, corrupt, or second-generation predecessor
is refused; generation-only and main-checkout revisions remain lineage-only.

Completed workspace runs emit `frontier-result.json` schema version 3 with
base/head/branch lineage, execution approval, applied manifests, apply and
verification receipts, immutable review/report manifests and payload digests,
and the final base-to-head diff. Verification receipt v2 binds its bounded
merged log by SHA-256 and byte count and strictly validates exit, timeout,
cleanup, lineage, profile, and workspace-cleanliness semantics. Legacy v1
receipts remain readable.
Partial/rejected runs remain ineligible for frontier review.

A completed run also emits `frontier-review-input.json`, a deterministic compact
projection containing the final diff, changed paths, applied-artifact and
receipt digests, concise verification outcomes, and relevant non-mutating
outputs. The full result remains the audit artifact and is bound by
`sourceArtifact.sha256`; the compact packet's own digest binds the review claim
and receipt.

Terminal cleanup removes only an isolated session worktree. The session branch
remains for manual inspection or external promotion. Checkout cleanup is
categorically refused. MLX Swarm provides no merge or cherry-pick action.
