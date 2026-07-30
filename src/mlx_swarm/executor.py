"""DAG executor — dependency-safe waves with deterministic repair loops."""
# @lat: [[Executor]]

from __future__ import annotations

import fcntl
import importlib.metadata
import json
import platform
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from .backend import BatchBackend, MLXBatchBackend
from .contracts import (
    EXACT_EDIT_MAX_TOKENS,
    Plan,
    ROLE_DEFAULTS,
    SwarmConfig,
    TaskDef,
    worker_capabilities_payload,
)
from .gates import evaluate_gate, gate_feedback_for_repair, normalize_output
from .model_identity import model_directory_identity
from .prompting import (
    compose_editing_prompt,
    compose_prompt,
    compose_reasoning_prompt,
    compose_repair_prompt,
)
from .session import Session, _run_id, _utc_now
from .workspace import (
    WorkspaceError,
    apply_artifact,
    archive_artifact_attempt,
    checkout_runner_lock_path,
    load_artifact,
    load_completed_artifact_evidence,
    load_workspace_snapshot,
    materialize_edit_manifest,
    persist_artifact,
    read_failed_verification_action,
    read_initial_decision,
    recover_artifact_application,
    release_checkout_lease,
    require_checkout_lease,
    revert_applied_artifact,
    run_verifications,
    submit_artifact_decision,
    validate_execution_snapshot,
)


