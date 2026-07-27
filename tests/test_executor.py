"""Integration tests for dependency-safe execution and resume behavior."""
# @lat: [[Tests#Executor]]

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from swarm_agents.contracts import (
    BatchConfig,
    GatePattern,
    ModelConfig,
    OutputGate,
    Plan,
    SwarmConfig,
    TaskDef,
    load_config,
    load_plan,
)
from swarm_agents.executor import execute_plan
from swarm_agents.session import Session


class FakeBackend:
    def __init__(self, responses: list[list[str] | Exception]):
        self.responses = list(responses)
        self.calls: list[tuple[list[TaskDef], list[str]]] = []
        self.closed = False

    def generate(
        self,
        tasks: list[TaskDef],
        prompts: list[str],
    ) -> tuple[list[str], dict[str, Any]]:
        self.calls.append((tasks, prompts))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response, {"batchSize": len(tasks)}

    def close(self) -> None:
        self.closed = True


def _config(tmp_path: Path, *, max_workers: int = 8) -> SwarmConfig:
    return SwarmConfig(
        source=tmp_path / "swarm.json",
        model=ModelConfig("unused", ""),
        batch=BatchConfig(max_workers=max_workers),
        artifacts_dir=tmp_path / "runs",
    )


def _plan(tmp_path: Path, tasks: tuple[TaskDef, ...], plan_id: str) -> Plan:
    return Plan(
        source=tmp_path / "plan.json",
        plan_id=plan_id,
        objective="Test execution",
        context=None,
        tasks=tasks,
        raw={},
    )


def test_rejected_dependency_blocks_descendant(tmp_path: Path) -> None:
    parent = TaskDef(
        id="parent",
        role="implementation",
        prompt="Generate PASS",
        gate=OutputGate(
            required_patterns=(GatePattern("must-pass", "PASS"),),
        ),
        max_repair_attempts=0,
    )
    child = TaskDef(
        id="child",
        role="test",
        prompt="Consume parent",
        depends_on=("parent",),
    )
    backend = FakeBackend([["bad output"]])

    session = execute_plan(
        _config(tmp_path),
        _plan(tmp_path, (parent, child), "blocked-child"),
        backend=backend,
    )

    assert session.get_task_status("parent") == "rejected"
    assert session.get_task_status("child") == "blocked"
    assert len(backend.calls) == 1
    assert [task.id for task in backend.calls[0][0]] == ["parent"]


def test_global_repair_cap_zero_disables_repairs(tmp_path: Path) -> None:
    task = TaskDef(
        id="task",
        role="implementation",
        prompt="Generate PASS",
        gate=OutputGate(
            required_patterns=(GatePattern("must-pass", "PASS"),),
        ),
        max_repair_attempts=2,
    )
    backend = FakeBackend([["bad output"]])

    session = execute_plan(
        _config(tmp_path),
        _plan(tmp_path, (task,), "repair-cap"),
        max_repair=0,
        backend=backend,
    )

    assert len(backend.calls) == 1
    assert session.state["tasks"]["task"]["repairAttempts"] == 0
    assert session.get_task_status("task") == "rejected"


def test_wide_level_is_chunked_to_max_workers(tmp_path: Path) -> None:
    tasks = tuple(
        TaskDef(id=f"task-{index}", role="general", prompt="work")
        for index in range(5)
    )
    backend = FakeBackend([
        ["a", "b"],
        ["c", "d"],
        ["e"],
    ])

    session = execute_plan(
        _config(tmp_path, max_workers=2),
        _plan(tmp_path, tasks, "chunked"),
        backend=backend,
    )

    assert session.state["status"] == "completed"
    assert [len(call[0]) for call in backend.calls] == [2, 2, 1]
    assert all(record["elapsedSeconds"] >= 0 for record in session.state["batches"])


def test_backend_failure_marks_task_failed_and_child_blocked(
    tmp_path: Path,
) -> None:
    parent = TaskDef(id="parent", role="general", prompt="work")
    child = TaskDef(
        id="child",
        role="general",
        prompt="consume",
        depends_on=("parent",),
    )
    backend = FakeBackend([RuntimeError("generation failed")])

    session = execute_plan(
        _config(tmp_path),
        _plan(tmp_path, (parent, child), "backend-failure"),
        backend=backend,
    )

    assert session.state["status"] == "failed"
    assert session.get_task_status("parent") == "failed"
    assert session.get_task_status("child") == "blocked"
    assert "generation failed" in session.state["tasks"]["parent"]["error"]


def test_backend_initialization_failure_is_persisted(tmp_path: Path) -> None:
    task = TaskDef(id="task", role="general", prompt="work")
    with patch(
        "swarm_agents.executor.MLXBatchBackend",
        side_effect=RuntimeError("model unavailable"),
    ):
        session = execute_plan(
            _config(tmp_path),
            _plan(tmp_path, (task,), "backend-init-failure"),
        )

    assert session.state["status"] == "failed"
    assert session.get_task_status("task") == "failed"
    assert "model unavailable" in session.state["tasks"]["task"]["error"]
    assert Path(session.state["frontierResult"]).is_file()


def test_resume_preserves_completed_tasks(tmp_path: Path) -> None:
    config_path = tmp_path / "swarm.json"
    config_path.write_text(json.dumps({
        "schemaVersion": 1,
        "model": {"repository": "unused"},
        "batch": {"maxWorkers": 2},
        "artifacts": str(tmp_path / "runs"),
    }))
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "schemaVersion": 1,
        "planId": "resume-plan",
        "objective": "Resume safely",
        "tasks": [
            {"id": "done", "role": "general", "prompt": "work"},
        ],
    }))
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    session_dir = tmp_path / "existing-session"
    existing = Session(session_dir, plan)
    existing.state["configSource"] = str(config_path)
    existing.state["planSource"] = str(plan_path)
    existing.update_task(
        "done",
        status="completed",
        output="preserve me",
        normalizedOutput="preserve me",
    )
    existing_id = existing.session_id
    backend = FakeBackend([])

    resumed = execute_plan(
        config,
        plan,
        session_dir=session_dir,
        backend=backend,
    )

    assert resumed.session_id == existing_id
    assert resumed.get_task_output("done") == "preserve me"
    assert backend.calls == []
    assert (session_dir / "frontier-result.json").is_file()


def test_frontier_packet_omits_rejected_output(tmp_path: Path) -> None:
    task = TaskDef(
        id="task",
        role="general",
        prompt="Generate PASS",
        gate=OutputGate(
            required_patterns=(GatePattern("must-pass", "PASS"),),
        ),
        max_repair_attempts=0,
    )
    backend = FakeBackend([["large rejected candidate"]])

    session = execute_plan(
        _config(tmp_path),
        _plan(tmp_path, (task,), "frontier-packet"),
        backend=backend,
    )
    packet = json.loads(
        Path(session.state["frontierResult"]).read_text(encoding="utf-8")
    )

    assert packet["reviewMode"] == "frontier-final-only"
    assert packet["tasks"]["task"]["status"] == "rejected"
    assert packet["tasks"]["task"]["output"] is None
    assert packet["localUsage"]["generationCalls"] == 1
