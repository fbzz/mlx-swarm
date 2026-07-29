# Tests

Test specifications for the mlx-swarm framework.

All tests use pytest with tmp_path fixtures. No MLX/model loading is required — backend and executor are mocked where needed.

## Backend

Prompt rendering and generation configuration tests. See [[src/mlx_swarm/backend.py]].

### Thinking-disabled template
Templates that always open a thinking block are closed in the assistant prefix when thinking is disabled.

### Thinking-enabled template
Thinking prefixes remain open when the plan explicitly enables reasoning.

### Per-task generation configuration
Role defaults and validated task overrides merge without inheriting another task's settings.

## Contracts

Validation of [[Config]] and [[Plans]] JSON schemas.

### Config loading
Valid config loads successfully with correct field resolution and artifacts path. See [[src/mlx_swarm/contracts.py#load_config]].

### Config with local path
Config with localPath resolves model directory correctly.

### Bad schema version
Schema version outside supported range raises ContractError.

### Unknown config field
Unknown fields in config raise ContractError.

### Missing required model
Config without model field raises ContractError.

### Bad max workers
maxWorkers=0 raises ContractError (must be >= 1).

### Strict boolean config
Non-boolean enableThinking values raise ContractError.

### Plan loading
Valid plan loads with correct tasks and dependencies.

### Duplicate task ID
Duplicate task IDs raise ContractError.

### Bad dependsOn
dependsOn referencing unknown task raises ContractError.

### Circular dependency
Circular dependencies detected by topological_order, raises ContractError.

### Topological order
Tasks correctly grouped by dependency level.

### Bad role
Invalid role value raises ContractError.

### Plan with gate
Gate patterns parse correctly from plan JSON.

### Generation overrides and formats
Unknown generation override keys and unsupported gate formats raise ContractError.

### Task output protocol
Task-specific output protocol parses and overrides shared protocol.

### Too many tasks
Plans exceeding 128 tasks raise ContractError.

### Invalid JSON
Malformed JSON raises ContractError with line number.

### Plan context
Context with authoritative sources, constraints, and rejection criteria parses correctly.

## Gates

[[Gates|Gate]] evaluation and output normalization. See [[src/mlx_swarm/gates.py]].

### Preamble stripping
"Here is the code." prefix is removed; plain code is unchanged.

### Code fence normalization
Single markdown code fence is stripped; no-fence output is unchanged.

### Gate with no configuration
None gate auto-passes with no violations.

### Required pattern match
Required pattern present passes; missing fails with violation.

### Forbidden pattern match
Forbidden pattern absent passes; present fails with violation.

### Max characters
Output exceeding maxCharacters fails with size violation.

### JSON format
Valid JSON passes; invalid JSON fails with format violation.

### Gate feedback
Feedback string contains violation details and fix instructions.

### Preamble + fence
Combined preamble stripping and code fence normalization works correctly.

### Typed Python and JSON validation
Python syntax errors and JSON schema violations produce deterministic gate failures.

### Model protocol normalization
Thinking blocks and trailing model role tokens are excluded from normalized artifacts.

## Prompting

[[Prompting|Prompt composition]] and dependency injection. See [[src/mlx_swarm/prompting.py]].

### No context
Prompt without context includes task prompt and worker identity.

### With context
Full prompt includes authority, objective, sources, constraints, rejection criteria, task, and protocol.

### Dependency injection
Completed dependency output is injected with untrusted-data warning.

### Dependency not completed
Uncompleted dependency shows placeholder message.

### Repair prompt
Repair prompt includes original prompt, gate feedback, and JSON-encoded
previous output without nested Markdown delimiters.

### Worker identity
Prompt always includes worker id and role section.

### Task-specific protocol
Task protocol overrides a conflicting shared output protocol.

### Edit-manifest protocol
Workspace prompts explain the strict edit JSON shape and preserve the
operator-visible unified-diff approval boundary.

## Session

[[Session]] persistence and state management. See [[src/mlx_swarm/session.py]].

### Initialization
Session creates correct initial state with all tasks pending and persists to disk.

### Update task
Task update persists and is retrievable.

### Get task output
Returns normalized output only after successful completion.

### Summary
Summary correctly counts completed/rejected/pending tasks.

### Export results
Export includes all task states with outputs and gate results.

### Batch records
Batch records are stored and persisted.

### Set status
Only terminal status updates include finishedAt.

### Persist and reload
State survives save/load cycle.

### Historical plan snapshots
New sessions retain the validated plan, while legacy sessions without a snapshot
continue to load from their original plan source.

### Immutable generation attempts
Initial generations and repairs persist exact prompt/output evidence and
identify repeated deterministic responses without exposing rejected text in
the frontier result.

## Executor

End-to-end local orchestration tests use a fake backend. See [[src/mlx_swarm/executor.py]].

### Dependency failure
Rejected parents block descendants and rejected text is never injected.

### Global repair cap
`max_repair=0` disables repair even if the task permits attempts.

### Repeated repair feedback
An identical rejected repair is recorded and changes the next feedback instead
of reproducing an identical deterministic prompt.

### Wide-level chunking
Dependency levels wider than maxWorkers execute in bounded chunks.

### Backend failure
Generation exceptions mark tasks failed and block descendants.

### Backend initialization failure
Model resolution failures become persisted failed sessions with a frontier packet.

### Resume preservation
Completed task state and session identity survive resume without re-execution.

### Frontier packet
One final packet is persisted and omits rejected raw output.

## CLI

CLI entrypoint tests. See [[src/mlx_swarm/cli.py]].

### Doctor ready
Doctor command returns 0 when model is available.

### Doctor not ready
Doctor command returns 1 when model is unavailable.

### Run success
Run command returns 0 when all tasks complete.

### Run partial
Run command returns 1 when status is partial.

### List empty
List command returns 0 with no sessions.

### List with sessions
List command returns 0 and shows existing sessions.

## Economics evaluation

[[economics-evaluation]] tests cover the frozen-study contracts, paired
execution evidence, economics calculation, and deterministic publication.

They cover portable BugsInPy metadata, safe command parsing and container
timeouts, deterministic disjoint suite selection, patch-size balancing,
leakage prevention, Codex JSONL and strict Hermes JSON usage capture,
frontier identity/version pins, schema-v1 profile compatibility,
schema-v2 adapter strictness, missing-usage invalidation,
symmetric oracle scoring, storage gates, immutable/resumable ledgers, seeded
bootstrap intervals, the strict claim gate, sanitized exports, deterministic
README rendering, and read-only cockpit API serialization.
Interrupted preparation tests prove completed case-runtime reuse, retained
exclusions, profile binding, metadata cleanup, and sealed-suite immutability.

Fairness regressions additionally require one immutable paired-arm contract
with a shared write-root set and task packet, reject contract drift, reject
narrowed local plan authority, reject rewritten workspace source excerpts, and
mark historical pre-protocol reports invalid rather than interpreting their
score gap as worker quality.

Protocol-v4 tests freeze the active Docker endpoint, separate verifier
infrastructure invalidation from candidate test failures, require all frozen
local calibration replays before measured work, preserve exact saved prompt
digests, and verify that reasoning-to-editing stages remain local and separately
auditable.

### Inspect
Inspect command returns session summary.

### Inspect task output
Inspect with --task and --output prints task output.

### Contract error
Invalid config raises SystemExit(2) via argparse error.

### UI command
The localhost cockpit receives the configured plan root, port, and browser-open
preference.

### Commander and skill commands
Commander creation, claim/import, unavailable usage, and config-independent
skill installation use deterministic CLI paths.

## Commander

Frontier contracts and persistence tests. See
[[src/mlx_swarm/commander.py]].

### Request and workspace
Requests bind to the config directory, preserve constraints and revision
lineage, and generate deterministic prompts.

### Exclusive phase claims
Planning and review response slots cannot be claimed or imported twice. Claims
can be released only before a raw response is recorded.

### Strict plan and review imports
One optional outer JSON fence is accepted; malformed contracts persist errors
and seal their phase without an automatic frontier retry.
Commander plans without a same-call evidence-backed causal diagnosis are
rejected and sealed.

### Digest approval
Canonical plan SHA-256 mismatch prevents launch.

### Usage separation
Planning and review receipts remain separate from local usage. Missing Codex
usage is nullable and explicitly unavailable.

### Completed-only review
Completed sessions accept one structured verdict. Partial and failed sessions
remain ineligible and use local resume/retry evidence.

### Bundled skill
The packaged skill validates, installs to an explicit skills root, and refuses
implicit overwrite.

## UI

[[UI]] server and serialization tests. See [[src/mlx_swarm/ui.py]] and the
packaged assets under [[src/mlx_swarm/ui_static/app.js]].

### Plan catalog
Only validated plans inside the approved root are launchable; artifact snapshots,
model files, invalid plans, and duplicate plan IDs cannot silently enter the
catalog.

### Launch isolation
Run, resume, and retry use argument arrays with `shell=False`, preserve configured
repair limits, and write runner diagnostics beside the session.

### Resume and retry
Resume preserves completed task state. Partial or failed runs remain unchanged
while retry creates a new session with `retryOf` lineage.

### Live detail serialization
Task states, normalized and raw output, gate violations, normalizations, repairs,
batch statistics, token totals, and the frontier packet are exposed together.

### Commander acceptance flow
A three-node DAG passes through request, import, digest approval, local
completion, self-contained frontier packet, and one final persisted verdict.

### HTTP boundary
Static assets and JSON endpoints enforce request size, JSON validity, path safety,
localhost binding, and same-origin mutation requests. Server shutdown is graceful.

## Workspace execution

Schema-v2 tests use real temporary Git repositories and never touch the source
checkout.

### Contracts and execution digest

Strict workspace config/profile fields, schema-v2 typed tasks, write-root
intersection, profile references, one mutating task per level, schema-v1
generation-only compatibility, plan binding, and HEAD-sensitive execution
digests are covered.

### Worktrees and lineage

Tests prove dirty source changes are excluded, the session branch begins at
committed HEAD, and applied artifacts create commits only in the worktree.

Final v3 packets contain base/head/diff evidence, cleanup removes only the
worktree, and the branch remains.

### Diff boundary

Traversal, binary metadata, unapproved paths, symlink traversal, submodule
modes, stale lineage, and repository-local external Git drivers are rejected.

Global/system Git drivers and inherited `GIT_*` overrides are disabled. Fixed
`git apply --check --recount` accepts correct edit bodies with inaccurate hunk
line metadata; remaining structural failures enter the bounded local repair
loop.

Strict `edit-manifest-v1` contracts require an exact JSON gate. Exact unique
anchors materialize into an immutable unified diff without changing the
worktree; malformed keys, ambiguous anchors, no-ops, and escaped paths fail
before artifact approval.

### Human decisions and recovery

The executor waits with its backend open, concurrent decisions cannot overwrite
evidence, and operator rejection blocks descendants.

Failed verification waits without another worker call, Verify reruns the same
profiles, Reject creates a revert commit, and crash recovery recognizes an
already committed artifact.

### Verification processes

Exact argv, `shell=False`, closed stdin, sanitized environment, confined cwd,
bounded one-megabyte logs, and process-group timeouts are asserted.

### CLI and API

Workspace preview, dual-digest launch, artifact decisions, status, cleanup,
same-origin artifact endpoints, digest mismatch, subprocess arrays, and
retained branches are covered.
