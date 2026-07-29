# Executor

DAG executor — runs tasks in dependency order with gate-feedback repair loops.

The executor in [[src/mlx_swarm/executor.py#execute_plan]] orchestrates the full plan execution.

## Execution Flow

1. **Initialize session**: Create or resume a [[Session]].
2. **Topological sort**: Group tasks by dependency level via `plan.topological_order()`.
3. **For each level**:
   a. Block tasks whose dependencies were rejected, failed, or blocked.
   b. Filter out already-completed tasks and chunk the rest by `maxWorkers`.
   c. Mark runnable tasks as "running".
   d. Compose prompts using [[Prompting|compose_prompt]] with successful dependency outputs.
   e. For a digest-bound evaluation replay, substitute the exact saved initial
      prompt instead of recomposing it.
   f. Validate prompt lengths against `maxPromptCharacters`.
   g. Call the persistent [[Backend]] directly, or run a local reasoning pass
      followed by a strict editing pass for mutating tasks when configured.
   h. Process outputs: evaluate [[Gates]], normalize, update session.
   i. **Repair loop**: For rejected tasks within both task and CLI repair caps, compose repair prompts with [[Gates#Gate Feedback]].
4. **Final status**: "completed" if all pass, "failed" if execution failed, "partial" otherwise.
5. **Frontier handoff**: Persist one compact `frontier-result.json` for final frontier review.

See [[src/mlx_swarm/executor.py#execute_plan]].

## Workspace task flow

For a schema-v2 task, successful deterministic gates are followed by typed
artifact validation. Non-mutating review/report artifacts complete normally.
A valid patch/test-suite artifact moves through:

`running → awaiting_approval → applying → verifying → completed`

Operator rejection produces `rejected_by_operator`. A failed configured check
produces `verification_failed`, which pauses for an operator Verify or Reject
decision. Verify runs only the same snapshotted profiles. Reject after apply
creates a revert commit. Neither action triggers local repair generation or a
frontier call.

In supervised mode the executor polls the immutable decision ledger while the
model remains resident. In YOLO mode it writes an equally immutable
`source: yolo` Apply receipt bound to the artifact and snapshotted execution
policy digests, then applies and verifies without another frontier call. A
verification failure ends the active runner as a resumable partial session; it
never triggers automatic repair or rollback.

The executor holds an exclusive session runner lock. Main-checkout YOLO also
holds a repository-wide runner lock so two sessions cannot mutate the same
checkout concurrently. Recovery reconciles interrupted artifact persistence and
Git commits without duplicate apply. See
[[workspace-execution]] and
[[src/mlx_swarm/executor.py#_await_workspace_tasks]].

## Repair Loop

After initial generation, rejected tasks with remaining task-level and global `--max-repair` budget enter the repair loop:

1. Collect all rejected tasks with remaining repair budget.
2. Compose repair prompts with [[Gates#Gate Feedback]] and previous output.
3. Run repair batch through [[Backend|generate_batch]].
4. Process outputs and evaluate gates again.
5. Repeat until all pass or budget exhausted.

See [[src/mlx_swarm/executor.py#_process_task_output]].

## Reasoning to editing

With `worker.mode=reasoning-edit`, patch and test-suite generations use two
local stages that remain entirely inside local usage.

The reasoning task enables the model's thinking template with a bounded local
token allowance. Its output is saved in an immutable
`reasoning-attempts` record and is explicitly non-authoritative. The editing
task disables thinking, embeds the reasoning as JSON-string-encoded untrusted
evidence, and requires only the task's strict artifact. Stage statistics are
aggregated once into local tokens, generation calls, timing, and model loads.
Review/report tasks retain the direct path.

## Output Processing

`_process_task_output` handles each worker's output:
1. Evaluate the gate (or auto-pass if no gate).
2. Normalize output (strip preamble, code fences).
3. Update session state with status, output, gate result.

## Batch Records

Each bounded chunk is recorded in the session's `batches` array with:
- Level index, chunk index, phase, task IDs.
- Start/finish timestamps.
- Generation statistics from the backend.
- Repair round details and real elapsed timing.
