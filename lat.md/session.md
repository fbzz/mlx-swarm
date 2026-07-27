# Session

Persistent session state — tracks task status, outputs, gate results, and batch statistics.

The `Session` class in [[src/swarm_agents/session.py#Session]] manages all state for a single plan execution. Every state change is immediately persisted to `session.json` on disk, enabling crash recovery and resume.

Every file-backed plan is snapshotted as `plan.snapshot.json` when its session is
created. Historical inspection and retry therefore remain available even if the
frontier-authored source plan is later changed or removed. Optional
`launchSource`, `retryOf`, and `maxRepair` metadata extend the session contract
without requiring migration of existing `session.json` files.

## Lifecycle

Session state transitions from creation to completion.

1. **Created**: When `execute_plan` starts, a new Session is initialized in `artifacts_dir / plan_id / run_id`.
2. **Running**: Tasks transition through pending → running → completed/rejected/failed.
3. **Finished**: Final status is set to "completed", "partial", or "failed", then one frontier-result packet is written.

## State Structure

JSON shape persisted to session.json on disk.

```json
{
  "sessionId": "20260727T120000Z-abcdef12",
  "planId": "my-plan",
  "objective": "...",
  "startedAt": "2026-07-27T12:00:00Z",
  "status": "running",
  "launchSource": "ui",
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
- **export_results**: Export completed artifacts and compact failure metadata.
- **write_frontier_result**: Persist the single final-review packet with its plan source; rejected raw generations are omitted and compact local token usage is included.

## Resume

Sessions can be resumed via `swarm resume <session_dir>`. The [[Executor]] preserves the session ID and completed artifacts, recovers interrupted tasks, and resumes stored rejections only when repair budget remains. See [[src/swarm_agents/session.py#Session#load]].
