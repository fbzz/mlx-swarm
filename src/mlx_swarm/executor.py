"""DAG executor — dependency-safe waves with deterministic repair loops."""
# @lat: [[Executor]]

from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .backend import BatchBackend, MLXBatchBackend
from .contracts import Plan, SwarmConfig, TaskDef
from .gates import evaluate_gate, gate_feedback_for_repair, normalize_output
from .prompting import compose_prompt, compose_repair_prompt
from .session import Session, _run_id, _utc_now
from .workspace import (
    WorkspaceError,
    apply_artifact,
    load_artifact,
    materialize_edit_manifest,
    persist_artifact,
    read_failed_verification_action,
    read_initial_decision,
    recover_artifact_application,
    revert_applied_artifact,
    run_verifications,
)


def execute_plan(
    config: SwarmConfig,
    plan: Plan,
    session_dir: Path | None = None,
    max_repair: int = 2,
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
    max_repair: int = 2,
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
    if plan.workspace_execution and not session.state.get("workspaceExecution"):
        raise RuntimeError(
            "Schema-v2 workspace plans require a digest-approved worktree snapshot."
        )
    session.set_status("running")
    _recover_interrupted_tasks(session)

    owns_backend = backend is None
    if backend is None:
        try:
            worker_backend: BatchBackend = MLXBatchBackend(config)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            for task in plan.tasks:
                if session.get_task_status(task.id) != "completed":
                    session.update_task(task.id, status="failed", error=message)
            session.set_status("failed")
            frontier_result = session.write_frontier_result()
            session.state["frontierResult"] = str(frontier_result)
            session._save()
            return session
    else:
        worker_backend = backend

    try:
        for level_idx, level_tasks in enumerate(plan.topological_order()):
            _await_workspace_tasks(
                session,
                level_tasks,
                poll_seconds=approval_poll_seconds,
            )
            _block_tasks_with_failed_dependencies(session, level_tasks)

            initial_tasks = [
                task
                for task in level_tasks
                if session.get_task_status(task.id) == "pending"
                and _dependencies_completed(session, task)
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
            # repair generation completed. Resume those tasks directly from
            # their stored gate feedback and previous output.
            resumable_rejections = [
                task
                for task in level_tasks
                if session.get_task_status(task.id) == "rejected"
                and _dependencies_completed(session, task)
                and session.state["tasks"][task.id]["repairAttempts"]
                < _repair_limit(task, max_repair)
            ]
            for chunk_idx, tasks in enumerate(
                _chunked(resumable_rejections, config.batch.max_workers)
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

    _block_all_remaining_descendants(session, plan)
    statuses = [session.get_task_status(task.id) for task in plan.tasks]
    if all(status == "completed" for status in statuses):
        session.set_status("completed")
    elif any(status == "failed" for status in statuses):
        session.set_status("failed")
    else:
        session.set_status("partial")

    frontier_result = session.write_frontier_result()
    session.state["frontierResult"] = str(frontier_result)
    session._save()
    return session


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
        session.plan = plan
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
                )
                session.update_workspace(workspace)
            except WorkspaceError as exc:
                session.update_task(
                    task_id,
                    status="failed",
                    error=(
                        "Could not safely recover interrupted application: "
                        f"{exc}"
                    ),
                )
                continue
            if recovery["state"] == "applied":
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


def _block_tasks_with_failed_dependencies(
    session: Session,
    tasks: Iterable[TaskDef],
) -> None:
    terminal_failure_states = {
        "rejected",
        "rejected_by_operator",
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

    prompts = [
        compose_prompt(plan.context, task, session=session)
        for task in tasks
    ]
    runnable_tasks, runnable_prompts = _filter_prompt_lengths(
        config,
        session,
        tasks,
        prompts,
        record,
    )

    if runnable_tasks:
        outputs, stats = _generate_or_fail(
            session,
            backend,
            runnable_tasks,
            runnable_prompts,
        )
        record["statistics"] = stats
        for task, prompt, output in zip(
            runnable_tasks,
            runnable_prompts,
            outputs,
        ):
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
    _await_workspace_tasks(
        session,
        runnable_tasks,
        poll_seconds=approval_poll_seconds,
    )
    record["finishedAt"] = _utc_now()
    record["elapsedSeconds"] = time.perf_counter() - started
    session.add_batch_record(record)


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
    _await_workspace_tasks(
        session,
        tasks,
        poll_seconds=approval_poll_seconds,
    )
    record["finishedAt"] = _utc_now()
    record["elapsedSeconds"] = time.perf_counter() - started
    session.add_batch_record(record)


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
            original_prompt = compose_prompt(
                plan.context,
                task,
                session=session,
            )
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
            outputs, stats = _generate_or_fail(
                session,
                backend,
                runnable_tasks,
                repair_prompts,
            )
            repair_record["statistics"] = stats
            for task, prompt, output in zip(
                runnable_tasks,
                repair_prompts,
                outputs,
            ):
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
        return [], {"batchSize": len(tasks), "error": message}


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
    if repeated_output and not gate_result["passed"]:
        gate_result.setdefault("violations", []).append({
            "id": "repeated-output",
            "kind": "repair",
            "message": (
                "The response exactly repeated the previous rejected output. "
                "Return a materially corrected artifact."
            ),
        })
    status = "completed" if gate_result["passed"] else "rejected"
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
        error=None,
    )


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
                    time.sleep(poll_seconds)
                    continue
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
                    time.sleep(poll_seconds)
                    continue
                request_name, decision = action
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
    if session.state.get("status") == "awaiting_approval":
        session.set_status("running")


def _process_initial_decision(
    session: Session,
    task: TaskDef,
    decision: dict[str, Any],
) -> None:
    manifest = session.state["tasks"][task.id].get("artifact") or {}
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
        return
    if decision.get("action") != "apply":
        session.update_task(
            task.id,
            status="failed",
            error="Invalid initial artifact decision.",
        )
        return
    workspace = session.workspace_snapshot()
    assert workspace is not None
    try:
        session.update_task(task.id, status="applying", decision=decision)
        apply_receipt = apply_artifact(session.dir, task, workspace)
        session.update_workspace(workspace)
        session.update_task(
            task.id,
            status="verifying",
            applyReceipt=apply_receipt,
        )
        results = run_verifications(session.dir, task, workspace)
    except WorkspaceError as exc:
        session.update_task(task.id, status="failed", error=str(exc))
        return
    _record_verification_outcome(session, task, results)


def _process_failed_verification_action(
    session: Session,
    task: TaskDef,
    decision: dict[str, Any],
) -> None:
    manifest = session.state["tasks"][task.id].get("artifact") or {}
    if decision.get("artifactSha256") != manifest.get("sha256"):
        session.update_task(
            task.id,
            status="failed",
            error="Artifact action digest does not match the persisted artifact.",
        )
        return
    workspace = session.workspace_snapshot()
    assert workspace is not None
    if decision.get("action") == "reject":
        try:
            revert_receipt = revert_applied_artifact(
                session.dir,
                task.id,
                workspace,
            )
            session.update_workspace(workspace)
        except WorkspaceError as exc:
            session.update_task(task.id, status="failed", error=str(exc))
            return
        session.update_task(
            task.id,
            status="rejected_by_operator",
            decision=decision,
            revertReceipt=revert_receipt,
            error=decision.get("reason") or "Rejected by the operator.",
        )
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
        session.update_task(task.id, status="failed", error=str(exc))
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
