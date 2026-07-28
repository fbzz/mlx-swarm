# Session

Persistent session state — tracks task status, outputs, gate results, and batch statistics.

The `Session` class in [[src/mlx_swarm/session.py#Session]] manages all state for a single plan execution. Every state change is immediately persisted to `session.json` on disk, enabling crash recovery and resume.

Every file-backed plan is snapshotted as `plan.snapshot.json` when its session is
created. Historical inspection and retry therefore remain available even if the
frontier-authored source plan is later changed or removed. Optional
`launchSource`, `retryOf`, `revisionOf`, commander approval, `reviewStatus`, and
`maxRepair` metadata extend the session contract
without requiring migration of existing `session.json` files.

## Lifecycle

Session state transitions from creation to completion.

1. **Created**: When `execute_plan` starts, a new Session is initialized in `artifacts_dir / plan_id / run_id`.
2. **Running**: Tasks transition through pending → running →
   completed/rejected/failed. Workspace tasks may additionally pause at
   awaiting_approval or verification_failed and pass through applying and
   verifying.
3. **Finished**: Final status is set to "completed", "partial", or "failed", then one frontier-result packet is written.
4. **Reviewed**: Completed sessions may receive one separate [[Commander|frontier review]]; local status never changes.

## State Structure

JSON shape persisted to session.json on disk.

```json
{
  "sessionId": "20260727T120000Z-abcdef12",
  "planId": "my-plan",
  "objective": "...",
  "startedAt": "2026-07-27T12:00:00Z",
  "status": "running",
  "reviewStatus": "pending_local",
  "launchSource": "commander",
  "commanderRequestId": "request-...",
  "planApproval": { "planSha256": "..." },
  "retryOf": "my-plan/20260727T110000Z-12345678",
  "maxRepair": 2,
  "planSnapshot": "plan.snapshot.json",
  "tasks": {
    "task-id": {
      "id": "task-id",
      "role": "implementation",
      "status": "completed",
      "output": "raw LLM output",
      "normalizedOutput": "cleaned output",
      "gateResult": { "passed": true, "violations": [] },
      "repairAttempts": 0,
      "generationAttempts": [
        {
          "attempt": 1,
          "phase": "generation",
          "path": "attempts/task-id/attempt-001.json",
          "promptSha256": "...",
          "outputSha256": "...",
          "gatePassed": true,
          "repeatedOutput": false
        }
      ],
      "batchIndex": 0
    }
  },
    "batches": [...],
    "frontierResult": "/path/to/frontier-result.json",
  "configSource": "/path/to/swarm.json",
  "planSource": "/path/to/plan.json"
}
```

## Operations

Methods for reading and updating session state.

- **update_task**: Update task fields and persist.
- **get_task_output**: Get normalized output only from a completed task.
- **get_task_status**: Check current status.
- **add_batch_record**: Record batch execution statistics.
- **record_generation_attempt**: Persist the exact prompt, raw output,
  normalized output, gate result, statistics, and digests in an immutable
  per-task attempt file.
- **export_results**: Export completed artifacts and compact failure metadata.
- **attach_commander**: Snapshot the planning receipt, digest approval, and optional revision lineage.
- **attach_workspace**: Persist the immutable execution contract, Git lineage,
  branch/worktree path, and dual-digest approval.
- **write_frontier_result**: Persist the self-contained v2 generation packet or
  v3 completed-workspace packet; rejected raw generations are omitted and
  compact local usage remains separate from frontier receipts.

## Resume

Sessions can be resumed via `mlx-swarm resume <session_dir>`. The [[Executor]] preserves the session ID and completed artifacts, recovers interrupted tasks, and resumes stored rejections only when repair budget remains. See [[src/mlx_swarm/session.py#Session#load]].

Workspace session loading reconstructs path and profile authority from
`workspace.snapshot.json`, not the live config. Completed v3 packets include
applied artifact manifests, apply/verification receipts, non-mutating outputs,
and the final base-to-head branch diff. See [[workspace-execution]].
