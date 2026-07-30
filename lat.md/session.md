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
3. **Finished**: Final status is set to "completed", "partial", or "failed",
   then the full `frontier-result.json` audit packet is written. A completed
   session also emits compact `frontier-review-input.json`.
4. **Reviewed**: Completed sessions may receive one separate
   [[Commander|frontier review]] from that compact, digest-bound input; local
   status never changes.

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
  "approvalMode": "yolo",
  "workspaceTarget": "worktree",
  "commanderRequestId": "request-...",
  "revisionOf": "prior-plan/prior-session",
  "revisionDepth": 1,
  "revisionInputSha256": "...",
  "inheritedBaseSha": "...",
  "carriedTasks": [
    {
      "taskId": "completed-task",
      "artifactSha256": "...",
      "commitSha": "..."
    }
  ],
  "planApproval": {
    "planSha256": "...",
    "executionSha256": "...",
    "executionPolicySha256": "..."
  },
  "retryOf": "my-plan/20260727T110000Z-12345678",
  "maxRepair": 0,
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
- **record_reasoning_attempt**: Persist a local reasoning stage separately and
  mark it non-authoritative.
- **replay_prompt**: Load a saved evaluation prompt only after verifying its
  session-confined path and SHA-256.
- **export_results**: Export completed artifacts and compact failure metadata.
- **attach_commander**: Snapshot the planning receipt, digest approval, optional
  revision lineage, and digest-validated `revision-input.json` carry-forward
  evidence.
- **attach_workspace**: Persist the immutable execution contract, execution
  policy, Git lineage, branch/execution path, and digest-bound approval.
- **write_frontier_result**: Persist the self-contained v2 generation packet or
  v3 workspace packet; rejected raw generations are omitted and compact local
  usage remains separate from frontier receipts. For completed sessions, also
  derive `frontier-review-input.json`, whose `sourceArtifact.sha256` binds the
  retained full packet and whose own canonical digest binds review claim and
  receipt.

Before local generation, the session also seals `localExecutionProfile`: the
resolved model path and content fingerprint, batch limits, worker capability
contract, role defaults, Python/platform/package versions, and the MLX Swarm
source digest. Resume rejects a different profile, and the cockpit plus final
packet expose it for later comparison.

## Resume

Sessions can be resumed via `mlx-swarm resume <session_dir>`. The [[Executor]] preserves the session ID and completed artifacts, recovers interrupted tasks, and resumes stored rejections only when repair budget remains. See [[src/mlx_swarm/session.py#Session#load]].

Workspace session loading reconstructs path and profile authority from
`workspace.snapshot.json`, not the live config. Pre-policy snapshots default to
supervised worktree semantics. Completed v3 packets include
applied artifact manifests, strict apply/verification receipts, digest-bound
review/report artifacts, the selected target/policy digest, and the final
base-to-head branch diff. If strict completed evidence is missing or corrupt,
the executor seals a partial diagnostic packet with the finalization error
instead of falsely marking the run completed. See [[workspace-execution]].

An incremental successor additionally snapshots `revision-input.json` and its
digest, inherited predecessor head, depth, and carried completed-task evidence.
It does not copy completed tasks into the successor DAG: the validated Git head
and evidence packet are the carry-forward boundary.
