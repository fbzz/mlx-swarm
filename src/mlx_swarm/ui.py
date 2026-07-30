"""Localhost-only HTTP API and static MLX Swarm cockpit."""
# @lat: [[UI]]

from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .backend import _resolve_model_path
from .commander import (
    CommanderError,
    CommanderStore,
    PlanApproval,
    canonical_json_sha256,
    validate_frontier_receipt,
)
from .contracts import (
    ContractError,
    OutputGate,
    Plan,
    SwarmConfig,
    TaskDef,
    load_plan,
    worker_capabilities_payload,
)
from .evaluation import EvaluationError, EvaluationStore
from .session import Session, _run_id, _utc_now
from .model_identity import model_metadata
from .workspace import (
    WorkspaceError,
    cleanup_session_worktree,
    cleanup_worktree,
    execution_policy,
    execution_preview,
    execution_previews,
    load_artifact,
    load_workspace_snapshot,
    prepare_workspace,
    submit_artifact_decision,
    workspace_readiness,
)

MAX_REQUEST_BYTES = 16_384
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class APIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class CockpitApp:
    """Read local plans/sessions and launch bounded CLI subprocesses."""

    def __init__(
        self,
        config: SwarmConfig,
        plans_dir: Path,
        *,
        popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
    ):
        self.config = config
        self.plans_dir = plans_dir.resolve()
        self.artifacts_dir = config.artifacts_dir.resolve()
        self.commander = CommanderStore(config)
        self.evaluations = EvaluationStore(config)
        self.popen_factory = popen_factory
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._lock = threading.Lock()
        self._commander_lock = threading.Lock()
        if not self.plans_dir.is_dir():
            raise RuntimeError(f"Plans directory not found: {self.plans_dir}")
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def status_payload(self) -> dict[str, Any]:
        try:
            model_path = _resolve_model_path(self.config)
            model_ready = True
            model_error = None
            metadata = model_metadata(
                model_path,
                declared_context_tokens=(
                    self.config.worker.capabilities.context_window_tokens
                ),
            )
            if metadata.get("contextCompatible") is False:
                model_ready = False
                model_error = metadata["warnings"][0]
        except Exception as exc:
            model_path = None
            model_ready = False
            model_error = str(exc)
            metadata = None
        workspace = workspace_readiness(self.config)
        ready = model_ready and (
            not workspace.get("enabled")
            or workspace.get("ready") is True
        )
        return {
            "ready": ready,
            "model": {
                "repository": self.config.model.repository,
                "path": str(model_path) if model_path else None,
                "error": model_error,
                "metadata": metadata,
            },
            "batch": {
                "maxWorkers": self.config.batch.max_workers,
                "prefillStepSize": self.config.batch.prefill_step_size,
                "maxPromptCharacters": self.config.batch.max_prompt_characters,
                "maxBatchPromptTokens": (
                    self.config.batch.max_batch_prompt_tokens
                ),
            },
            "worker": {
                "mode": self.config.worker.mode,
                "reasoningMaxTokens": (
                    self.config.worker.reasoning_max_tokens
                ),
                "capabilities": worker_capabilities_payload(
                    self.config.worker.capabilities
                ),
            },
            "plansDir": str(self.plans_dir),
            "artifactsDir": str(self.artifacts_dir),
            "evaluationsDir": str(self.evaluations.root),
            "commanderRoot": str(self.commander.requests_root),
            "workspaceRoot": (
                workspace.get("workspaceRoot")
                or str(self.commander.workspace_root)
            ),
            "workspace": workspace,
            "brand": "MLX Swarm",
            "reviewMode": "frontier-final-only",
        }

    def commander_requests_payload(self) -> dict[str, Any]:
        return {"requests": self.commander.list_requests()}

    def evaluations_payload(self) -> dict[str, Any]:
        return {"evaluations": self.evaluations.list()}

    def evaluation_detail(self, evaluation_id: str) -> dict[str, Any]:
        _validate_identifier(evaluation_id, "evaluationId")
        try:
            return self.evaluations.detail(evaluation_id)
        except EvaluationError as exc:
            raise APIError(HTTPStatus.NOT_FOUND, str(exc)) from exc

    def commander_request_detail(self, request_id: str) -> dict[str, Any]:
        _validate_identifier(request_id, "requestId")
        try:
            return self.commander.request_detail(request_id)
        except CommanderError as exc:
            status = (
                HTTPStatus.NOT_FOUND
                if "not found" in str(exc).lower()
                else HTTPStatus.CONFLICT
            )
            raise APIError(status, str(exc)) from exc

    def create_commander_request(
        self,
        objective: str,
        constraints: Any,
        revision_of: str | None,
    ) -> dict[str, Any]:
        if constraints is None:
            constraints = []
        if not isinstance(constraints, list) or any(
            not isinstance(value, str) for value in constraints
        ):
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "constraints must be an array of strings.",
            )
        if revision_of is not None and not isinstance(revision_of, str):
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "revisionOf must be a run reference.",
            )
        try:
            return self.commander.create_request(
                objective,
                constraints,
                revision_of=revision_of,
            )
        except CommanderError as exc:
            raise APIError(HTTPStatus.BAD_REQUEST, str(exc)) from exc

    def plans_payload(self) -> dict[str, Any]:
        valid, invalid = self._plan_catalog()
        return {
            "plans": [
                _serialize_plan(
                    plan,
                    path,
                    _safe_execution_preview(self.config, plan),
                    _safe_execution_previews(self.config, plan),
                )
                for plan, path in sorted(
                    valid.values(),
                    key=lambda item: item[0].plan_id,
                )
            ],
            "invalid": invalid,
        }

    def runs_payload(self) -> dict[str, Any]:
        runs: list[dict[str, Any]] = []
        if self.artifacts_dir.is_dir():
            for candidate in self.artifacts_dir.glob("*/*/session.json"):
                state_path = candidate.resolve()
                if not _is_within(state_path, self.artifacts_dir):
                    continue
                try:
                    state = _read_json_file(state_path)
                except (OSError, ValueError):
                    continue
                runs.append(
                    _run_summary(
                        state,
                        state_path.parent,
                        self._runner_active(state),
                    )
                )
        runs.sort(
            key=lambda run: run.get("startedAt") or "",
            reverse=True,
        )
        return {"runs": runs}

    def run_detail(self, plan_id: str, session_id: str) -> dict[str, Any]:
        session_dir, state = self._load_run_state(plan_id, session_id)
        plan = self._load_historical_plan(session_dir, state)
        if plan is not None:
            plan_payload = _serialize_plan(
                plan,
                plan.source,
                (
                    state.get("executionApproval")
                    if plan.workspace_execution
                    else None
                ),
            )
            levels = [
                [task.id for task in level]
                for level in plan.topological_order()
            ]
        else:
            plan_payload = {
                "planId": state.get("planId"),
                "objective": state.get("objective", ""),
                "source": state.get("planSource"),
                "tasks": [
                    {
                        "id": task_id,
                        "role": task.get("role", "general"),
                        "artifactType": task.get("artifactType", "report"),
                        "allowedPaths": task.get("allowedPaths", []),
                        "verification": task.get("verification", []),
                        "prompt": "",
                        "dependsOn": task.get("dependsOn", []),
                        "maxRepairAttempts": None,
                        "outputProtocol": "",
                        "gate": None,
                    }
                    for task_id, task in state.get("tasks", {}).items()
                ],
            }
            levels = _levels_from_state(state)

        frontier_result = None
        frontier_path = session_dir / "frontier-result.json"
        if frontier_path.is_file():
            try:
                frontier_result = _read_json_file(frontier_path)
            except (OSError, ValueError):
                frontier_result = None

        review_detail = self.commander.review_detail(session_dir)
        planning_receipt = None
        planning_receipt_path = session_dir / "frontier-plan-receipt.json"
        if planning_receipt_path.is_file():
            try:
                planning_receipt = _read_json_file(planning_receipt_path)
            except (OSError, ValueError):
                planning_receipt = None

        active = self._runner_active(state)
        summary = _run_summary(state, session_dir, active)
        workspace_payload = state.get("workspace")
        if state.get("workspaceExecution"):
            try:
                snapshot = load_workspace_snapshot(session_dir)
                workspace_payload = {
                    **snapshot,
                    "executionApproval": state.get("executionApproval"),
                }
            except WorkspaceError:
                pass
        artifacts = _serialize_artifacts(
            session_dir,
            state,
            approval_mode=state.get("approvalMode", "supervised"),
        )
        return {
            "run": summary,
            "plan": plan_payload,
            "levels": levels,
            "tasks": state.get("tasks", {}),
            "artifacts": artifacts,
            "workspace": workspace_payload,
            "retryExecutionPreview": (
                _safe_execution_preview(
                    self.config,
                    plan,
                    approval_mode=state.get(
                        "approvalMode",
                        "supervised",
                    ),
                    workspace_target=state.get(
                        "workspaceTarget",
                        "worktree",
                    ),
                )
                if plan is not None and plan.workspace_execution
                else None
            ),
            "batches": state.get("batches", []),
            "localUsage": (
                frontier_result.get("localUsage", {})
                if frontier_result
                else _local_usage(state.get("batches", []))
            ),
            "localExecutionProfile": state.get("localExecutionProfile"),
            "frontierResult": frontier_result,
            "frontierUsage": review_detail["frontierUsage"],
            "frontierReview": review_detail["review"],
            "frontierReviewReceipt": review_detail["reviewReceipt"],
            "frontierPlanReceipt": planning_receipt,
            "reviewStatus": review_detail["reviewStatus"],
            "reviewError": review_detail["reviewError"],
            "commander": {
                "requestId": state.get("commanderRequestId"),
                "approval": state.get("planApproval"),
                "revisionOf": state.get("revisionOf"),
                **review_detail["handoff"],
            },
            "actions": {
                "resume": (
                    not active
                    and (
                        state.get("status") in {
                            "pending",
                            "running",
                            "awaiting_approval",
                        }
                        or _recoverable_partial(state)
                    )
                ),
                "retry": state.get("status") in {"partial", "failed"},
                "review": (
                    not active
                    and state.get("status") == "completed"
                    and review_detail["reviewStatus"] == "awaiting_review"
                ),
                "cleanupWorkspace": (
                    not active
                    and state.get("workspaceExecution") is True
                    and state.get("status") in {
                        "completed",
                        "partial",
                        "failed",
                    }
                    and not (workspace_payload or {}).get("cleanedUp", False)
                    and state.get("workspaceTarget", "worktree")
                    == "worktree"
                    and not _recoverable_partial(state)
                ),
            },
            "runnerLogAvailable": (session_dir / "runner.log").is_file(),
        }

    def launch_run(
        self,
        plan_id: str,
        max_repair: int,
        *,
        retry_of: str | None = None,
        plan_override: tuple[Plan, Path] | None = None,
        commander_evidence: dict[str, Any] | None = None,
        mark_commander_launched: bool = False,
        plan_digest: str | None = None,
        execution_digest: str | None = None,
        approval_mode: str = "supervised",
        workspace_target: str = "worktree",
    ) -> dict[str, Any]:
        if not isinstance(max_repair, int) or isinstance(max_repair, bool):
            raise APIError(HTTPStatus.BAD_REQUEST, "maxRepair must be an integer.")
        if not 0 <= max_repair <= 5:
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "maxRepair must be between 0 and 5.",
            )
        if (
            not isinstance(approval_mode, str)
            or approval_mode not in {"supervised", "yolo"}
        ):
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "approvalMode must be supervised or yolo.",
            )
        if (
            not isinstance(workspace_target, str)
            or workspace_target not in {"worktree", "checkout"}
        ):
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "workspaceTarget must be worktree or checkout.",
            )

        if plan_override is None:
            valid, _ = self._plan_catalog()
            if plan_id not in valid:
                raise APIError(
                    HTTPStatus.NOT_FOUND,
                    f"Unknown or invalid plan: {plan_id}",
                )
            plan, plan_path = valid[plan_id]
        else:
            plan, plan_path = plan_override

        if commander_evidence is not None:
            try:
                validated_receipt = validate_frontier_receipt(
                    commander_evidence["planningReceipt"],
                    expected_phase="plan",
                    expected_artifact_sha256=canonical_json_sha256(
                        plan.raw
                    ),
                )
                revision_input = commander_evidence.get("revisionInput")
                revision_digest = commander_evidence.get(
                    "revisionInputSha256"
                )
                revision_authority = commander_evidence.get(
                    "revisionAuthority"
                )
                if revision_input is None:
                    if (
                        revision_digest is not None
                        or revision_authority is not None
                    ):
                        raise CommanderError(
                            "Incremental revision evidence is incomplete."
                        )
                else:
                    actual_revision_digest = canonical_json_sha256(
                        revision_input
                    )
                    expected_authority = {
                        "revisionOf": commander_evidence.get(
                            "revisionOf"
                        ),
                        "revisionInputSha256": actual_revision_digest,
                        "predecessorExecutionDigest": revision_input.get(
                            "predecessorExecutionDigest"
                        ),
                        "predecessorBranch": revision_input.get(
                            "predecessorBranch"
                        ),
                        "baseSha": revision_input.get("baseSha"),
                    }
                    if (
                        revision_digest != actual_revision_digest
                        or not isinstance(revision_authority, dict)
                        or any(
                            revision_authority.get(key) != value
                            for key, value in expected_authority.items()
                        )
                    ):
                        raise CommanderError(
                            "Incremental revision authority is invalid."
                        )
                commander_evidence = {
                    **commander_evidence,
                    "planningReceipt": validated_receipt,
                }
            except (CommanderError, KeyError, TypeError) as exc:
                raise APIError(
                    HTTPStatus.CONFLICT,
                    f"Commander launch evidence is invalid: {exc}",
                ) from exc

        run_id = _run_id()
        session_dir = self.artifacts_dir / plan.plan_id / run_id
        workspace_snapshot = None
        execution_approval = None
        if not plan.workspace_execution and (
            approval_mode != "supervised"
            or workspace_target != "worktree"
        ):
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "YOLO and workspace targets require a schema-v2 workspace plan.",
            )
        if plan.workspace_execution:
            actual_plan_digest = canonical_json_sha256(plan.raw)
            approved_plan_digest = (
                plan_digest
                or (
                    commander_evidence["approval"].plan_sha256
                    if commander_evidence is not None
                    else None
                )
            )
            if approved_plan_digest != actual_plan_digest:
                raise APIError(
                    HTTPStatus.CONFLICT,
                    "Plan digest mismatch; refresh the displayed plan.",
                )
            approved_execution_digest = (
                execution_digest
                or (
                    commander_evidence["approval"].execution_digest
                    if commander_evidence is not None
                    else None
                )
            )
            if not isinstance(approved_execution_digest, str):
                raise APIError(
                    HTTPStatus.CONFLICT,
                    "Workspace plans require the displayed execution digest.",
                )
            try:
                workspace_snapshot = prepare_workspace(
                    self.config,
                    plan,
                    session_id=run_id,
                    expected_execution_digest=approved_execution_digest,
                    approval_mode=approval_mode,
                    workspace_target=workspace_target,
                    revision_authority=(
                        commander_evidence.get("revisionAuthority")
                        if commander_evidence is not None
                        else None
                    ),
                )
            except WorkspaceError as exc:
                raise APIError(HTTPStatus.CONFLICT, str(exc)) from exc
            execution_approval = {
                "schemaVersion": 1,
                "planSha256": actual_plan_digest,
                "executionDigest": approved_execution_digest,
                "workspaceRoot": workspace_snapshot["workspaceRoot"],
                "baseSha": workspace_snapshot["baseSha"],
                "approvalMode": approval_mode,
                "workspaceTarget": workspace_target,
                "executionPolicySha256": workspace_snapshot[
                    "executionPolicySha256"
                ],
                "approvedAt": _utc_now(),
                "source": (
                    "commander"
                    if commander_evidence is not None
                    else "cockpit"
                ),
            }
            if commander_evidence is not None:
                previous = commander_evidence["approval"]
                commander_evidence = {
                    **commander_evidence,
                    "approval": PlanApproval(
                        request_id=previous.request_id,
                        plan_sha256=previous.plan_sha256,
                        approved_at=_utc_now(),
                        source=previous.source,
                        execution_digest=approved_execution_digest,
                        workspace_root=workspace_snapshot["workspaceRoot"],
                        base_sha=workspace_snapshot["baseSha"],
                        approval_mode=approval_mode,
                        workspace_target=workspace_target,
                        execution_policy_sha256=workspace_snapshot[
                            "executionPolicySha256"
                        ],
                    ),
                }
        session = None
        try:
            session = Session(
                session_dir,
                plan,
                session_id=run_id,
                retry_of=retry_of,
                launch_source=(
                    "commander"
                    if commander_evidence is not None
                    else "ui"
                ),
            )
            session.set_sources(
                config_source=self.config.source,
                plan_source=plan_path,
            )
            session.state["maxRepair"] = max_repair
            session._save()
            if workspace_snapshot is not None:
                assert execution_approval is not None
                session.attach_workspace(
                    workspace_snapshot,
                    execution_approval=execution_approval,
                )
            if commander_evidence is not None:
                approval = commander_evidence["approval"]
                session.attach_commander(
                    request_id=commander_evidence["requestId"],
                    approval=approval.to_json(),
                    planning_receipt=commander_evidence[
                        "planningReceipt"
                    ],
                    revision_of=commander_evidence.get("revisionOf"),
                    revision_input=commander_evidence.get(
                        "revisionInput"
                    ),
                    revision_input_sha256=commander_evidence.get(
                        "revisionInputSha256"
                    ),
                )
                if mark_commander_launched:
                    self.commander.mark_launched(
                        commander_evidence["requestId"],
                        approval,
                        plan_id=plan.plan_id,
                        session_id=run_id,
                    )
        except Exception as exc:
            if session is not None:
                session.state["launchError"] = str(exc)
                session.state["pauseReason"] = (
                    "launch_evidence_attachment_failed"
                )
                session.set_status("failed")
            if (
                workspace_snapshot is not None
                and workspace_target == "worktree"
            ):
                try:
                    cleanup_snapshot = (
                        load_workspace_snapshot(session_dir)
                        if (session_dir / "workspace.snapshot.json").is_file()
                        else workspace_snapshot
                    )
                    cleanup_worktree(cleanup_snapshot)
                    if session is not None:
                        session.update_workspace(cleanup_snapshot)
                except WorkspaceError:
                    pass
            raise
        assert session is not None
        self._spawn(
            session,
            [
                sys.executable,
                "-m",
                "mlx_swarm.cli",
                "--config",
                str(self.config.source),
                "run",
                str(session_dir / "plan.snapshot.json"),
                "--session-dir",
                str(session_dir),
                "--max-repair",
                str(max_repair),
            ],
        )
        return self.run_detail(plan.plan_id, run_id)

    def approve_commander_run(
        self,
        request_id: str,
        plan_digest: str,
        max_repair: int,
        execution_digest: str | None = None,
        approval_mode: str = "supervised",
        workspace_target: str = "worktree",
    ) -> dict[str, Any]:
        _validate_identifier(request_id, "requestId")
        if (
            not isinstance(max_repair, int)
            or isinstance(max_repair, bool)
            or not 0 <= max_repair <= 5
        ):
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "maxRepair must be an integer between 0 and 5.",
            )
        with self._commander_lock:
            try:
                plan, plan_path, approval, receipt, request = (
                    self.commander.approved_plan(
                        request_id,
                        plan_digest,
                        source="cockpit",
                        execution_digest=execution_digest,
                        approval_mode=approval_mode,
                        workspace_target=workspace_target,
                    )
                )
            except CommanderError as exc:
                raise APIError(HTTPStatus.CONFLICT, str(exc)) from exc
            return self.launch_run(
                plan.plan_id,
                max_repair,
                plan_override=(plan, plan_path),
                commander_evidence={
                    "requestId": request_id,
                    "approval": approval,
                    "planningReceipt": receipt,
                    "revisionOf": request.get("revisionOf"),
                    "revisionInput": self.commander.revision_input(
                        request_id
                    ),
                    "revisionInputSha256": request.get(
                        "revisionInputSha256"
                    ),
                    "revisionAuthority": self.commander.revision_authority(
                        request_id
                    ),
                },
                mark_commander_launched=True,
                plan_digest=plan_digest,
                execution_digest=execution_digest,
                approval_mode=approval_mode,
                workspace_target=workspace_target,
            )

    def resume_run(self, plan_id: str, session_id: str) -> dict[str, Any]:
        session_dir, state = self._load_run_state(plan_id, session_id)
        if self._runner_active(state):
            raise APIError(HTTPStatus.CONFLICT, "Run is already active.")
        if state.get("launchSource") == "commander":
            request_id = state.get("commanderRequestId")
            if (
                not isinstance(request_id, str)
                or not isinstance(state.get("planApproval"), dict)
                or not (
                    session_dir / "frontier-plan-receipt.json"
                ).is_file()
            ):
                raise APIError(
                    HTTPStatus.CONFLICT,
                    "Commander evidence attachment is incomplete; this run "
                    "cannot be resumed.",
                )
            try:
                request = self.commander.request_detail(request_id)[
                    "request"
                ]
            except CommanderError as exc:
                raise APIError(
                    HTTPStatus.CONFLICT,
                    f"Commander evidence is invalid: {exc}",
                ) from exc
            request_ref = request.get("sessionRef")
            current_ref = f"{plan_id}/{session_id}"
            bound_to_request = request_ref == current_ref
            cursor_state = state
            seen: set[str] = set()
            while (
                not bound_to_request
                and isinstance(cursor_state.get("retryOf"), str)
                and len(seen) < 128
            ):
                parent_ref = cursor_state["retryOf"]
                if parent_ref == request_ref:
                    bound_to_request = True
                    break
                if parent_ref in seen or parent_ref.count("/") != 1:
                    break
                seen.add(parent_ref)
                parent_plan, parent_session = parent_ref.split("/", 1)
                try:
                    _parent_dir, parent_state = self._load_run_state(
                        parent_plan,
                        parent_session,
                    )
                except APIError:
                    break
                if (
                    parent_state.get("commanderRequestId")
                    != request_id
                ):
                    break
                cursor_state = parent_state
            if (
                request.get("status") != "launched"
                or not bound_to_request
            ):
                raise APIError(
                    HTTPStatus.CONFLICT,
                    "Commander request is not durably bound to this run.",
                )
            if state.get("revisionInputSha256") is not None and (
                not (session_dir / "revision-input.json").is_file()
                or not (session_dir / "workspace.snapshot.json").is_file()
            ):
                raise APIError(
                    HTTPStatus.CONFLICT,
                    "Incremental revision authority is incomplete; this run "
                    "cannot be resumed.",
                )
        if state.get("status") not in {
            "pending",
            "running",
            "awaiting_approval",
        } and not _recoverable_partial(state):
            raise APIError(
                HTTPStatus.CONFLICT,
                "Only interrupted or pending runs can be resumed.",
            )
        session = Session.load(session_dir, self.config)
        max_repair = state.get("maxRepair", 0)
        if not isinstance(max_repair, int) or isinstance(max_repair, bool):
            max_repair = 0
        self._spawn(
            session,
            [
                sys.executable,
                "-m",
                "mlx_swarm.cli",
                "--config",
                str(self.config.source),
                "resume",
                str(session_dir),
                "--max-repair",
                str(max_repair),
            ],
        )
        return self.run_detail(plan_id, session_id)

    def retry_run(
        self,
        plan_id: str,
        session_id: str,
        max_repair: int,
        *,
        execution_digest: str | None = None,
        approval_mode: str | None = None,
        workspace_target: str | None = None,
    ) -> dict[str, Any]:
        session_dir, state = self._load_run_state(plan_id, session_id)
        if state.get("status") not in {"partial", "failed"}:
            raise APIError(
                HTTPStatus.CONFLICT,
                "Only partial or failed runs can be retried.",
            )
        plan = self._load_historical_plan(session_dir, state)
        if plan is None:
            raise APIError(
                HTTPStatus.CONFLICT,
                "The original plan is unavailable for retry.",
            )
        plan_path = plan.source
        selected_approval_mode = (
            approval_mode
            or state.get("approvalMode")
            or "supervised"
        )
        selected_workspace_target = (
            workspace_target
            or state.get("workspaceTarget")
            or "worktree"
        )
        commander_evidence = None
        receipt_path = session_dir / "frontier-plan-receipt.json"
        approval_raw = state.get("planApproval")
        request_id = state.get("commanderRequestId")
        if (
            receipt_path.is_file()
            and isinstance(approval_raw, dict)
            and isinstance(request_id, str)
        ):
            try:
                approval = PlanApproval(
                    request_id=approval_raw["requestId"],
                    plan_sha256=approval_raw["planSha256"],
                    approved_at=approval_raw["approvedAt"],
                    source=approval_raw.get("source", "cockpit"),
                    execution_digest=approval_raw.get("executionDigest"),
                    workspace_root=approval_raw.get("workspaceRoot"),
                    base_sha=approval_raw.get("baseSha"),
                    approval_mode=approval_raw.get("approvalMode"),
                    workspace_target=approval_raw.get("workspaceTarget"),
                    execution_policy_sha256=approval_raw.get(
                        "executionPolicySha256"
                    ),
                )
                commander_evidence = {
                    "requestId": request_id,
                    "approval": approval,
                    "planningReceipt": _read_json_file(receipt_path),
                    "revisionOf": state.get("revisionOf"),
                    "revisionInput": self.commander.revision_input(
                        request_id
                    ),
                    "revisionInputSha256": state.get(
                        "revisionInputSha256"
                    ),
                    "revisionAuthority": self.commander.revision_authority(
                        request_id
                    ),
                }
            except CommanderError as exc:
                raise APIError(
                    HTTPStatus.CONFLICT,
                    "Incremental commander evidence is invalid: "
                    f"{exc}",
                ) from exc
            except (KeyError, OSError, ValueError) as exc:
                if state.get("revisionInputSha256") is not None:
                    raise APIError(
                        HTTPStatus.CONFLICT,
                        "Incremental commander evidence is incomplete.",
                    ) from exc
                commander_evidence = None
        if (
            state.get("revisionInputSha256") is not None
            and commander_evidence is None
        ):
            raise APIError(
                HTTPStatus.CONFLICT,
                "Incremental revisions cannot be retried without their "
                "commander and revision authority.",
            )
        return self.launch_run(
            plan.plan_id,
            max_repair,
            retry_of=f"{plan_id}/{session_id}",
            plan_override=(plan, plan_path),
            commander_evidence=commander_evidence,
            plan_digest=canonical_json_sha256(plan.raw),
            execution_digest=execution_digest,
            approval_mode=selected_approval_mode,
            workspace_target=selected_workspace_target,
        )

    def artifact_action(
        self,
        plan_id: str,
        session_id: str,
        task_id: str,
        *,
        action: str,
        artifact_digest: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        _validate_identifier(task_id, "taskId")
        session_dir, state = self._load_run_state(plan_id, session_id)
        task_state = state.get("tasks", {}).get(task_id)
        if not isinstance(task_state, dict):
            raise APIError(HTTPStatus.NOT_FOUND, "Task not found.")
        status = task_state.get("status")
        allowed = {
            "apply": {"awaiting_approval"},
            "reject": {"awaiting_approval", "verification_failed"},
            "verify": {"verification_failed"},
        }
        if action not in allowed or status not in allowed[action]:
            raise APIError(
                HTTPStatus.CONFLICT,
                f"Artifact action {action!r} is not available from {status!r}.",
            )
        if not isinstance(reason, (str, type(None))):
            raise APIError(HTTPStatus.BAD_REQUEST, "reason must be a string.")
        try:
            submit_artifact_decision(
                session_dir,
                task_id,
                action=action,
                artifact_sha256=artifact_digest,
                source="cockpit",
                reason=reason,
            )
        except WorkspaceError as exc:
            raise APIError(HTTPStatus.CONFLICT, str(exc)) from exc
        refreshed = _read_json_file(session_dir / "session.json")
        if not self._runner_active(refreshed):
            self.resume_run(plan_id, session_id)
        return self.run_detail(plan_id, session_id)

    def cleanup_run_workspace(
        self,
        plan_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        session_dir, state = self._load_run_state(plan_id, session_id)
        if self._runner_active(state):
            raise APIError(HTTPStatus.CONFLICT, "Run is still active.")
        if state.get("status") not in {"completed", "partial", "failed"}:
            raise APIError(
                HTTPStatus.CONFLICT,
                "Only terminal-run worktrees can be cleaned up.",
            )
        try:
            snapshot = load_workspace_snapshot(session_dir)
            if snapshot.get("cleanedUp"):
                raise WorkspaceError("Session worktree was already cleaned up.")
            cleanup_session_worktree(
                session_dir,
                snapshot,
                task_states=state.get("tasks", {}),
                pause_reason=state.get("pauseReason"),
            )
            session = Session.load(session_dir, self.config)
            session.update_workspace(snapshot)
        except WorkspaceError as exc:
            raise APIError(HTTPStatus.CONFLICT, str(exc)) from exc
        return self.run_detail(plan_id, session_id)

    def _plan_catalog(
        self,
    ) -> tuple[dict[str, tuple[Plan, Path]], list[dict[str, str]]]:
        by_id: dict[str, list[tuple[Plan, Path]]] = {}
        invalid: list[dict[str, str]] = []
        config_source = self.config.source.resolve()
        model_root: Path | None = None
        if self.config.model.local_path:
            model_root = Path(self.config.model.local_path)
            if not model_root.is_absolute():
                model_root = config_source.parent / model_root
            model_root = model_root.resolve()
        for path in sorted(self.plans_dir.rglob("*.json")):
            resolved = path.resolve()
            if (
                resolved == config_source
                or _is_within(resolved, self.artifacts_dir)
                or (
                    model_root is not None
                    and _is_within(resolved, model_root)
                )
                or not _is_within(resolved, self.plans_dir)
            ):
                continue
            try:
                plan = load_plan(resolved, self.config)
            except ContractError as exc:
                invalid.append({
                    "path": str(resolved.relative_to(self.plans_dir)),
                    "error": str(exc),
                })
                continue
            by_id.setdefault(plan.plan_id, []).append((plan, resolved))

        valid: dict[str, tuple[Plan, Path]] = {}
        for plan_id, matches in by_id.items():
            if len(matches) == 1:
                valid[plan_id] = matches[0]
                continue
            paths = ", ".join(
                str(path.relative_to(self.plans_dir))
                for _, path in matches
            )
            invalid.append({
                "path": paths,
                "error": f"Duplicate planId {plan_id!r}.",
            })
        return valid, invalid

    def _load_run_state(
        self,
        plan_id: str,
        session_id: str,
    ) -> tuple[Path, dict[str, Any]]:
        _validate_identifier(plan_id, "planId")
        _validate_identifier(session_id, "sessionId")
        session_dir = (
            self.artifacts_dir / plan_id / session_id
        ).resolve()
        if not _is_within(session_dir, self.artifacts_dir):
            raise APIError(HTTPStatus.BAD_REQUEST, "Invalid run path.")
        state_path = session_dir / "session.json"
        if not state_path.is_file():
            raise APIError(HTTPStatus.NOT_FOUND, "Run not found.")
        try:
            state = _read_json_file(state_path)
        except (OSError, ValueError) as exc:
            raise APIError(
                HTTPStatus.CONFLICT,
                f"Run state is unreadable: {exc}",
            ) from exc
        if (
            state.get("planId") != plan_id
            or state.get("sessionId") != session_id
        ):
            raise APIError(HTTPStatus.CONFLICT, "Run identity mismatch.")
        return session_dir, state

    def _load_historical_plan(
        self,
        session_dir: Path,
        state: dict[str, Any],
    ) -> Plan | None:
        snapshot = state.get("planSnapshot")
        if snapshot:
            snapshot_path = (session_dir / snapshot).resolve()
            if _is_within(snapshot_path, session_dir) and snapshot_path.is_file():
                try:
                    return load_plan(snapshot_path, self.config)
                except ContractError:
                    pass
        source_value = state.get("planSource")
        if not source_value:
            return None
        source = Path(source_value).resolve()
        if not _is_within(source, self.plans_dir) or not source.is_file():
            return None
        try:
            return load_plan(source, self.config)
        except ContractError:
            return None

    def _spawn(self, session: Session, argv: list[str]) -> None:
        log_path = session.dir / "runner.log"
        session.state["runnerStartedAt"] = _utc_now()
        session._save()
        with log_path.open("ab", buffering=0) as log_file:
            process = self.popen_factory(
                argv,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        runner_path = session.dir / "runner.json"
        runner_temp = session.dir / "runner.json.tmp"
        runner_temp.write_text(
            json.dumps({
                "pid": process.pid,
                "startedAt": session.state["runnerStartedAt"],
                "argv": argv,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        runner_temp.replace(runner_path)
        key = f"{session.plan.plan_id}/{session.session_id}"
        with self._lock:
            self._processes[key] = process

    def _runner_active(self, state: dict[str, Any]) -> bool:
        key = f"{state.get('planId')}/{state.get('sessionId')}"
        if state.get("status") in {"completed", "partial", "failed"}:
            with self._lock:
                self._processes.pop(key, None)
            return False
        with self._lock:
            process = self._processes.get(key)
            if process is not None:
                if process.poll() is None:
                    return True
                self._processes.pop(key, None)
        pid = state.get("runnerPid")
        if not isinstance(pid, int):
            plan_id = state.get("planId")
            session_id = state.get("sessionId")
            if (
                isinstance(plan_id, str)
                and isinstance(session_id, str)
                and _SAFE_ID.fullmatch(plan_id)
                and _SAFE_ID.fullmatch(session_id)
            ):
                runner_path = (
                    self.artifacts_dir / plan_id / session_id / "runner.json"
                ).resolve()
                if _is_within(runner_path, self.artifacts_dir):
                    try:
                        pid = _read_json_file(runner_path).get("pid")
                    except (OSError, ValueError):
                        pid = None
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True


def _serialize_plan(
    plan: Plan,
    source: Path,
    execution: dict[str, Any] | None = None,
    previews: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = {
        "schemaVersion": plan.schema_version,
        "planId": plan.plan_id,
        "objective": plan.objective,
        "context": plan.raw.get("context"),
        "integrationVerification": list(plan.integration_verification),
        "source": str(source),
        "digest": canonical_json_sha256(plan.raw),
        "tasks": [_serialize_task(task) for task in plan.tasks],
    }
    if execution is not None:
        value["execution"] = execution
    if previews is not None:
        value["executionPreviews"] = previews
    return value


def _serialize_task(task: TaskDef) -> dict[str, Any]:
    return {
        "id": task.id,
        "role": task.role,
        "artifactType": task.artifact_type,
        "workerOutputProtocol": task.worker_output_protocol,
        "executionMode": task.execution_mode,
        "contextRefs": (
            list(task.context_refs)
            if task.context_refs is not None
            else None
        ),
        "interfaceContract": task.interface_contract,
        "expectedOutputTokens": task.expected_output_tokens,
        "allowedPaths": list(task.allowed_paths),
        "verification": list(task.verification),
        "prompt": task.prompt,
        "dependsOn": list(task.depends_on),
        "maxRepairAttempts": task.max_repair_attempts,
        "outputProtocol": task.output_protocol,
        "generationOverride": task.generation_override,
        "gate": _serialize_gate(task.gate),
    }


def _safe_execution_preview(
    config: SwarmConfig,
    plan: Plan,
    *,
    approval_mode: str = "supervised",
    workspace_target: str = "worktree",
) -> dict[str, Any] | None:
    if not plan.workspace_execution:
        return None
    try:
        return execution_preview(
            config,
            plan,
            approval_mode=approval_mode,
            workspace_target=workspace_target,
        )
    except WorkspaceError as exc:
        return {
            "ready": False,
            "executionPolicy": execution_policy(
                approval_mode=approval_mode,
                workspace_target=workspace_target,
            ),
            "error": str(exc),
        }


def _safe_execution_previews(
    config: SwarmConfig,
    plan: Plan,
) -> dict[str, Any] | None:
    if not plan.workspace_execution:
        return None
    return execution_previews(config, plan)


def _serialize_artifacts(
    session_dir: Path,
    state: dict[str, Any],
    *,
    approval_mode: str = "supervised",
) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for task_id, task_state in state.get("tasks", {}).items():
        manifest = task_state.get("artifact")
        if not isinstance(manifest, dict):
            continue
        try:
            persisted, payload = load_artifact(session_dir, task_id)
        except WorkspaceError:
            persisted, payload = manifest, ""
        verification = task_state.get("verificationResults", [])
        enriched_verification: list[dict[str, Any]] = []
        for result in verification:
            item = dict(result)
            output = item.get("output")
            if isinstance(output, str):
                log_path = (session_dir / output).resolve()
                if _is_within(log_path, session_dir) and log_path.is_file():
                    raw = log_path.read_bytes()[:200_000]
                    item["log"] = raw.decode("utf-8", "replace")
                    item["logDisplayTruncated"] = log_path.stat().st_size > len(raw)
            enriched_verification.append(item)
        status = task_state.get("status")
        artifacts[task_id] = {
            "manifest": persisted,
            "payload": payload,
            "status": status,
            "decision": task_state.get("decision"),
            "applyReceipt": task_state.get("applyReceipt"),
            "revertReceipt": task_state.get("revertReceipt"),
            "verification": enriched_verification,
            "actions": {
                "apply": (
                    status == "awaiting_approval"
                    and approval_mode != "yolo"
                ),
                "reject": status in {
                    "awaiting_approval",
                    "verification_failed",
                },
                "verify": status == "verification_failed",
            },
        }
    return artifacts


def _serialize_gate(gate: OutputGate | None) -> dict[str, Any] | None:
    if gate is None:
        return None
    value = asdict(gate)
    return {
        "requiredPatterns": [
            {"id": item["identifier"], "pattern": item["pattern"]}
            for item in value["required_patterns"]
        ],
        "forbiddenPatterns": [
            {"id": item["identifier"], "pattern": item["pattern"]}
            for item in value["forbidden_patterns"]
        ],
        "maxCharacters": value["max_characters"],
        "format": value["output_format"],
        "stripSingleCodeFence": value["strip_single_code_fence"],
        "pythonSyntax": value["python_syntax"],
        "jsonRequiredKeys": list(value["json_required_keys"]),
        "jsonAllowedKeys": list(value["json_allowed_keys"]),
        "jsonFieldEnums": {
            key: list(choices)
            for key, choices in value["json_field_enums"].items()
        },
    }


def _run_summary(
    state: dict[str, Any],
    session_dir: Path,
    active: bool,
) -> dict[str, Any]:
    tasks = state.get("tasks", {})
    counts: dict[str, int] = {}
    for task in tasks.values():
        task_status = task.get("status", "pending")
        counts[task_status] = counts.get(task_status, 0) + 1
    finished_at = state.get("finishedAt")
    started_at = state.get("startedAt")
    elapsed = _elapsed_seconds(started_at, finished_at)
    summary = {
        "sessionId": state.get("sessionId"),
        "planId": state.get("planId"),
        "objective": state.get("objective", ""),
        "status": state.get("status", "pending"),
        "active": active,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "elapsedSeconds": elapsed,
        "counts": counts,
        "total": len(tasks),
        "completed": counts.get("completed", 0),
        "retryOf": state.get("retryOf"),
        "revisionOf": state.get("revisionOf"),
        "commanderRequestId": state.get("commanderRequestId"),
        "reviewStatus": state.get(
            "reviewStatus",
            (
                "awaiting_review"
                if state.get("status") == "completed"
                else "not_eligible"
            ),
        ),
        "launchSource": state.get("launchSource", "cli"),
        "pauseReason": state.get("pauseReason"),
        "maxRepair": state.get("maxRepair"),
        "frontierResult": (
            str(session_dir / "frontier-result.json")
            if (session_dir / "frontier-result.json").is_file()
            else None
        ),
    }
    if state.get("workspaceExecution"):
        summary["approvalMode"] = state.get(
            "approvalMode",
            "supervised",
        )
        summary["workspaceTarget"] = state.get(
            "workspaceTarget",
            "worktree",
        )
    return summary


def _recoverable_partial(state: dict[str, Any]) -> bool:
    if state.get("status") != "partial":
        return False
    if state.get("pauseReason") == "finalization_validation_failed":
        return True
    recoverable = {
        "awaiting_approval",
        "verification_failed",
        "applying",
        "verifying",
    }
    return any(
        task.get("status") in recoverable
        for task in state.get("tasks", {}).values()
        if isinstance(task, dict)
    )


def _elapsed_seconds(
    started_at: str | None,
    finished_at: str | None,
) -> float | None:
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = (
            datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
            if finished_at
            else datetime.now(started.tzinfo)
        )
        return max(0.0, (end - started).total_seconds())
    except (TypeError, ValueError):
        return None


def _local_usage(batches: list[dict[str, Any]]) -> dict[str, int]:
    prompt_tokens = 0
    generation_tokens = 0
    generation_calls = 0
    model_loads = 0
    for batch in batches:
        statistics = [batch.get("statistics", {})]
        statistics.extend(
            repair.get("statistics", {})
            for repair in batch.get("repairs", [])
        )
        for stats in statistics:
            if not stats:
                continue
            if float(stats.get("loadSeconds", 0.0)) > 0:
                model_loads += 1
            if stats.get("batchSize", 0) == 0:
                continue
            generation_calls += (
                int(stats.get("generationCalls", 0))
                or len(stats.get("groups", []))
                or 1
            )
            prompt_tokens += int(stats.get("promptTokens", 0))
            generation_tokens += int(stats.get("generationTokens", 0))
    return {
        "promptTokens": prompt_tokens,
        "generationTokens": generation_tokens,
        "generationCalls": generation_calls,
        "modelLoads": model_loads,
    }


def _levels_from_state(state: dict[str, Any]) -> list[list[str]]:
    tasks = state.get("tasks", {})
    remaining = set(tasks)
    completed: set[str] = set()
    levels: list[list[str]] = []
    while remaining:
        ready = sorted(
            task_id
            for task_id in remaining
            if all(
                dependency in completed
                for dependency in tasks[task_id].get("dependsOn", [])
            )
        )
        if not ready:
            levels.append(sorted(remaining))
            break
        levels.append(ready)
        completed.update(ready)
        remaining.difference_update(ready)
    return levels


def _validate_identifier(value: str, label: str) -> None:
    if _SAFE_ID.fullmatch(value) is None:
        raise APIError(HTTPStatus.BAD_REQUEST, f"Invalid {label}.")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_json_file(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


class CockpitHandler(BaseHTTPRequestHandler):
    app: CockpitApp
    server_version = "MLXSwarmCockpit/0.3"

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/status":
                self._send_json(self.app.status_payload())
                return
            if path == "/api/plans":
                self._send_json(self.app.plans_payload())
                return
            if path == "/api/commander/requests":
                self._send_json(self.app.commander_requests_payload())
                return
            if path == "/api/evaluations":
                self._send_json(self.app.evaluations_payload())
                return
            if path == "/api/runs":
                self._send_json(self.app.runs_payload())
                return
            evaluation_id = _api_evaluation_id(path)
            if evaluation_id is not None:
                self._send_json(
                    self.app.evaluation_detail(evaluation_id)
                )
                return
            commander_parts = _api_commander_request_parts(path)
            if commander_parts is not None:
                request_id, action = commander_parts
                if action is None:
                    self._send_json(
                        self.app.commander_request_detail(request_id)
                    )
                    return
            parts = _api_run_parts(path)
            if parts is not None:
                plan_id, session_id, action = parts
                if action is None:
                    self._send_json(
                        self.app.run_detail(plan_id, session_id)
                    )
                    return
            self._serve_static(path)
        except APIError as exc:
            self._send_error_json(exc.status, exc.message)
        except Exception as exc:
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Internal server error: {exc}",
            )

    def do_POST(self) -> None:
        try:
            self._check_origin()
            path = urlparse(self.path).path
            body = self._read_json_body()
            if path == "/api/commander/requests":
                _validate_body_keys(
                    body,
                    {"objective"},
                    {"constraints", "revisionOf"},
                )
                self._send_json(
                    self.app.create_commander_request(
                        _required_text(body, "objective"),
                        body.get("constraints"),
                        body.get("revisionOf"),
                    ),
                    status=HTTPStatus.CREATED,
                )
                return
            if path == "/api/runs":
                _validate_body_keys(
                    body,
                    {"planId"},
                    {
                        "maxRepair",
                        "planDigest",
                        "executionDigest",
                        "approvalMode",
                        "workspaceTarget",
                    },
                )
                self._send_json(
                    self.app.launch_run(
                        _required_text(body, "planId"),
                        body.get("maxRepair", 0),
                        plan_digest=body.get("planDigest"),
                        execution_digest=body.get("executionDigest"),
                        approval_mode=body.get(
                            "approvalMode",
                            "supervised",
                        ),
                        workspace_target=body.get(
                            "workspaceTarget",
                            "worktree",
                        ),
                    ),
                    status=HTTPStatus.ACCEPTED,
                )
                return
            commander_parts = _api_commander_request_parts(path)
            if commander_parts is not None:
                request_id, action = commander_parts
                if action == "approve-run":
                    _validate_body_keys(
                        body,
                        {"planDigest"},
                        {
                            "executionDigest",
                            "maxRepair",
                            "approvalMode",
                            "workspaceTarget",
                        },
                    )
                    self._send_json(
                        self.app.approve_commander_run(
                            request_id,
                            _required_text(body, "planDigest"),
                            body.get("maxRepair", 0),
                            body.get("executionDigest"),
                            body.get("approvalMode", "supervised"),
                            body.get("workspaceTarget", "worktree"),
                        ),
                        status=HTTPStatus.ACCEPTED,
                    )
                    return
            artifact_parts = _api_artifact_parts(path)
            if artifact_parts is not None:
                plan_id, session_id, task_id, action = artifact_parts
                _validate_body_keys(
                    body,
                    {"artifactDigest"},
                    {"reason"},
                )
                self._send_json(
                    self.app.artifact_action(
                        plan_id,
                        session_id,
                        task_id,
                        action=action,
                        artifact_digest=_required_text(
                            body,
                            "artifactDigest",
                        ),
                        reason=body.get("reason"),
                    ),
                    status=HTTPStatus.ACCEPTED,
                )
                return
            workspace_parts = _api_workspace_parts(path)
            if workspace_parts is not None:
                plan_id, session_id, action = workspace_parts
                _validate_body_keys(body, set(), set())
                if action == "cleanup":
                    self._send_json(
                        self.app.cleanup_run_workspace(
                            plan_id,
                            session_id,
                        )
                    )
                    return
            parts = _api_run_parts(path)
            if parts is not None:
                plan_id, session_id, action = parts
                if action == "resume":
                    self._send_json(
                        self.app.resume_run(plan_id, session_id),
                        status=HTTPStatus.ACCEPTED,
                    )
                    return
                if action == "retry":
                    _validate_body_keys(
                        body,
                        set(),
                        {
                            "maxRepair",
                            "executionDigest",
                            "approvalMode",
                            "workspaceTarget",
                        },
                    )
                    self._send_json(
                        self.app.retry_run(
                            plan_id,
                            session_id,
                            body.get("maxRepair", 0),
                            execution_digest=body.get("executionDigest"),
                            approval_mode=body.get("approvalMode"),
                            workspace_target=body.get("workspaceTarget"),
                        ),
                        status=HTTPStatus.ACCEPTED,
                    )
                    return
            raise APIError(HTTPStatus.NOT_FOUND, "Endpoint not found.")
        except APIError as exc:
            self._send_error_json(exc.status, exc.message)
        except Exception as exc:
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Internal server error: {exc}",
            )

    def _read_json_body(self) -> dict[str, Any]:
        length_value = self.headers.get("Content-Length")
        if length_value is None:
            raise APIError(HTTPStatus.LENGTH_REQUIRED, "Content-Length required.")
        try:
            length = int(length_value)
        except ValueError as exc:
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "Invalid Content-Length.",
            ) from exc
        if not 0 <= length <= MAX_REQUEST_BYTES:
            raise APIError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "Request body is too large.",
            )
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise APIError(HTTPStatus.BAD_REQUEST, "Invalid JSON body.") from exc
        if not isinstance(value, dict):
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "JSON body must be an object.",
            )
        return value

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if not origin:
            return
        parsed = urlparse(origin)
        server_host, server_port = self.server.server_address[:2]
        try:
            origin_port = parsed.port
            request_host = urlparse(
                f"http://{self.headers.get('Host', '')}"
            ).hostname
        except ValueError as exc:
            raise APIError(
                HTTPStatus.FORBIDDEN,
                "Cross-origin request rejected.",
            ) from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname not in LOCAL_HOSTS
            or parsed.hostname != request_host
        ):
            raise APIError(HTTPStatus.FORBIDDEN, "Cross-origin request rejected.")
        if origin_port != server_port:
            raise APIError(HTTPStatus.FORBIDDEN, "Cross-origin request rejected.")
        if server_host not in LOCAL_HOSTS:
            raise APIError(HTTPStatus.FORBIDDEN, "Mutation is not localhost-bound.")

    def _serve_static(self, path: str) -> None:
        asset_name = {
            "/": "index.html",
            "/index.html": "index.html",
            "/app.js": "app.js",
            "/styles.css": "styles.css",
        }.get(path)
        if asset_name is None:
            raise APIError(HTTPStatus.NOT_FOUND, "Not found.")
        asset = files("mlx_swarm.ui_static").joinpath(asset_name)
        content = asset.read_bytes()
        content_type = mimetypes.guess_type(asset_name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", _content_security_policy())
        self.end_headers()
        self.wfile.write(content)

    def _send_json(
        self,
        value: Any,
        *,
        status: int = HTTPStatus.OK,
    ) -> None:
        content = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def log_message(self, format: str, *args: Any) -> None:
        return


def _api_run_parts(path: str) -> tuple[str, str, str | None] | None:
    parts = [unquote(part) for part in path.strip("/").split("/")]
    if len(parts) not in {4, 5} or parts[:2] != ["api", "runs"]:
        return None
    action = parts[4] if len(parts) == 5 else None
    if action not in {None, "resume", "retry"}:
        return None
    return parts[2], parts[3], action


def _api_evaluation_id(path: str) -> str | None:
    parts = [unquote(part) for part in path.strip("/").split("/")]
    if len(parts) != 3 or parts[:2] != ["api", "evaluations"]:
        return None
    return parts[2]


def _api_commander_request_parts(
    path: str,
) -> tuple[str, str | None] | None:
    parts = [unquote(part) for part in path.strip("/").split("/")]
    if (
        len(parts) not in {4, 5}
        or parts[:3] != ["api", "commander", "requests"]
    ):
        return None
    action = parts[4] if len(parts) == 5 else None
    if action not in {None, "approve-run"}:
        return None
    return parts[3], action


def _api_artifact_parts(
    path: str,
) -> tuple[str, str, str, str] | None:
    parts = [unquote(part) for part in path.strip("/").split("/")]
    if (
        len(parts) != 7
        or parts[:2] != ["api", "runs"]
        or parts[4] != "artifacts"
        or parts[6] not in {"apply", "reject", "verify"}
    ):
        return None
    return parts[2], parts[3], parts[5], parts[6]


def _api_workspace_parts(
    path: str,
) -> tuple[str, str, str] | None:
    parts = [unquote(part) for part in path.strip("/").split("/")]
    if (
        len(parts) != 6
        or parts[:2] != ["api", "runs"]
        or parts[4] != "workspace"
        or parts[5] != "cleanup"
    ):
        return None
    return parts[2], parts[3], parts[5]


def _required_text(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise APIError(HTTPStatus.BAD_REQUEST, f"{key} is required.")
    return result.strip()


def _validate_body_keys(
    value: dict[str, Any],
    required: set[str],
    optional: set[str],
) -> None:
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise APIError(
            HTTPStatus.BAD_REQUEST,
            f"Missing fields: {', '.join(sorted(missing))}.",
        )
    if unknown:
        raise APIError(
            HTTPStatus.BAD_REQUEST,
            f"Unknown fields: {', '.join(sorted(unknown))}.",
        )


def _content_security_policy() -> str:
    return (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    )


def make_handler(app: CockpitApp) -> type[CockpitHandler]:
    class BoundCockpitHandler(CockpitHandler):
        pass

    BoundCockpitHandler.app = app
    return BoundCockpitHandler


def serve_ui(
    config: SwarmConfig,
    plans_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    server_factory: Callable[..., ThreadingHTTPServer] = ThreadingHTTPServer,
) -> None:
    if host not in LOCAL_HOSTS:
        raise RuntimeError(
            "The cockpit is localhost-only; use 127.0.0.1, localhost, or ::1."
        )
    app = CockpitApp(config, plans_dir)
    server = server_factory((host, port), make_handler(app))
    actual_host, actual_port = server.server_address[:2]
    display_host = f"[{actual_host}]" if ":" in actual_host else actual_host
    url = f"http://{display_host}:{actual_port}/"
    print(json.dumps({
        "ready": True,
        "url": url,
        "plansDir": str(app.plans_dir),
        "artifactsDir": str(app.artifacts_dir),
    }, indent=2))
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