def _worker_strategy_compatible(
    existing: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    """Accept the pre-capability snapshot shape for one compatibility cycle."""
    if existing == current:
        return True
    legacy = {
        "mode": current["mode"],
        "reasoningMaxTokens": current["reasoningMaxTokens"],
    }
    return existing == legacy


def execute_plan(
    config: SwarmConfig,
    plan: Plan,
    session_dir: Path | None = None,
    max_repair: int = 0,
    *,
    backend: BatchBackend | None = None,
    retry_of: str | None = None,
    launch_source: str = "cli",
    approval_poll_seconds: float = 1.0,
) -> Session:
    """Execute with an exclusive per-session runner lock."""
    if session_dir is None:
        run_id = _run_id()
        session_dir = config.artifacts_dir / plan.plan_id / run_id
    session_dir = session_dir.resolve()
    session_dir.mkdir(parents=True, exist_ok=True)
    with _runner_lock(session_dir):
        with _workspace_execution_lock(config, session_dir):
            return _execute_plan_unlocked(
                config,
                plan,
                session_dir=session_dir,
                max_repair=max_repair,
                backend=backend,
                retry_of=retry_of,
                launch_source=launch_source,
                approval_poll_seconds=approval_poll_seconds,
            )


def _execute_plan_unlocked(
    config: SwarmConfig,
    plan: Plan,
    session_dir: Path,
    max_repair: int = 0,
    *,
    backend: BatchBackend | None = None,
    retry_of: str | None = None,
    launch_source: str = "cli",
    approval_poll_seconds: float = 1.0,
) -> Session:
    """Execute a validated plan and persist one final frontier-review packet."""
    if max_repair < 0:
        raise ValueError("max_repair must be non-negative.")
    if approval_poll_seconds <= 0:
        raise ValueError("approval_poll_seconds must be positive.")

    session = _open_session(
        config,
        plan,
        session_dir,
        retry_of=retry_of,
        launch_source=launch_source,
    )
    # Existing sessions always execute their immutable plan snapshot.
    plan = session.plan
    stored_max_repair = session.state.get("maxRepair")
    if (
        isinstance(stored_max_repair, int)
        and not isinstance(stored_max_repair, bool)
        and stored_max_repair >= 0
    ):
        max_repair = stored_max_repair
    else:
        session.state["maxRepair"] = max_repair
    if plan.workspace_execution and not session.state.get("workspaceExecution"):
        raise RuntimeError(
            "Schema-v2 workspace plans require a digest-approved worktree snapshot."
        )
    if plan.workspace_execution:
        workspace = session.workspace_snapshot()
        assert workspace is not None
        validate_execution_snapshot(
            workspace,
            plan=plan,
            approval=session.state.get("executionApproval"),
            session_id=session.session_id,
        )
        policy = workspace.get("executionPolicy")
        if not isinstance(policy, dict):
            policy = {
                "schemaVersion": 1,
                "approvalMode": "supervised",
                "workspaceTarget": "worktree",
                "onVerificationFailure": "pause",
            }
        existing_policy = session.state.get("executionPolicy")
        if isinstance(existing_policy, dict) and existing_policy != policy:
            raise RuntimeError(
                "Resume execution policy differs from the snapshotted session."
            )
        session.state["executionPolicy"] = policy
        session.state["approvalMode"] = policy.get(
            "approvalMode",
            "supervised",
        )
        session.state["workspaceTarget"] = policy.get(
            "workspaceTarget",
            "worktree",
        )
        session.state["executionPolicySha256"] = workspace.get(
            "executionPolicySha256"
        )
        session._save()
    session.set_status("running")
    _recover_interrupted_tasks(session)
    _process_existing_workspace_states(
        session,
        plan,
        poll_seconds=approval_poll_seconds,
    )
    _execute_ready_deterministic_tasks(
        session,
        plan,
        poll_seconds=approval_poll_seconds,
    )

    if _session_needs_generation(session, plan, max_repair):
        worker_strategy = {
            "mode": config.worker.mode,
            "reasoningMaxTokens": config.worker.reasoning_max_tokens,
            "capabilities": worker_capabilities_payload(
                config.worker.capabilities
            ),
        }
        existing_strategy = session.state.get("workerStrategy")
        if (
            isinstance(existing_strategy, dict)
            and not _worker_strategy_compatible(
                existing_strategy,
                worker_strategy,
            )
        ):
            raise RuntimeError(
                "Resume worker strategy differs from the snapshotted session."
            )
        session.state["workerStrategy"] = worker_strategy
        session._save()
        owns_backend = backend is None
        if backend is None:
            try:
                worker_backend: BatchBackend = MLXBatchBackend(config)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                for task in plan.tasks:
                    if session.get_task_status(task.id) != "completed":
                        session.update_task(
                            task.id,
                            status="failed",
                            error=message,
                        )
                return _finalize_session(session, plan)
        else:
            worker_backend = backend

        local_execution_profile = _local_execution_profile(
            config,
            worker_backend,
        )
        existing_profile = session.state.get("localExecutionProfile")
        if (
            isinstance(existing_profile, dict)
            and existing_profile != local_execution_profile
        ):
            if owns_backend:
                worker_backend.close()
            raise RuntimeError(
                "Resume local execution profile differs from the snapshotted "
                "session."
            )
        session.state["localExecutionProfile"] = local_execution_profile
        session._save()

        try:
            for level_idx, level_tasks in enumerate(
                plan.topological_order()
            ):
                _await_workspace_tasks(
                    session,
                    level_tasks,
                    poll_seconds=approval_poll_seconds,
                )
                _unblock_tasks_with_completed_dependencies(
                    session,
                    level_tasks,
                )
                _block_tasks_with_failed_dependencies(session, level_tasks)

                initial_tasks = [
                    task
                    for task in level_tasks
                    if session.get_task_status(task.id) == "pending"
                    and _dependencies_completed(session, task)
                ]
                deterministic_tasks = [
                    task
                    for task in initial_tasks
                    if task.execution_mode == "deterministic-edit"
                ]
                if deterministic_tasks:
                    _execute_deterministic_chunk(
                        session,
                        deterministic_tasks,
                        level_idx=level_idx,
                        poll_seconds=approval_poll_seconds,
                    )
                initial_tasks = [
                    task
                    for task in initial_tasks
                    if task.execution_mode == "local-agent"
                ]
                for chunk_idx, tasks in enumerate(
                    _chunked(initial_tasks, config.batch.max_workers)
                ):
                    _execute_initial_chunk(
                        config,
                        plan,
                        session,
                        worker_backend,
                        list(tasks),
                        level_idx,
                        chunk_idx,
                        max_repair,
                        approval_poll_seconds,
                    )

                # A process can die after a rejection was saved but before its
                # repair generation completed. Resume those tasks directly
                # from their stored gate feedback and previous output.
                resumable_rejections = [
                    task
                    for task in level_tasks
                    if session.get_task_status(task.id) == "rejected"
                    and _dependencies_completed(session, task)
                    and session.state["tasks"][task.id]["repairAttempts"]
                    < _repair_limit(task, max_repair)
                ]
                for chunk_idx, tasks in enumerate(
                    _chunked(
                        resumable_rejections,
                        config.batch.max_workers,
                    )
                ):
                    _execute_resume_repairs(
                        config,
                        plan,
                        session,
                        worker_backend,
                        list(tasks),
                        level_idx,
                        chunk_idx,
                        max_repair,
                        approval_poll_seconds,
                    )
        finally:
            if owns_backend:
                worker_backend.close()

    return _finalize_session(session, plan)


def _open_session(
    config: SwarmConfig,
    plan: Plan,
    session_dir: Path | None,
    *,
    retry_of: str | None,
    launch_source: str,
) -> Session:
    if session_dir is None:
        run_id = _run_id()
        session_dir = config.artifacts_dir / plan.plan_id / run_id
        session = Session(
            session_dir,
            plan,
            session_id=run_id,
            retry_of=retry_of,
            launch_source=launch_source,
        )
    elif (session_dir / "session.json").is_file():
        session = Session.load(session_dir, config)
        if session.plan.plan_id != plan.plan_id:
            raise RuntimeError(
                f"Session plan {session.plan.plan_id!r} does not match "
                f"requested plan {plan.plan_id!r}."
            )
        if session.plan.raw != plan.raw:
            raise RuntimeError(
                "Requested plan differs from the immutable session snapshot."
            )
        # The snapshotted contract remains authoritative even when the caller
        # supplied equivalent JSON from another source path.
        plan = session.plan
    else:
        session = Session(
            session_dir,
            plan,
            session_id=session_dir.name,
            retry_of=retry_of,
            launch_source=launch_source,
        )

    session.set_sources(config_source=config.source, plan_source=plan.source)
    return session


def _recover_interrupted_tasks(session: Session) -> None:
    tasks_by_id = {task.id: task for task in session.plan.tasks}
    for task_id, state in session.state["tasks"].items():
        if state["status"] == "running":
            task = tasks_by_id[task_id]
            if session.plan.workspace_execution:
                try:
                    manifest, payload = load_artifact(
                        session.dir,
                        task_id,
                    )
                except WorkspaceError:
                    pass
                else:
                    session.update_task(
                        task_id,
                        status=(
                            "awaiting_approval"
                            if task.mutates_workspace
                            else "completed"
                        ),
                        output=None,
                        normalizedOutput=payload,
                        artifact=manifest,
                        gateResult={
                            "configured": task.gate is not None,
                            "passed": True,
                            "violations": [],
                            "normalizations": [
                                "recovered-immutable-artifact"
                            ],
                        },
                        error=None,
                    )
                    continue
            session.update_task(
                task_id,
                status="pending",
                error="Interrupted before completion; queued for resume.",
            )
        elif state["status"] in {"applying", "verifying"}:
            task = tasks_by_id[task_id]
            workspace = session.workspace_snapshot()
            if workspace is None:
                session.update_task(
                    task_id,
                    status="failed",
                    error="Interrupted workspace task has no workspace snapshot.",
                )
                continue
            try:
                recovery = recover_artifact_application(
                    session.dir,
                    task,
                    workspace,
                    expected_artifact_sha256=str(
                        state.get("artifact", {}).get("sha256", "")
                    ),
                )
                session.update_workspace(workspace)
            except WorkspaceError as exc:
                session.update_task(
                    task_id,
                    status="applying",
                    error=(
                        "Interrupted artifact application requires explicit "
                        "safe recovery: "
                        f"{exc}"
                    ),
                )
                session.state["pauseReason"] = "apply_recovery_required"
                session._save()
                continue
            if recovery["state"] == "applied":
                try:
                    evidence = load_completed_artifact_evidence(
                        session.dir,
                        task,
                        workspace,
                    )
                except WorkspaceError:
                    evidence = None
                if evidence is not None:
                    session.update_task(
                        task_id,
                        status="completed",
                        applyReceipt=evidence["applyReceipt"],
                        verificationResults=evidence[
                            "verificationReceipts"
                        ],
                        error=None,
                        recoveredVerificationAfterCrash=True,
                    )
                    continue
                session.update_task(
                    task_id,
                    status="verification_failed",
                    applyReceipt=recovery["receipt"],
                    error=(
                        "Interrupted after application; operator verification "
                        "or rejection is required."
                    ),
                )
            else:
                session.update_task(
                    task_id,
                    status="awaiting_approval",
                    error=(
                        "Interrupted before the artifact commit; the sealed "
                        "apply decision will be resumed."
                    ),
                )


def _dependencies_completed(session: Session, task: TaskDef) -> bool:
    return all(
        session.get_task_status(dependency) == "completed"
        for dependency in task.depends_on
    )


def _process_existing_workspace_states(
    session: Session,
    plan: Plan,
    *,
    poll_seconds: float,
) -> None:
    """Finish queued human/YOLO actions before loading a local model."""
    if not plan.workspace_execution:
        return
    for level_tasks in plan.topological_order():
        if any(
            session.get_task_status(task.id)
            in {"awaiting_approval", "verification_failed"}
            for task in level_tasks
        ):
            _await_workspace_tasks(
                session,
                level_tasks,
                poll_seconds=poll_seconds,
            )
        _unblock_tasks_with_completed_dependencies(session, level_tasks)
        _block_tasks_with_failed_dependencies(session, level_tasks)


def _session_needs_generation(
    session: Session,
    plan: Plan,
    max_repair: int,
) -> bool:
    for task in plan.tasks:
        status = session.get_task_status(task.id)
        if not _dependencies_completed(session, task):
            continue
        if status == "pending" and task.execution_mode == "local-agent":
            return True
        if (
            status == "rejected"
            and task.execution_mode == "local-agent"
            and session.state["tasks"][task.id]["repairAttempts"]
            < _repair_limit(task, max_repair)
        ):
            return True
    return False


def _execute_ready_deterministic_tasks(
    session: Session,
    plan: Plan,
    *,
    poll_seconds: float,
) -> None:
    """Materialize frontier-known edits without loading the local model."""
    progressed = True
    while progressed:
        progressed = False
        for level_idx, level_tasks in enumerate(plan.topological_order()):
            ready = [
                task
                for task in level_tasks
                if task.execution_mode == "deterministic-edit"
                and session.get_task_status(task.id) == "pending"
                and _dependencies_completed(session, task)
            ]
            if not ready:
                continue
            _execute_deterministic_chunk(
                session,
                ready,
                level_idx=level_idx,
                poll_seconds=poll_seconds,
            )
            progressed = True


def _execute_deterministic_chunk(
    session: Session,
    tasks: list[TaskDef],
    *,
    level_idx: int,
    poll_seconds: float,
) -> None:
    record: dict[str, Any] = {
        "levelIndex": level_idx,
        "chunkIndex": 0,
        "phase": "deterministic-edit",
        "taskIds": [task.id for task in tasks],
        "startedAt": _utc_now(),
        "statistics": {
            "batchSize": 0,
            "generationCalls": 0,
            "promptTokens": 0,
            "generationTokens": 0,
        },
    }
    started = time.perf_counter()
    for task in tasks:
        session.update_task(
            task.id,
            status="running",
            batchIndex=level_idx,
        )
        payload = json.dumps(
            {"edits": list(task.deterministic_edits)},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        _process_task_output(
            session,
            task,
            payload,
            prompt="",
            phase="deterministic-edit",
            statistics=record["statistics"],
        )
        if session.get_task_status(task.id) == "rejected":
            session.update_task(
                task.id,
                status="failed",
                error=(
                    "Frontier-authored deterministic edits failed structural "
                    "workspace validation."
                ),
            )
    record["generationFinishedAt"] = _utc_now()
    record["generationElapsedSeconds"] = time.perf_counter() - started
    record["state"] = "awaiting-workspace-actions"
    record_index = session.add_batch_record(record)
    _await_workspace_tasks(
        session,
        tasks,
        poll_seconds=poll_seconds,
    )
    session.update_batch_record(
        record_index,
        state="completed",
        finishedAt=_utc_now(),
        elapsedSeconds=time.perf_counter() - started,
    )


def _block_tasks_with_failed_dependencies(
    session: Session,
    tasks: Iterable[TaskDef],
) -> None:
    terminal_failure_states = {
        "rejected",
        "rejected_by_operator",
        "verification_failed",
        "failed",
        "blocked",
    }
    for task in tasks:
        if session.get_task_status(task.id) not in {"pending", "running"}:
            continue
        blocked_by = [
            dependency
            for dependency in task.depends_on
            if session.get_task_status(dependency) in terminal_failure_states
        ]
        if blocked_by:
            session.update_task(
                task.id,
                status="blocked",
                blockedBy=blocked_by,
                error=(
                    "Dependency did not complete successfully: "
                    + ", ".join(blocked_by)
                ),
            )


def _unblock_tasks_with_completed_dependencies(
    session: Session,
    tasks: Iterable[TaskDef],
) -> None:
    """Requeue descendants after an earlier failed verification later passes."""
    for task in tasks:
        if (
            session.get_task_status(task.id) == "blocked"
            and _dependencies_completed(session, task)
        ):
            session.update_task(
                task.id,
                status="pending",
                blockedBy=None,
                error=None,
            )


def _block_all_remaining_descendants(session: Session, plan: Plan) -> None:
    changed = True
    while changed:
        changed = False
        for task in plan.tasks:
            if session.get_task_status(task.id) not in {"pending", "running"}:
                continue
            blocked_by = [
                dependency
                for dependency in task.depends_on
                if session.get_task_status(dependency) != "completed"
            ]
            if blocked_by:
                session.update_task(
                    task.id,
                    status="blocked",
                    blockedBy=blocked_by,
                    error=(
                        "Dependency did not complete successfully: "
                        + ", ".join(blocked_by)
                    ),
                )
                changed = True


def _execute_initial_chunk(
    config: SwarmConfig,
    plan: Plan,
    session: Session,
    backend: BatchBackend,
    tasks: list[TaskDef],
    level_idx: int,
    chunk_idx: int,
    max_repair: int,
    approval_poll_seconds: float,
) -> None:
    record: dict[str, Any] = {
        "levelIndex": level_idx,
        "chunkIndex": chunk_idx,
        "phase": "generation",
        "taskIds": [task.id for task in tasks],
        "startedAt": _utc_now(),
    }
    started = time.perf_counter()

    for task in tasks:
        session.update_task(task.id, status="running", batchIndex=level_idx)

    prompts = [_base_task_prompt(plan, session, task) for task in tasks]
    runnable_tasks, runnable_prompts = _filter_prompt_lengths(
        config,
        session,
        tasks,
        prompts,
        record,
    )

    if runnable_tasks:
        outputs, stats, stages = _generate_with_worker_strategy(
            config,
            session,
            backend,
            runnable_tasks,
            runnable_prompts,
            phase="generation",
        )
        record["statistics"] = stats
        if stages:
            record["stages"] = stages
        for task, prompt, output in zip(
            runnable_tasks,
            runnable_prompts,
            outputs,
        ):
            if session.get_task_status(task.id) == "failed":
                continue
            _process_task_output(
                session,
                task,
                output,
                prompt=prompt,
                phase="generation",
                statistics=stats,
            )
    else:
        record["statistics"] = {"batchSize": 0}

    _repair_rejected_tasks(
        config,
        plan,
        session,
        backend,
        runnable_tasks,
        max_repair,
        record,
    )
    record["generationFinishedAt"] = _utc_now()
    record["generationElapsedSeconds"] = time.perf_counter() - started
    record["state"] = "awaiting-workspace-actions"
    record_index = session.add_batch_record(record)
    _await_workspace_tasks(
        session,
        runnable_tasks,
        poll_seconds=approval_poll_seconds,
    )
    session.update_batch_record(
        record_index,
        state="completed",
        finishedAt=_utc_now(),
        elapsedSeconds=time.perf_counter() - started,
    )


def _execute_resume_repairs(
    config: SwarmConfig,
    plan: Plan,
    session: Session,
    backend: BatchBackend,
    tasks: list[TaskDef],
    level_idx: int,
    chunk_idx: int,
    max_repair: int,
    approval_poll_seconds: float,
) -> None:
    record: dict[str, Any] = {
        "levelIndex": level_idx,
        "chunkIndex": chunk_idx,
        "phase": "resume-repair",
        "taskIds": [task.id for task in tasks],
        "startedAt": _utc_now(),
        "statistics": {"batchSize": 0},
    }
    started = time.perf_counter()
    _repair_rejected_tasks(
        config,
        plan,
        session,
        backend,
        tasks,
        max_repair,
        record,
    )
    record["generationFinishedAt"] = _utc_now()
    record["generationElapsedSeconds"] = time.perf_counter() - started
    record["state"] = "awaiting-workspace-actions"
    record_index = session.add_batch_record(record)
    _await_workspace_tasks(
        session,
        tasks,
        poll_seconds=approval_poll_seconds,
    )
    session.update_batch_record(
        record_index,
        state="completed",
        finishedAt=_utc_now(),
        elapsedSeconds=time.perf_counter() - started,
    )


def _repair_rejected_tasks(
    config: SwarmConfig,
    plan: Plan,
    session: Session,
    backend: BatchBackend,
    candidate_tasks: list[TaskDef],
    max_repair: int,
    record: dict[str, Any],
) -> None:
    repair_round = 0
    candidates = [
        task
        for task in candidate_tasks
        if session.get_task_status(task.id) == "rejected"
        and session.state["tasks"][task.id]["repairAttempts"]
        < _repair_limit(task, max_repair)
    ]

    while candidates:
        repair_round += 1
        repair_prompts: list[str] = []
        runnable_tasks: list[TaskDef] = []
        repair_record: dict[str, Any] = {
            "round": repair_round,
            "taskIds": [task.id for task in candidates],
            "startedAt": _utc_now(),
        }
        repair_started = time.perf_counter()

        for task in candidates:
            task_state = session.state["tasks"][task.id]
            original_prompt = _base_task_prompt(plan, session, task)
            feedback = gate_feedback_for_repair(task_state["gateResult"])
            previous_output = task_state["output"] or ""
            repair_prompt = compose_repair_prompt(
                original_prompt,
                feedback,
                previous_output,
            )
            if len(repair_prompt) > config.batch.max_prompt_characters:
                session.update_task(
                    task.id,
                    status="failed",
                    error=(
                        f"Repair prompt exceeds "
                        f"{config.batch.max_prompt_characters} characters."
                    ),
                )
                continue
            session.update_task(
                task.id,
                repairAttempts=task_state["repairAttempts"] + 1,
                status="running",
            )
            runnable_tasks.append(task)
            repair_prompts.append(repair_prompt)

        if runnable_tasks:
            outputs, stats, stages = _generate_with_worker_strategy(
                config,
                session,
                backend,
                runnable_tasks,
                repair_prompts,
                phase=f"repair-{repair_round}",
            )
            repair_record["statistics"] = stats
            if stages:
                repair_record["stages"] = stages
            for task, prompt, output in zip(
                runnable_tasks,
                repair_prompts,
                outputs,
            ):
                if session.get_task_status(task.id) == "failed":
                    continue
                _process_task_output(
                    session,
                    task,
                    output,
                    prompt=prompt,
                    phase=f"repair-{repair_round}",
                    statistics=stats,
                )
        else:
            repair_record["statistics"] = {"batchSize": 0}

        repair_record["finishedAt"] = _utc_now()
        repair_record["elapsedSeconds"] = time.perf_counter() - repair_started
        record.setdefault("repairs", []).append(repair_record)
        candidates = [
            task
            for task in runnable_tasks
            if session.get_task_status(task.id) == "rejected"
            and session.state["tasks"][task.id]["repairAttempts"]
            < _repair_limit(task, max_repair)
        ]


def _filter_prompt_lengths(
    config: SwarmConfig,
    session: Session,
    tasks: list[TaskDef],
    prompts: list[str],
    record: dict[str, Any],
) -> tuple[list[TaskDef], list[str]]:
    runnable_tasks: list[TaskDef] = []
    runnable_prompts: list[str] = []
    for task, prompt in zip(tasks, prompts):
        if len(prompt) > config.batch.max_prompt_characters:
            error = (
                f"Prompt exceeds {config.batch.max_prompt_characters} "
                f"characters ({len(prompt)} characters)."
            )
            session.update_task(task.id, status="failed", error=error)
            record.setdefault("errors", []).append({
                "taskId": task.id,
                "message": error,
            })
        else:
            runnable_tasks.append(task)
            runnable_prompts.append(prompt)
    return runnable_tasks, runnable_prompts


def _base_task_prompt(
    plan: Plan,
    session: Session,
    task: TaskDef,
) -> str:
    """Use an immutable evaluation prompt when present, otherwise compose it."""
    replay = session.replay_prompt(task.id)
    if replay is not None:
        return replay
    return compose_prompt(plan.context, task, session=session)


def _generate_or_fail(
    session: Session,
    backend: BatchBackend,
    tasks: list[TaskDef],
    prompts: list[str],
) -> tuple[list[str], dict[str, Any]]:
    try:
        outputs, stats = backend.generate(tasks, prompts)
        if len(outputs) != len(tasks):
            raise RuntimeError(
                f"Backend returned {len(outputs)} outputs for {len(tasks)} tasks."
            )
        return outputs, stats
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        for task in tasks:
            session.update_task(task.id, status="failed", error=message)
        partial_statistics = getattr(exc, "statistics", None)
        if isinstance(partial_statistics, dict):
            statistics = dict(partial_statistics)
            statistics["error"] = message
            statistics.setdefault("batchSize", 0)
            statistics.setdefault("attemptedBatchSize", len(tasks))
            return [], statistics
        load_seconds = 0.0
        if (
            getattr(backend, "model", None) is not None
            and getattr(backend, "_load_reported", True) is False
        ):
            load_seconds = float(getattr(backend, "load_seconds", 0.0))
            setattr(backend, "_load_reported", True)
        return [], {
            "batchSize": 0,
            "attemptedBatchSize": len(tasks),
            "generationCalls": 0,
            "loadSeconds": load_seconds,
            "error": message,
        }


def _generate_with_worker_strategy(
    config: SwarmConfig,
    session: Session,
    backend: BatchBackend,
    tasks: list[TaskDef],
    prompts: list[str],
    *,
    phase: str,
) -> tuple[list[str], dict[str, Any], list[dict[str, Any]]]:
    """Generate directly or with a local reasoner followed by a strict editor."""
    if config.worker.mode == "direct":
        outputs, statistics = _generate_or_fail(
            session,
            backend,
            tasks,
            prompts,
        )
        return outputs, statistics, []

    reasoning_indices = [
        index
        for index, task in enumerate(tasks)
        if task.mutates_workspace
    ]
    direct_indices = [
        index
        for index in range(len(tasks))
        if index not in reasoning_indices
    ]
    outputs = [""] * len(tasks)
    statistics_parts: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] = []

    if direct_indices:
        direct_tasks = [tasks[index] for index in direct_indices]
        direct_prompts = [prompts[index] for index in direct_indices]
        direct_outputs, direct_statistics = _generate_or_fail(
            session,
            backend,
            direct_tasks,
            direct_prompts,
        )
        for index, output in zip(direct_indices, direct_outputs):
            outputs[index] = output
        statistics_parts.append(direct_statistics)
        stages.append({
            "name": "direct",
            "taskIds": [task.id for task in direct_tasks],
            "statistics": direct_statistics,
        })

    if reasoning_indices:
        reasoning_tasks: list[TaskDef] = []
        reasoning_prompts: list[str] = []
        for index in reasoning_indices:
            task = tasks[index]
            generation_override = dict(task.generation_override)
            generation_override.update({
                "temperature": 0.0,
                "top_p": 1.0,
                "enable_thinking": True,
                "max_tokens": config.worker.reasoning_max_tokens,
            })
            reasoning_tasks.append(
                replace(
                    task,
                    generation_override=generation_override,
                )
            )
            reasoning_prompts.append(
                compose_reasoning_prompt(prompts[index])
            )
        reasoning_outputs, reasoning_statistics = _generate_or_fail(
            session,
            backend,
            reasoning_tasks,
            reasoning_prompts,
        )
        statistics_parts.append(reasoning_statistics)
        stages.append({
            "name": "reasoning",
            "taskIds": [task.id for task in reasoning_tasks],
            "statistics": reasoning_statistics,
        })

        editor_tasks: list[TaskDef] = []
        editor_prompts: list[str] = []
        editor_indices: list[int] = []
        for index, task, original_prompt, reasoning_prompt, reasoning in zip(
            reasoning_indices,
            reasoning_tasks,
            (prompts[value] for value in reasoning_indices),
            reasoning_prompts,
            reasoning_outputs,
        ):
            session.record_reasoning_attempt(
                task.id,
                phase=f"{phase}-reasoning",
                prompt=reasoning_prompt,
                output=reasoning,
                statistics=reasoning_statistics,
            )
            editor_prompt = compose_editing_prompt(
                original_prompt,
                reasoning,
            )
            if len(editor_prompt) > config.batch.max_prompt_characters:
                session.update_task(
                    task.id,
                    status="failed",
                    error=(
                        "Reasoning-to-editing prompt exceeds "
                        f"{config.batch.max_prompt_characters} characters."
                    ),
                )
                continue
            generation_override = dict(tasks[index].generation_override)
            generation_override["enable_thinking"] = False
            editor_tasks.append(
                replace(
                    tasks[index],
                    generation_override=generation_override,
                )
            )
            editor_prompts.append(editor_prompt)
            editor_indices.append(index)

        if editor_tasks:
            editor_outputs, editor_statistics = _generate_or_fail(
                session,
                backend,
                editor_tasks,
                editor_prompts,
            )
            for index, output in zip(editor_indices, editor_outputs):
                outputs[index] = output
            statistics_parts.append(editor_statistics)
            stages.append({
                "name": "editing",
                "taskIds": [task.id for task in editor_tasks],
                "statistics": editor_statistics,
            })

    return outputs, _merge_generation_statistics(statistics_parts), stages


def _merge_generation_statistics(
    values: list[dict[str, Any]],
) -> dict[str, Any]:
    """Combine multiple local stages without mixing them with frontier usage."""
    active = [
        value
        for value in values
        if value
        and (
            value.get("batchSize", 0)
            or value.get("attemptedBatchSize", 0)
            or float(value.get("loadSeconds", 0.0)) > 0
        )
    ]
    if not active:
        errors = [
            str(value["error"])
            for value in values
            if value.get("error")
        ]
        result: dict[str, Any] = {"batchSize": 0}
        if errors:
            result["error"] = "; ".join(errors)
        return result
    return {
        "loadSeconds": sum(
            float(value.get("loadSeconds", 0.0))
            for value in active
        ),
        "modelReused": all(
            bool(value.get("modelReused", False))
            for value in active
        ),
        "generationSeconds": sum(
            float(value.get("generationSeconds", 0.0))
            for value in active
        ),
        "batchSize": sum(
            int(value.get("batchSize", 0))
            for value in active
        ),
        "promptTokens": sum(
            int(value.get("promptTokens", 0))
            for value in active
        ),
        "renderedPromptTokens": sum(
            int(value.get("renderedPromptTokens", 0))
            for value in active
        ),
        "generationTokens": sum(
            int(value.get("generationTokens", 0))
            for value in active
        ),
        "generationCalls": sum(
            _statistics_generation_calls(value)
            for value in active
        ),
        "peakMemoryGigabytes": max(
            float(value.get("peakMemoryGigabytes", 0.0))
            for value in active
        ),
        "samplerGroupCount": sum(
            int(value.get("samplerGroupCount", 0))
            for value in active
        ),
        "physicalBatchCount": sum(
            int(
                value.get(
                    "physicalBatchCount",
                    value.get("generationCalls", 0),
                )
            )
            for value in active
        ),
        "maxTrueBatchWidth": max(
            int(value.get("maxTrueBatchWidth", 0))
            for value in active
        ),
        "samplerFragmented": any(
            bool(value.get("samplerFragmented", False))
            for value in active
        ),
        "batchSplitByPromptBudget": any(
            bool(value.get("batchSplitByPromptBudget", False))
            for value in active
        ),
        "groups": [
            group
            for value in active
            for group in value.get("groups", [])
        ],
    }


def _statistics_generation_calls(value: dict[str, Any]) -> int:
    if "generationCalls" in value:
        return int(value["generationCalls"])
    groups = value.get("groups", [])
    if groups:
        return len(groups)
    return 1 if int(value.get("batchSize", 0)) > 0 else 0


def _repair_limit(task: TaskDef, max_repair: int) -> int:
    return min(task.max_repair_attempts, max_repair)


def _chunked(values: list[TaskDef], size: int) -> Iterable[list[TaskDef]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _process_task_output(
    session: Session,
    task: TaskDef,
    output: str,
    *,
    prompt: str,
    phase: str,
    statistics: dict[str, Any],
) -> None:
    """Evaluate the gate, normalize output, and update session state."""
    previous_output = session.state["tasks"][task.id].get("output")
    repeated_output = (
        isinstance(previous_output, str)
        and previous_output == output
    )
    gate_result = evaluate_gate(output, task.gate)
    hit_token_limit = _task_output_hit_token_limit(
        statistics,
        task.id,
    )
    if hit_token_limit:
        gate_result["passed"] = False
        gate_result.setdefault("violations", []).append({
            "id": "output-token-limit",
            "kind": "size",
            "message": (
                "Generation reached its output-token limit. Split the task "
                "or deliberately raise its bounded generation ceiling."
            ),
        })
    reject_repeated = bool(
        session.state["tasks"][task.id].get("rejectRepeatedOutput")
    )
    if repeated_output and (
        not gate_result["passed"] or reject_repeated
    ):
        gate_result["passed"] = False
        gate_result.setdefault("violations", []).append({
            "id": "repeated-output",
            "kind": "repair",
            "message": (
                "The response exactly repeated the previous ineffective "
                "output. Further repairs are stopped."
            ),
        })
    normalized, _ = normalize_output(output, task.gate)
    artifact = None
    if gate_result["passed"] and session.plan.workspace_execution:
        try:
            if task.worker_output_protocol == "edit-manifest-v1":
                normalized = materialize_edit_manifest(
                    normalized,
                    task=task,
                    workspace=session.workspace_snapshot(),
                )
                gate_result.setdefault("normalizations", []).append(
                    "edit-manifest-v1-to-unified-diff"
                )
            artifact = persist_artifact(
                session.dir,
                task,
                normalized,
                session.workspace_snapshot(),
            )
        except WorkspaceError as exc:
            gate_result["passed"] = False
            gate_result.setdefault("violations", []).append({
                "id": "workspace-artifact",
                "kind": "workspace",
                "message": str(exc),
            })
    status = "completed" if gate_result["passed"] else "rejected"
    error = None
    if hit_token_limit:
        status = "failed"
        error = (
            "Local generation reached its output-token limit; blind repair "
            "was stopped. Split the task or deliberately raise its bounded "
            "generation ceiling."
        )
    elif repeated_output and not gate_result["passed"]:
        status = "failed"
        error = (
            "Local generation repeated the previous rejected output; "
            "further repairs were stopped."
        )
    if (
        gate_result["passed"]
        and task.mutates_workspace
        and artifact is not None
    ):
        status = "awaiting_approval"

    session.record_generation_attempt(
        task.id,
        phase=phase,
        prompt=prompt,
        output=output,
        normalized_output=normalized,
        gate_result=gate_result,
        statistics=statistics,
        repeated_output=repeated_output,
    )
    session.update_task(
        task.id,
        status=status,
        output=output,
        normalizedOutput=normalized,
        gateResult=gate_result,
        artifact=artifact,
        error=error,
    )


def _task_output_hit_token_limit(
    statistics: dict[str, Any],
    task_id: str,
) -> bool:
    """Use the last task stage so reasoning truncation does not mask editing."""
    result = False
    matched = False
    for group in statistics.get("groups", []):
        values = group.get("hitTokenLimit")
        if isinstance(values, dict) and task_id in values:
            result = values[task_id] is True
            matched = True
    return result if matched else False


def _await_workspace_tasks(
    session: Session,
    tasks: Iterable[TaskDef],
    *,
    poll_seconds: float,
) -> None:
    if not session.plan.workspace_execution:
        return
    for task in tasks:
        while session.get_task_status(task.id) in {
            "awaiting_approval",
            "verification_failed",
        }:
            status = session.get_task_status(task.id)
            if session.state.get("status") != "awaiting_approval":
                session.set_status("awaiting_approval")
            if status == "awaiting_approval":
                try:
                    decision = read_initial_decision(
                        session.dir,
                        task.id,
                    )
                except WorkspaceError as exc:
                    session.update_task(
                        task.id,
                        status="failed",
                        error=str(exc),
                    )
                    continue
                if decision is None:
                    if _approval_mode(session) == "yolo":
                        manifest = session.state["tasks"][task.id].get(
                            "artifact",
                            {},
                        )
                        digest = manifest.get("sha256")
                        if not isinstance(digest, str):
                            session.update_task(
                                task.id,
                                status="failed",
                                error=(
                                    "YOLO artifact is missing its immutable "
                                    "digest."
                                ),
                            )
                            continue
                        try:
                            decision = submit_artifact_decision(
                                session.dir,
                                task.id,
                                action="apply",
                                artifact_sha256=digest,
                                source="yolo",
                                reason=(
                                    "Automatically approved by the "
                                    "digest-bound YOLO execution policy."
                                ),
                            )
                        except WorkspaceError:
                            # A concurrent operator decision may have won the
                            # immutable ledger race. Read and honor that exact
                            # sealed decision rather than overwriting it.
                            try:
                                decision = read_initial_decision(
                                    session.dir,
                                    task.id,
                                )
                            except WorkspaceError as exc:
                                session.update_task(
                                    task.id,
                                    status="failed",
                                    error=str(exc),
                                )
                                continue
                            if decision is None:
                                session.update_task(
                                    task.id,
                                    status="failed",
                                    error=(
                                        "YOLO could not seal an artifact "
                                        "decision."
                                    ),
                                )
                                continue
                    else:
                        time.sleep(poll_seconds)
                        continue
                assert decision is not None
                _process_initial_decision(session, task, decision)
            else:
                task_state = session.state["tasks"][task.id]
                processed = task_state.get(
                    "processedVerificationRequests",
                    [],
                )
                try:
                    action = read_failed_verification_action(
                        session.dir,
                        task.id,
                        processed,
                    )
                except WorkspaceError as exc:
                    session.update_task(
                        task.id,
                        status="failed",
                        error=str(exc),
                    )
                    continue
                if action is None:
                    if _approval_mode(session) == "yolo":
                        if _attempt_yolo_verification_recovery(
                            session,
                            task,
                        ):
                            continue
                        session.state["pauseReason"] = (
                            "verification_failed"
                        )
                        session._save()
                        break
                    time.sleep(poll_seconds)
                    continue
                request_name, decision = action
                # Verify requests are unique and can be durably consumed before
                # execution. The fixed rejection slot remains replayable until
                # its revert receipt is safely persisted.
                if request_name != "rejection.json":
                    processed = [*processed, request_name]
                    session.update_task(
                        task.id,
                        processedVerificationRequests=processed,
                    )
                _process_failed_verification_action(
                    session,
                    task,
                    decision,
                )
                if (
                    request_name == "rejection.json"
                    and session.get_task_status(task.id)
                    == "rejected_by_operator"
                ):
                    session.update_task(
                        task.id,
                        processedVerificationRequests=[
                            *processed,
                            request_name,
                        ],
                    )
                elif request_name == "rejection.json":
                    session.state["pauseReason"] = (
                        "workspace_action_failed"
                    )
                    session._save()
                    if _approval_mode(session) == "yolo":
                        break
                    time.sleep(poll_seconds)
    if session.state.get("status") == "awaiting_approval":
        session.set_status("running")


def _attempt_yolo_verification_recovery(
    session: Session,
    task: TaskDef,
) -> bool:
    """Revert and requeue one failed worktree artifact within its repair cap."""
    workspace = session.workspace_snapshot()
    if workspace is None:
        return False
    policy = workspace.get("executionPolicy")
    if (
        not isinstance(policy, dict)
        or policy.get("onVerificationFailure") != "repair-once"
        or policy.get("workspaceTarget") != "worktree"
    ):
        return False
    state = session.state["tasks"][task.id]
    attempts = int(state.get("verificationRecoveryAttempts", 0))
    if attempts >= 1:
        return False
    max_repair = int(session.state.get("maxRepair", 0))
    if state.get("repairAttempts", 0) >= _repair_limit(task, max_repair):
        return False
    manifest = state.get("artifact")
    digest = manifest.get("sha256") if isinstance(manifest, dict) else None
    if not isinstance(digest, str):
        return False
    verification_results = state.get("verificationResults", [])
    failed_result = next(
        (
            result
            for result in reversed(verification_results)
            if isinstance(result, dict)
            and result.get("passed") is False
        ),
        None,
    )
    output = (
        str(failed_result.get("output", ""))[:4000]
        if failed_result is not None
        else ""
    )
    profile_id = (
        str(failed_result.get("profileId", "unknown"))
        if failed_result is not None
        else "unknown"
    )
    try:
        revert_receipt = revert_applied_artifact(
            session.dir,
            task,
            workspace,
            expected_artifact_sha256=digest,
        )
        session.update_workspace(workspace)
        archive = archive_artifact_attempt(
            session.dir,
            task.id,
            attempt=attempts + 1,
            reason="yolo-verification-recovery",
        )
    except WorkspaceError as exc:
        session.state["pauseReason"] = "workspace_action_failed"
        session.update_task(
            task.id,
            status="verification_failed",
            error=(
                "YOLO could not safely revert and archive the failed "
                f"artifact: {exc}"
            ),
        )
        return False

    histories = list(state.get("artifactHistory", []))
    histories.append({
        **archive,
        "artifactSha256": digest,
        "revertReceipt": revert_receipt,
        "verificationResults": verification_results,
    })
    gate_result = {
        "configured": task.gate is not None,
        "passed": False,
        "violations": [{
            "id": "verification-failed",
            "kind": "verification",
            "message": (
                f"Allowlisted verification profile {profile_id} failed. "
                "Return a materially corrected artifact. Bounded output:\n"
                f"{output}"
            ),
        }],
        "normalizations": [],
    }
    session.state.pop("pauseReason", None)
    session.update_task(
        task.id,
        status="rejected",
        gateResult=gate_result,
        artifact=None,
        decision=None,
        applyReceipt=None,
        revertReceipt=revert_receipt,
        verificationResults=[],
        artifactHistory=histories,
        verificationRecoveryAttempts=attempts + 1,
        rejectRepeatedOutput=True,
        error=(
            "YOLO reverted the failed artifact and queued one bounded "
            "verification-guided repair."
        ),
    )
    return True


def _process_initial_decision(
    session: Session,
    task: TaskDef,
    decision: dict[str, Any],
) -> None:
    manifest = session.state["tasks"][task.id].get("artifact") or {}
    workspace = session.workspace_snapshot()
    expected_policy = (
        workspace.get("executionPolicySha256")
        if workspace is not None
        else None
    )
    if (
        isinstance(expected_policy, str)
        and decision.get("executionPolicySha256") != expected_policy
    ):
        session.update_task(
            task.id,
            status="failed",
            error=(
                "Artifact decision is not bound to the approved execution "
                "policy."
            ),
        )
        return
    if decision.get("artifactSha256") != manifest.get("sha256"):
        session.update_task(
            task.id,
            status="failed",
            error="Artifact decision digest does not match the persisted artifact.",
        )
        return
    if decision.get("action") == "reject":
        session.update_task(
            task.id,
            status="rejected_by_operator",
            decision=decision,
            error=decision.get("reason") or "Rejected by the operator.",
        )
        session.state.pop("pauseReason", None)
        session._save()
        return
    if decision.get("action") != "apply":
        session.update_task(
            task.id,
            status="failed",
            error="Invalid initial artifact decision.",
        )
        return
    assert workspace is not None
    try:
        session.update_task(task.id, status="applying", decision=decision)
        apply_receipt = apply_artifact(
            session.dir,
            task,
            workspace,
            expected_artifact_sha256=str(manifest.get("sha256", "")),
        )
    except WorkspaceError as exc:
        try:
            recovery = recover_artifact_application(
                session.dir,
                task,
                workspace,
                expected_artifact_sha256=str(
                    manifest.get("sha256", "")
                ),
            )
        except WorkspaceError as recovery_exc:
            session.update_task(
                task.id,
                status="applying",
                error=(
                    f"Artifact application requires safe recovery: {exc}; "
                    f"{recovery_exc}"
                ),
            )
            session.state["pauseReason"] = "apply_recovery_required"
            session._save()
            return
        if recovery["state"] != "applied":
            session.update_task(task.id, status="failed", error=str(exc))
            return
        apply_receipt = recovery["receipt"]
    session.update_workspace(workspace)
    session.update_task(
        task.id,
        status="verifying",
        applyReceipt=apply_receipt,
    )
    try:
        results = run_verifications(session.dir, task, workspace)
    except WorkspaceError as exc:
        session.update_task(
            task.id,
            status="verification_failed",
            error=str(exc),
        )
        return
    _record_verification_outcome(session, task, results)


def _process_failed_verification_action(
    session: Session,
    task: TaskDef,
    decision: dict[str, Any],
) -> None:
    manifest = session.state["tasks"][task.id].get("artifact") or {}
    workspace = session.workspace_snapshot()
    assert workspace is not None
    expected_policy = workspace.get("executionPolicySha256")
    if (
        isinstance(expected_policy, str)
        and decision.get("executionPolicySha256") != expected_policy
    ):
        session.update_task(
            task.id,
            status="failed",
            error=(
                "Artifact action is not bound to the approved execution "
                "policy."
            ),
        )
        return
    if decision.get("artifactSha256") != manifest.get("sha256"):
        session.update_task(
            task.id,
            status="failed",
            error="Artifact action digest does not match the persisted artifact.",
        )
        return
    if decision.get("action") == "reject":
        try:
            revert_receipt = revert_applied_artifact(
                session.dir,
                task,
                workspace,
                expected_artifact_sha256=str(
                    manifest.get("sha256", "")
                ),
            )
            session.update_workspace(workspace)
        except WorkspaceError as exc:
            session.update_task(
                task.id,
                status="verification_failed",
                error=str(exc),
            )
            return
        session.update_task(
            task.id,
            status="rejected_by_operator",
            decision=decision,
            revertReceipt=revert_receipt,
            error=decision.get("reason") or "Rejected by the operator.",
        )
        session.state.pop("pauseReason", None)
        session._save()
        return
    if decision.get("action") != "verify":
        session.update_task(
            task.id,
            status="failed",
            error="Invalid failed-verification action.",
        )
        return
    try:
        session.update_task(task.id, status="verifying", decision=decision)
        results = run_verifications(session.dir, task, workspace)
    except WorkspaceError as exc:
        session.update_task(
            task.id,
            status="verification_failed",
            error=str(exc),
        )
        return
    _record_verification_outcome(session, task, results)


def _record_verification_outcome(
    session: Session,
    task: TaskDef,
    results: list[dict[str, Any]],
) -> None:
    previous = session.state["tasks"][task.id].get(
        "verificationResults",
        [],
    )
    combined = [*previous, *results]
    passed = all(result.get("passed") for result in results)
    session.update_task(
        task.id,
        status="completed" if passed else "verification_failed",
        verificationResults=combined,
        error=(
            None
            if passed
            else "One or more allowlisted verification profiles failed."
        ),
    )
    if passed:
        session.state.pop("pauseReason", None)
        session._save()


def _approval_mode(session: Session) -> str:
    policy = session.state.get("executionPolicy")
    if isinstance(policy, dict) and policy.get("approvalMode") == "yolo":
        return "yolo"
    return "supervised"


def _local_execution_profile(
    config: SwarmConfig,
    backend: BatchBackend,
) -> dict[str, Any]:
    resolved = getattr(backend, "model_path", None)
    if not isinstance(resolved, Path):
        resolved = (
            Path(config.model.local_path).expanduser().resolve()
            if config.model.local_path
            else None
        )
    model_fingerprint = None
    model_identity = None
    if isinstance(resolved, Path) and resolved.is_dir():
        model_identity = model_directory_identity(resolved)
        model_fingerprint = model_identity["sha256"]
    return {
        "schemaVersion": 1,
        "model": {
            "repository": config.model.repository,
            "revision": config.model.revision,
            "configuredLocalPath": config.model.local_path,
            "resolvedPath": str(resolved) if resolved is not None else None,
            "fingerprint": model_fingerprint,
            "fingerprintAlgorithm": (
                model_identity["algorithm"]
                if model_identity is not None
                else None
            ),
            "fingerprintedFiles": (
                model_identity["fileCount"]
                if model_identity is not None
                else 0
            ),
        },
        "batch": {
            "maxWorkers": config.batch.max_workers,
            "prefillStepSize": config.batch.prefill_step_size,
            "maxPromptCharacters": config.batch.max_prompt_characters,
            "maxBatchPromptTokens": (
                config.batch.max_batch_prompt_tokens
            ),
        },
        "enableThinking": config.enable_thinking,
        "seed": config.seed,
        "worker": {
            "mode": config.worker.mode,
            "reasoningMaxTokens": config.worker.reasoning_max_tokens,
            "capabilities": worker_capabilities_payload(
                config.worker.capabilities
            ),
        },
        "roleDefaults": ROLE_DEFAULTS,
        "structuredOutputDefaults": {
            "temperature": 0.0,
            "topP": 1.0,
            "maxTokens": EXACT_EDIT_MAX_TOKENS,
            "appliesWhenPlanOmitsOverride": True,
        },
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "mlx": _installed_version("mlx"),
            "mlxLm": _installed_version("mlx-lm"),
            "mlxSwarm": _installed_version("mlx-swarm"),
            "sourceSha256": _package_source_sha256(),
        },
    }


def _installed_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _package_source_sha256() -> str:
    import hashlib

    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _finalize_session(session: Session, plan: Plan) -> Session:
    """Seal review evidence without exposing a false completed state."""
    _reconcile_batch_records(session)
    _block_all_remaining_descendants(session, plan)
    statuses = [session.get_task_status(task.id) for task in plan.tasks]
    if (
        statuses
        and all(status == "completed" for status in statuses)
        and plan.integration_verification
    ):
        previous = session.state.get("integrationVerificationResults", [])
        if not (
            isinstance(previous, list)
            and previous
            and all(
                isinstance(result, dict)
                and result.get("passed") is True
                for result in previous
            )
        ):
            workspace = session.workspace_snapshot()
            if workspace is None:
                session.state["integrationVerificationError"] = (
                    "Integration verification requires a workspace snapshot."
                )
            else:
                integration_task = TaskDef(
                    id="plan-integration",
                    role="test",
                    prompt="Run final allowlisted integration verification.",
                    artifact_type="report",
                    verification=plan.integration_verification,
                )
                try:
                    results = run_verifications(
                        session.dir,
                        integration_task,
                        workspace,
                    )
                except WorkspaceError as exc:
                    session.state["integrationVerificationResults"] = []
                    session.state["integrationVerificationError"] = str(exc)
                else:
                    session.state["integrationVerificationResults"] = results
                    if results and all(
                        result.get("passed") is True
                        for result in results
                    ):
                        session.state.pop(
                            "integrationVerificationError",
                            None,
                        )
                    else:
                        session.state["integrationVerificationError"] = (
                            "Final integration verification failed."
                        )
            if session.state.get("integrationVerificationError"):
                session.state["pauseReason"] = (
                    "integration_verification_failed"
                )
                session._save()
                statuses = [*statuses, "verification_failed"]
    if all(status == "completed" for status in statuses):
        desired_status = "completed"
    elif any(status == "failed" for status in statuses):
        desired_status = "failed"
    else:
        desired_status = "partial"

    if desired_status == "completed":
        try:
            frontier_result = session.write_frontier_result(
                status_override="completed",
            )
        except WorkspaceError as exc:
            session.state["pauseReason"] = (
                "finalization_validation_failed"
            )
            session.state["finalizationError"] = str(exc)
            session.set_status("partial")
            frontier_result = session.write_frontier_result(
                status_override="partial",
            )
            session.state["frontierResult"] = str(frontier_result)
            session._save()
            return session
        session.state.pop("pauseReason", None)
        session.state.pop("finalizationError", None)
        session.state["frontierResult"] = str(frontier_result)
        session.set_status("completed")
    else:
        session.set_status(desired_status)
        frontier_result = session.write_frontier_result()
        session.state["frontierResult"] = str(frontier_result)

    _release_checkout_lease_if_resolved(session)
    session._save()
    return session


def _reconcile_batch_records(session: Session) -> None:
    changed = False
    for record in session.state.get("batches", []):
        if record.get("state") != "awaiting-workspace-actions":
            continue
        task_ids = record.get("taskIds", [])
        if any(
            session.get_task_status(str(task_id))
            in {"running", "awaiting_approval", "applying", "verifying"}
            for task_id in task_ids
        ):
            continue
        record["state"] = "completed-after-resume"
        record["recoveredAt"] = _utc_now()
        record.setdefault("finishedAt", record["recoveredAt"])
        record.setdefault(
            "elapsedSeconds",
            record.get("generationElapsedSeconds", 0.0),
        )
        changed = True
    if changed:
        session._save()


@contextmanager
def _runner_lock(session_dir: Path):
    lock_path = session_dir / "runner.lock"
    handle = lock_path.open("a+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another runner already owns this session.") from exc
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def _workspace_execution_lock(config: SwarmConfig, session_dir: Path):
    """Prevent concurrent sessions from mutating the operator checkout."""
    snapshot_path = session_dir / "workspace.snapshot.json"
    if not snapshot_path.is_file():
        yield
        return
    try:
        workspace = load_workspace_snapshot(session_dir)
    except WorkspaceError:
        # The normal executor validation owns the user-facing snapshot error.
        yield
        return
    policy = workspace.get("executionPolicy")
    if (
        not isinstance(policy, dict)
        or policy.get("workspaceTarget") != "checkout"
    ):
        yield
        return
    require_checkout_lease(
        workspace,
        plan_id=str(
            workspace.get("planId", session_dir.parent.name)
        ),
        session_id=str(
            workspace.get("sessionId", session_dir.name)
        ),
    )
    del config
    lock_path = checkout_runner_lock_path(workspace)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "Another YOLO run already owns the main checkout."
            ) from exc
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _release_checkout_lease_if_resolved(session: Session) -> None:
    workspace = session.workspace_snapshot()
    if workspace is None:
        return
    policy = workspace.get("executionPolicy")
    if (
        not isinstance(policy, dict)
        or policy.get("workspaceTarget") != "checkout"
    ):
        return
    for task_id, task_state in session.state.get("tasks", {}).items():
        if task_state.get("status") in {
            "applying",
            "verifying",
            "verification_failed",
        }:
            return
        artifact_dir = session.dir / "artifacts" / task_id
        if (
            (artifact_dir / "apply-receipt.json").is_file()
            and not (artifact_dir / "revert-receipt.json").is_file()
            and task_state.get("status") != "completed"
        ):
            return
    release_checkout_lease(
        workspace,
        plan_id=session.plan.plan_id,
        session_id=session.session_id,
    )
    session.state["checkoutLeaseReleasedAt"] = _utc_now()
