# Workspace Execution

Workspace execution is the opt-in schema-v2 boundary that converts local model
output into typed, auditable artifacts. It is implemented in
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
worktree runtime root. Approval submits both this digest and the independent
canonical plan digest. See
[[src/mlx_swarm/workspace.py#execution_preview]].

## Typed task artifacts

Schema-v2 tasks require:

- `artifactType`: `patch`, `test-suite`, `review`, or `report`.
- `allowedPaths`: task-specific relative ceilings, each below a configured
  write root.
- `verification`: configured profile IDs only.

`patch` and `test-suite` are mutating unified Git diffs. `review` is a JSON
object. `report` is non-mutating text or Markdown. Review and report tasks use
empty path/profile arrays. A DAG level may contain at most one mutating task,
while non-mutating tasks remain batchable.

Workers never define a command. Workspace prompts state the output type,
approved paths, and profile IDs, while treating every dependency artifact as
untrusted data. See [[Prompting]].

## Worktree lifecycle

Approval records the current committed HEAD and any staged, unstaged, or
untracked source warning. The session starts at that committed SHA, so dirty
source state is excluded.

MLX Swarm creates:

- branch `mlx-swarm/<planId>/<sessionId>`;
- worktree `<artifacts>/_worktrees/<planId>/<sessionId>`;
- immutable `workspace.snapshot.json` beside `session.json`.

The snapshot fixes session authority even if the live config later changes.
The original checkout is never modified.

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
- patches that fail fixed `git apply --check --index`.

Structural failures become deterministic workspace gate violations and may use
only the existing bounded local repair budget.

A valid diff becomes `awaiting_approval`. Apply or Reject writes an immutable
digest-bound decision. Concurrent decisions cannot overwrite one another.

Apply rechecks worktree HEAD and cleanliness, validates the diff again, runs
`git apply --index`, and creates one unsigned, hook-free commit with a fixed
local identity. Reject before apply seals the task as
`rejected_by_operator` and blocks descendants.

## Verification

Only snapshotted profiles run. Each subprocess receives the exact configured
argument array with `shell=False`, closed stdin, a worktree-confined resolved
cwd, and a sanitized environment.

It also receives a new process group, a hard timeout, and a bounded combined
log. Verification-created tracked changes are restored and make the attempt
fail; any remaining workspace changes are recorded.

A failed profile sets `verification_failed` and keeps the applied commit
visible. The operator may enqueue another run of the same profiles or Reject,
which creates an explicit revert commit. No local worker repair or frontier
call occurs because of human rejection or verification failure.

Descendants run only after the artifact is applied and every referenced
profile passes.

## Recovery, final packet, and cleanup

The executor owns an exclusive session runner lock while it waits, so the
resident backend remains loaded.

On resume, an interrupted apply is reconciled against the manifest, commit
parent, affected paths, and receipt; it is never blindly applied twice. An
interrupted verification returns to an operator-controlled verify/reject
pause.

Completed workspace runs emit `frontier-result.json` schema version 3 with
base/head/branch lineage, execution approval, applied manifests, apply and
verification receipts, non-mutating outputs, and the final base-to-head diff.
Partial/rejected runs remain ineligible for frontier review.

Terminal cleanup removes only the session worktree. The session branch remains
for manual inspection or external promotion. MLX Swarm provides no merge,
cherry-pick, or original-checkout apply action.
