"""Durable MLX Swarm session and frontier-review persistence."""
# @lat: [[Session]]

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import Plan, TaskDef


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{uuid.uuid4().hex[:8]}"


class Session:
    """A persistent swarm session that accumulates plan state across batches.

    The session stores:
    - The original plan (master LLM's decomposition)
    - Per-task status (pending, running, completed, rejected, failed)
    - Normalized outputs for dependency injection
    - Gate results for inspection
    - Statistics for each batch execution
    """

    def __init__(
        self,
        session_dir: Path,
        plan: Plan,
        *,
        session_id: str | None = None,
        retry_of: str | None = None,
        launch_source: str = "cli",
    ):
        self.dir = session_dir.resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.plan = plan
        self.session_id = session_id or _run_id()
        self.state: dict[str, Any] = {
            "sessionId": self.session_id,
            "planId": plan.plan_id,
            "objective": plan.objective,
            "startedAt": _utc_now(),
            "tasks": {t.id: _initial_task_state(t) for t in plan.tasks},
            "batches": [],
            "status": "pending",
            "reviewStatus": "pending_local",
            "launchSource": launch_source,
        }
        if retry_of:
            self.state["retryOf"] = retry_of
        self._write_plan_snapshot()
        self._save()

    @classmethod
    def load(
        cls,
        session_dir: Path,
        config: "SwarmConfig | None" = None,
    ) -> "Session":
        """Load an existing session from disk."""
        session_dir = session_dir.resolve()
        state_path = session_dir / "session.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        # Reconstruct plan
        from .contracts import (
            SwarmConfig,
            VerificationProfile,
            WorkspaceConfig,
            load_config,
            load_plan,
        )

        if config is None:
            config_path = Path(state["configSource"])
            config = load_config(config_path)
        snapshot_name = state.get("planSnapshot")
        if snapshot_name and (session_dir / snapshot_name).is_file():
            plan_path = session_dir / snapshot_name
        else:
            plan_path = Path(state["planSource"])
        workspace_snapshot = session_dir / "workspace.snapshot.json"
        if workspace_snapshot.is_file():
            workspace_raw = json.loads(
                workspace_snapshot.read_text(encoding="utf-8")
            )
            profiles = {
                identifier: VerificationProfile(
                    identifier=identifier,
                    argv=tuple(value["argv"]),
                    cwd=value["cwd"],
                    timeout_seconds=value["timeoutSeconds"],
                    inherit_env=tuple(value["inheritEnv"]),
                    environment=dict(value["environment"]),
                )
                for identifier, value in workspace_raw.get(
                    "verificationProfiles",
                    {},
                ).items()
            }
            config = replace(
                config,
                schema_version=2,
                workspace=WorkspaceConfig(
                    write_roots=tuple(workspace_raw["writeRoots"]),
                    verification_profiles=profiles,
                ),
            )
        plan = load_plan(plan_path, config)
        obj = cls.__new__(cls)
        obj.dir = session_dir
        obj.plan = plan
        obj.session_id = state["sessionId"]
        obj.state = state
        return obj

    def _write_plan_snapshot(self) -> None:
        if not self.plan.raw:
            return
        snapshot_name = "plan.snapshot.json"
        snapshot_path = self.dir / snapshot_name
        temp_path = self.dir / f"{snapshot_name}.tmp"
        temp_path.write_text(
            json.dumps(self.plan.raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(snapshot_path)
        self.state["planSnapshot"] = snapshot_name

    def set_sources(self, *, config_source: Path, plan_source: Path) -> None:
        self.state["configSource"] = str(config_source.resolve())
        self.state["planSource"] = str(plan_source.resolve())
        self._save()

    def _save(self) -> None:
        path = self.dir / "session.json"
        temp_path = self.dir / "session.json.tmp"
        temp_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)

    def update_task(self, task_id: str, **fields: Any) -> None:
        if task_id not in self.state["tasks"]:
            raise KeyError(f"Unknown task: {task_id}")
        self.state["tasks"][task_id].update(fields)
        self._save()

    def get_task_output(
        self,
        task_id: str,
        *,
        completed_only: bool = True,
    ) -> str | None:
        task = self.state["tasks"].get(task_id)
        if task is None:
            return None
        if completed_only and task.get("status") != "completed":
            return None
        return task.get("normalizedOutput") or task.get("output")

    def get_task_status(self, task_id: str) -> str:
        return self.state["tasks"].get(task_id, {}).get("status", "pending")

    def add_batch_record(self, batch: dict[str, Any]) -> None:
        self.state["batches"].append(batch)
        self._save()

    def set_status(self, status: str) -> None:
        self.state["status"] = status
        if status in {"completed", "partial", "failed"}:
            self.state["finishedAt"] = _utc_now()
            if self.state.get("reviewStatus") in {
                None,
                "pending_local",
                "not_eligible",
                "awaiting_review",
            }:
                self.state["reviewStatus"] = (
                    "awaiting_review"
                    if status == "completed"
                    else "not_eligible"
                )
        else:
            self.state.pop("finishedAt", None)
            if self.state.get("reviewStatus") in {
                None,
                "not_eligible",
                "awaiting_review",
            }:
                self.state["reviewStatus"] = "pending_local"
        self._save()

    def attach_commander(
        self,
        *,
        request_id: str,
        approval: dict[str, Any],
        planning_receipt: dict[str, Any],
        revision_of: str | None = None,
    ) -> None:
        """Attach immutable commander evidence to a newly approved session."""
        from .commander import write_frontier_usage

        self.state["commanderRequestId"] = request_id
        self.state["planApproval"] = approval
        self.state["frontierPlanReceipt"] = "frontier-plan-receipt.json"
        if revision_of is not None:
            self.state["revisionOf"] = revision_of
        _atomic_json(
            self.dir / "frontier-plan-receipt.json",
            planning_receipt,
        )
        write_frontier_usage(self.dir)
        self._save()

    def attach_workspace(
        self,
        snapshot: dict[str, Any],
        *,
        execution_approval: dict[str, Any],
    ) -> None:
        """Attach the immutable Git/worktree authority to a new session."""
        _atomic_json(self.dir / "workspace.snapshot.json", snapshot)
        self.state["workspaceExecution"] = True
        self.state["workspaceSnapshot"] = "workspace.snapshot.json"
        self.state["executionApproval"] = execution_approval
        self.state["workspace"] = {
            "workspaceRoot": snapshot["workspaceRoot"],
            "baseSha": snapshot["baseSha"],
            "headSha": snapshot["headSha"],
            "branch": snapshot["branch"],
            "worktreePath": snapshot["worktreePath"],
            "dirty": snapshot.get("dirty", False),
            "cleanedUp": snapshot.get("cleanedUp", False),
        }
        self._save()

    def workspace_snapshot(self) -> dict[str, Any] | None:
        if not self.state.get("workspaceExecution"):
            return None
        from .workspace import load_workspace_snapshot

        return load_workspace_snapshot(self.dir)

    def update_workspace(self, snapshot: dict[str, Any]) -> None:
        _atomic_json(self.dir / "workspace.snapshot.json", snapshot)
        self.state["workspace"] = {
            "workspaceRoot": snapshot["workspaceRoot"],
            "baseSha": snapshot["baseSha"],
            "headSha": snapshot["headSha"],
            "branch": snapshot["branch"],
            "worktreePath": snapshot["worktreePath"],
            "dirty": snapshot.get("dirty", False),
            "cleanedUp": snapshot.get("cleanedUp", False),
        }
        self._save()

    def summary(self) -> dict[str, Any]:
        tasks = self.state["tasks"]
        return {
            "sessionId": self.session_id,
            "planId": self.plan.plan_id,
            "status": self.state["status"],
            "total": len(tasks),
            "completed": sum(1 for t in tasks.values() if t["status"] == "completed"),
            "rejected": sum(1 for t in tasks.values() if t["status"] == "rejected"),
            "failed": sum(1 for t in tasks.values() if t["status"] == "failed"),
            "blocked": sum(1 for t in tasks.values() if t["status"] == "blocked"),
            "pending": sum(1 for t in tasks.values() if t["status"] == "pending"),
            "awaitingApproval": sum(
                1
                for t in tasks.values()
                if t["status"] == "awaiting_approval"
            ),
            "verificationFailed": sum(
                1
                for t in tasks.values()
                if t["status"] == "verification_failed"
            ),
            "batches": len(self.state["batches"]),
        }

    def export_results(self) -> dict[str, Any]:
        """Export all task results for the orchestrator (master LLM)."""
        return {
            "sessionId": self.session_id,
            "planId": self.plan.plan_id,
            "planSource": str(self.plan.source),
            "objective": self.plan.objective,
            "status": self.state["status"],
            "launchSource": self.state.get("launchSource", "cli"),
            "retryOf": self.state.get("retryOf"),
            "tasks": {
                tid: {
                    "id": tid,
                    "role": t["role"],
                    "artifactType": t.get("artifactType", "report"),
                    "status": t["status"],
                    "gatePassed": t.get("gateResult", {}).get("passed") if t.get("gateResult") else None,
                    "output": (
                        t.get("normalizedOutput") or t.get("output")
                        if t["status"] == "completed"
                        else None
                    ),
                    "violationIds": [
                        v["id"] for v in t.get("gateResult", {}).get("violations", [])
                    ] if t.get("gateResult") else [],
                    "gateResult": t.get("gateResult"),
                    "repairAttempts": t.get("repairAttempts", 0),
                    "artifact": t.get("artifact"),
                    "applyReceipt": t.get("applyReceipt"),
                    "revertReceipt": t.get("revertReceipt"),
                    "verificationResults": t.get("verificationResults", []),
                    "decision": t.get("decision"),
                }
                for tid, t in self.state["tasks"].items()
            },
        }

    def write_frontier_result(self) -> Path:
        """Persist the single compact packet intended for final frontier review."""
        from .commander import canonical_json_sha256, write_frontier_usage

        path = self.dir / "frontier-result.json"
        packet = self.export_results()
        workspace = self.workspace_snapshot()
        packet["schemaVersion"] = 3 if workspace is not None else 2
        packet["reviewMode"] = "frontier-final-only"
        packet["requiresFrontierReview"] = (
            self.state.get("status") == "completed"
        )
        packet["reviewStatus"] = self.state.get(
            "reviewStatus",
            (
                "awaiting_review"
                if self.state.get("status") == "completed"
                else "not_eligible"
            ),
        )
        packet["planSha256"] = canonical_json_sha256(self.plan.raw)
        packet["planContract"] = self.plan.raw
        packet["planApproval"] = self.state.get("planApproval")
        packet["commanderRequestId"] = self.state.get("commanderRequestId")
        packet["revisionOf"] = self.state.get("revisionOf")
        packet["localUsage"] = self.local_usage()
        if workspace is not None:
            from .workspace import final_workspace_diff

            task_definitions = {
                task.id: task for task in self.plan.tasks
            }
            applied_artifacts: list[dict[str, Any]] = []
            verification_receipts: list[dict[str, Any]] = []
            non_mutating_outputs: list[dict[str, Any]] = []
            for task_id, task_state in self.state["tasks"].items():
                task = task_definitions[task_id]
                if (
                    task.mutates_workspace
                    and task_state.get("status") == "completed"
                    and isinstance(task_state.get("applyReceipt"), dict)
                ):
                    applied_artifacts.append({
                        "taskId": task_id,
                        "manifest": task_state.get("artifact"),
                        "applyReceipt": task_state["applyReceipt"],
                    })
                    verification_receipts.extend(
                        task_state.get("verificationResults", [])
                    )
                elif (
                    not task.mutates_workspace
                    and task_state.get("status") == "completed"
                ):
                    non_mutating_outputs.append({
                        "taskId": task_id,
                        "artifactType": task.artifact_type,
                        "output": (
                            task_state.get("normalizedOutput")
                            or task_state.get("output")
                        ),
                    })
            workspace_packet = {
                "workspaceRoot": workspace["workspaceRoot"],
                "baseSha": workspace["baseSha"],
                "headSha": workspace["headSha"],
                "branch": workspace["branch"],
                "dirtyAtLaunch": workspace.get("dirty", False),
                "executionDigest": workspace["executionDigest"],
                "writeRoots": workspace["writeRoots"],
                "cleanedUp": workspace.get("cleanedUp", False),
                "appliedArtifacts": applied_artifacts,
                "verificationReceipts": verification_receipts,
                "nonMutatingOutputs": non_mutating_outputs,
            }
            if self.state.get("status") == "completed":
                final_diff, final_digest = final_workspace_diff(workspace)
                workspace_packet["finalDiff"] = final_diff
                workspace_packet["finalDiffSha256"] = final_digest
            packet["workspace"] = workspace_packet
            packet["executionApproval"] = self.state.get(
                "executionApproval"
            )
        temp_path = self.dir / "frontier-result.json.tmp"
        temp_path.write_text(
            json.dumps(packet, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)
        if (self.dir / "frontier-plan-receipt.json").is_file():
            write_frontier_usage(self.dir)
        return path

    def local_usage(self) -> dict[str, int]:
        prompt_tokens = 0
        generation_tokens = 0
        generation_calls = 0
        model_loads = 0
        for batch in self.state["batches"]:
            statistics = [batch.get("statistics", {})]
            statistics.extend(
                repair.get("statistics", {})
                for repair in batch.get("repairs", [])
            )
            for stats in statistics:
                if not stats or stats.get("batchSize", 0) == 0:
                    continue
                generation_calls += len(stats.get("groups", [])) or 1
                prompt_tokens += int(stats.get("promptTokens", 0))
                generation_tokens += int(stats.get("generationTokens", 0))
                if float(stats.get("loadSeconds", 0.0)) > 0:
                    model_loads += 1
        return {
            "promptTokens": prompt_tokens,
            "generationTokens": generation_tokens,
            "generationCalls": generation_calls,
            "modelLoads": model_loads,
        }


def _initial_task_state(task: TaskDef) -> dict[str, Any]:
    return {
        "id": task.id,
        "role": task.role,
        "artifactType": task.artifact_type,
        "allowedPaths": list(task.allowed_paths),
        "verification": list(task.verification),
        "status": "pending",
        "dependsOn": list(task.depends_on),
        "output": None,
        "normalizedOutput": None,
        "gateResult": None,
        "repairAttempts": 0,
        "batchIndex": None,
    }


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
