"""Integration tests for dependency-safe execution and resume behavior."""
# @lat: [[Tests#Executor]]

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from mlx_swarm.contracts import (
    BatchConfig,
    GatePattern,
    ModelConfig,
    OutputGate,
    Plan,
    SwarmConfig,
    TaskDef,
    WorkerConfig,
    load_config,
    load_plan,
)
from mlx_swarm.executor import (
    _generate_with_worker_strategy,
    _local_execution_profile,
    _repair_rejected_tasks,
    _task_output_hit_token_limit,
    _worker_strategy_compatible,
    execute_plan,
)
from mlx_swarm.model_identity import model_metadata
from mlx_swarm.session import Session


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


class TokenLimitBackend(FakeBackend):
    def generate(
        self,
        tasks: list[TaskDef],
        prompts: list[str],
    ) -> tuple[list[str], dict[str, Any]]:
        self.calls.append((tasks, prompts))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response, {
            "batchSize": len(tasks),
            "groups": [{
                "taskIds": [task.id for task in tasks],
                "hitTokenLimit": {
                    task.id: True for task in tasks
                },
            }],
        }


def test_legacy_worker_strategy_snapshot_remains_compatible() -> None:
    current = {
        "mode": "direct",
        "reasoningMaxTokens": 1200,
        "capabilities": {"parameterScale": "4B"},
    }
    assert _worker_strategy_compatible({
        "mode": "direct",
        "reasoningMaxTokens": 1200,
    }, current)
    assert not _worker_strategy_compatible({
        "mode": "reasoning-edit",
        "reasoningMaxTokens": 1200,
    }, current)


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


def test_deterministic_edit_gate_failure_names_actual_violation(
    tmp_path: Path,
) -> None:
    task = TaskDef(
        id="frozen-edit",
        role="implementation",
        prompt="Apply the embedded edits.",
        gate=OutputGate(
            max_characters=10,
            output_format="json",
            json_required_keys=("edits",),
            json_allowed_keys=("edits",),
        ),
        artifact_type="patch",
        worker_output_protocol="edit-manifest-v1",
        execution_mode="deterministic-edit",
        deterministic_edits=(
            {"path": "src/value.py", "old": "VALUE = 1", "new": "VALUE = 2"},
        ),
    )
    backend = FakeBackend([])

    session = execute_plan(
        _config(tmp_path),
        _plan(tmp_path, (task,), "deterministic-gate-error"),
        backend=backend,
    )

    assert session.get_task_status("frozen-edit") == "failed"
    error = session.state["tasks"]["frozen-edit"]["error"]
    assert "max-characters" in error
    assert "structural workspace validation" not in error
    assert len(backend.calls) == 0


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


def test_resume_preserves_original_global_repair_cap(tmp_path: Path) -> None:
    task = TaskDef(
        id="task",
        role="implementation",
        prompt="Generate PASS",
        gate=OutputGate(
            required_patterns=(GatePattern("must-pass", "PASS"),),
        ),
        max_repair_attempts=2,
    )
    config = _config(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({
            "schemaVersion": 1,
            "planId": "resume-repair-cap",
            "objective": "Test immutable resume repair cap",
            "tasks": [{
                "id": "task",
                "role": "implementation",
                "prompt": "Generate PASS",
                "gate": {
                    "requiredPatterns": [{
                        "id": "must-pass",
                        "pattern": "PASS",
                    }],
                    "forbiddenPatterns": [],
                    "maxCharacters": 2000,
                },
                "maxRepairAttempts": 2,
            }],
        }),
        encoding="utf-8",
    )
    plan = load_plan(plan_path, config)
    session_dir = config.artifacts_dir / plan.plan_id / "fixed-session"
    first_backend = FakeBackend([["bad output"]])
    first = execute_plan(
        config,
        plan,
        session_dir=session_dir,
        max_repair=0,
        backend=first_backend,
    )
    assert first.state["maxRepair"] == 0

    second_backend = FakeBackend([["PASS"]])
    second = execute_plan(
        config,
        plan,
        session_dir=session_dir,
        max_repair=2,
        backend=second_backend,
    )

    assert second.state["maxRepair"] == 0
    assert second.get_task_status("task") == "rejected"
    assert second_backend.calls == []


def test_local_model_fingerprint_detects_same_size_replacement(
    tmp_path: Path,
) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    weight = model_dir / "weights.safetensors"
    weight.write_bytes(b"aaaa")
    config = SwarmConfig(
        source=tmp_path / "swarm.json",
        model=ModelConfig("unused", "", str(model_dir)),
        batch=BatchConfig(max_workers=4),
        artifacts_dir=tmp_path / "runs",
    )
    backend = FakeBackend([])

    before = _local_execution_profile(config, backend)
    cache = model_dir / ".cache"
    cache.mkdir()
    (cache / "download.incomplete").write_bytes(b"transient")
    after_cache_churn = _local_execution_profile(config, backend)
    weight.write_bytes(b"bbbb")
    after = _local_execution_profile(config, backend)

    assert (
        before["model"]["fingerprint"]
        == after_cache_churn["model"]["fingerprint"]
    )
    assert before["model"]["fingerprint"] != after["model"]["fingerprint"]


def test_model_metadata_detects_context_overstatement(
    tmp_path: Path,
) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text(
        json.dumps({
            "model_type": "qwen3_5",
            "text_config": {
                "model_type": "qwen3_5_text",
                "max_position_embeddings": 262144,
            },
            "quantization": {"bits": 6},
        }),
        encoding="utf-8",
    )

    compatible = model_metadata(
        model_dir,
        declared_context_tokens=262144,
    )
    overstated = model_metadata(
        model_dir,
        declared_context_tokens=300000,
    )

    assert compatible["contextCompatible"] is True
    assert compatible["quantizationBits"] == 6
    assert compatible["modelType"] == "qwen3_5_text"
    assert overstated["contextCompatible"] is False
    assert "exceeds checkpoint metadata" in overstated["warnings"][0]


def test_generation_attempts_stop_after_repeated_rejected_output(
    tmp_path: Path,
) -> None:
    task = TaskDef(
        id="task",
        role="implementation",
        prompt="Generate PASS",
        gate=OutputGate(
            required_patterns=(GatePattern("must-pass", "PASS"),),
        ),
        max_repair_attempts=2,
    )
    backend = FakeBackend([
        ["bad output"],
        ["bad output"],
        ["different but still bad"],
    ])

    session = execute_plan(
        _config(tmp_path),
        _plan(tmp_path, (task,), "attempt-audit"),
        max_repair=2,
        backend=backend,
    )

    attempts = session.state["tasks"]["task"]["generationAttempts"]
    assert len(attempts) == 2
    assert attempts[0]["repeatedOutput"] is False
    assert attempts[1]["repeatedOutput"] is True
    assert session.get_task_status("task") == "failed"
    assert "further repairs were stopped" in session.state["tasks"]["task"][
        "error"
    ].lower()
    records = [
        json.loads((session.dir / attempt["path"]).read_text())
        for attempt in attempts
    ]
    assert [record["phase"] for record in records] == [
        "generation",
        "repair-1",
    ]
    assert [record["output"] for record in records] == [
        "bad output",
        "bad output",
    ]
    assert records[1]["promptSha256"] == attempts[1]["promptSha256"]


def test_generation_stops_without_repair_after_output_token_limit(
    tmp_path: Path,
) -> None:
    task = TaskDef(
        id="task",
        role="implementation",
        prompt="Generate PASS",
        gate=OutputGate(
            required_patterns=(GatePattern("must-pass", "PASS"),),
        ),
        max_repair_attempts=2,
    )
    backend = TokenLimitBackend([
        ["truncated output"],
        ["unused repair"],
    ])

    session = execute_plan(
        _config(tmp_path),
        _plan(tmp_path, (task,), "token-limit-stop"),
        max_repair=0,
        backend=backend,
    )

    assert len(backend.calls) == 1
    assert session.get_task_status("task") == "failed"
    task_state = session.state["tasks"]["task"]
    assert task_state["repairAttempts"] == 0
    assert "split the task" in task_state["error"].lower()
    assert task_state["gateResult"]["violations"][-1]["id"] == (
        "output-token-limit"
    )


class TruncateFirstCallBackend(FakeBackend):
    def generate(
        self,
        tasks: list[TaskDef],
        prompts: list[str],
    ) -> tuple[list[str], dict[str, Any]]:
        self.calls.append((tasks, prompts))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        truncated = len(self.calls) == 1
        return response, {
            "batchSize": len(tasks),
            "groups": [{
                "taskIds": [task.id for task in tasks],
                "hitTokenLimit": {
                    task.id: truncated for task in tasks
                },
            }],
        }


def test_truncated_generation_escalates_ceiling_and_repairs(
    tmp_path: Path,
) -> None:
    task = TaskDef(
        id="task",
        role="implementation",
        prompt="Generate PASS",
        gate=OutputGate(
            required_patterns=(GatePattern("must-pass", "PASS"),),
        ),
        max_repair_attempts=1,
    )
    backend = TruncateFirstCallBackend([
        ["truncated output"],
        ["PASS after escalation"],
    ])

    session = execute_plan(
        _config(tmp_path),
        _plan(tmp_path, (task,), "token-limit-escalate"),
        max_repair=1,
        backend=backend,
    )

    assert session.get_task_status("task") == "completed"
    assert len(backend.calls) == 2
    escalated = backend.calls[1][0][0]
    assert escalated.generation_override["max_tokens"] == 2048
    task_state = session.state["tasks"]["task"]
    assert task_state["repairAttempts"] == 1
    assert task_state["escalatedMaxTokens"] == 2048


def test_truncated_generation_at_capability_cap_fails_fast(
    tmp_path: Path,
) -> None:
    task = TaskDef(
        id="task",
        role="implementation",
        prompt="Generate PASS",
        gate=OutputGate(
            required_patterns=(GatePattern("must-pass", "PASS"),),
        ),
        max_repair_attempts=2,
        generation_override={"max_tokens": 2048},
    )
    backend = TokenLimitBackend([
        ["truncated output"],
        ["unused repair"],
    ])

    session = execute_plan(
        _config(tmp_path),
        _plan(tmp_path, (task,), "token-limit-at-cap"),
        max_repair=2,
        backend=backend,
    )

    assert len(backend.calls) == 1
    assert session.get_task_status("task") == "failed"
    task_state = session.state["tasks"]["task"]
    assert task_state["repairAttempts"] == 0
    assert "split the task" in task_state["error"].lower()


class SuspectedLimitBackend(FakeBackend):
    def generate(
        self,
        tasks: list[TaskDef],
        prompts: list[str],
    ) -> tuple[list[str], dict[str, Any]]:
        self.calls.append((tasks, prompts))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response, {
            "batchSize": len(tasks),
            "groups": [{
                "taskIds": [task.id for task in tasks],
                "hitTokenLimit": {
                    task.id: False for task in tasks
                },
                "suspectedTokenLimit": {
                    task.id: True for task in tasks
                },
            }],
        }


def test_gate_passing_output_near_ceiling_is_not_truncated(
    tmp_path: Path,
) -> None:
    task = TaskDef(
        id="task",
        role="implementation",
        prompt="Generate PASS",
        gate=OutputGate(
            required_patterns=(GatePattern("must-pass", "PASS"),),
        ),
        max_repair_attempts=1,
    )
    backend = SuspectedLimitBackend([["PASS near the ceiling"]])

    session = execute_plan(
        _config(tmp_path),
        _plan(tmp_path, (task,), "suspected-but-complete"),
        max_repair=1,
        backend=backend,
    )

    assert session.get_task_status("task") == "completed"
    assert len(backend.calls) == 1


def test_gate_failing_output_near_ceiling_is_treated_as_truncated(
    tmp_path: Path,
) -> None:
    task = TaskDef(
        id="task",
        role="implementation",
        prompt="Generate PASS",
        gate=OutputGate(
            required_patterns=(GatePattern("must-pass", "PASS"),),
        ),
        max_repair_attempts=0,
    )
    backend = SuspectedLimitBackend([["truncated garbag"]])

    session = execute_plan(
        _config(tmp_path),
        _plan(tmp_path, (task,), "suspected-and-failing"),
        max_repair=0,
        backend=backend,
    )

    assert session.get_task_status("task") == "failed"
    task_state = session.state["tasks"]["task"]
    assert task_state["gateResult"]["violations"][-1]["id"] == (
        "output-token-limit"
    )


def test_recorded_escalation_survives_plan_derived_task_resume(
    tmp_path: Path,
) -> None:
    task = TaskDef(
        id="task",
        role="implementation",
        prompt="Generate PASS",
        gate=OutputGate(
            required_patterns=(GatePattern("must-pass", "PASS"),),
        ),
        max_repair_attempts=2,
    )
    config = _config(tmp_path)
    plan = _plan(tmp_path, (task,), "escalation-resume")
    session = Session(tmp_path / "runs" / "escalation-resume", plan)
    session.update_task(
        "task",
        status="rejected",
        output="truncated output",
        gateResult={
            "configured": True,
            "passed": False,
            "violations": [{
                "id": "output-token-limit",
                "kind": "size",
                "message": "Generation reached its output-token limit.",
            }],
            "normalizations": [],
        },
        repairAttempts=1,
        escalatedMaxTokens=2048,
    )
    backend = FakeBackend([["unused"]])

    _repair_rejected_tasks(
        config,
        plan,
        session,
        backend,
        [task],
        2,
        {},
    )

    assert len(backend.calls) == 0
    assert session.get_task_status("task") == "failed"
    error = session.state["tasks"]["task"]["error"]
    assert "no escalation headroom" in error


def test_repair_skips_deterministic_replay_of_recorded_dispatch(
    tmp_path: Path,
) -> None:
    task = TaskDef(
        id="task",
        role="implementation",
        prompt="Generate PASS",
        gate=OutputGate(
            required_patterns=(GatePattern("must-pass", "PASS"),),
        ),
        max_repair_attempts=1,
        generation_override={"temperature": 0.0},
    )
    config = _config(tmp_path)
    plan = _plan(tmp_path, (task,), "replay-skip")
    session = Session(tmp_path / "runs" / "replay-skip", plan)
    rejected_state = {
        "status": "rejected",
        "output": "bad output",
        "gateResult": {
            "configured": True,
            "passed": False,
            "violations": [{
                "id": "must-pass",
                "kind": "required-pattern",
                "message": "Required pattern not found: must-pass.",
            }],
            "normalizations": [],
        },
        "repairAttempts": 0,
    }
    session.update_task("task", **rejected_state)
    first_backend = FakeBackend([["still bad"]])

    _repair_rejected_tasks(
        config,
        plan,
        session,
        first_backend,
        [task],
        1,
        {},
    )

    assert len(first_backend.calls) == 1
    assert len(session.state["tasks"]["task"]["repairSignatures"]) == 1

    # Simulate resume after an interrupted dispatch: the pre-dispatch task
    # state is restored while the recorded signature survives.
    session.update_task("task", **rejected_state)
    replay_backend = FakeBackend([["unused"]])

    _repair_rejected_tasks(
        config,
        plan,
        session,
        replay_backend,
        [task],
        1,
        {},
    )

    assert len(replay_backend.calls) == 0
    assert session.get_task_status("task") == "failed"
    error = session.state["tasks"]["task"]["error"]
    assert "deterministically replay" in error


def test_token_limit_detection_uses_final_reasoning_edit_stage() -> None:
    assert _task_output_hit_token_limit({
        "groups": [
            {"hitTokenLimit": {"task": True}},
            {"hitTokenLimit": {"task": False}},
        ],
    }, "task") is False
    assert _task_output_hit_token_limit({
        "groups": [
            {"hitTokenLimit": {"task": False}},
            {"hitTokenLimit": {"task": True}},
        ],
    }, "task") is True


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
        "mlx_swarm.executor.MLXBatchBackend",
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


def test_reasoning_edit_worker_uses_two_local_stages_and_records_reasoning(
    tmp_path: Path,
) -> None:
    task = TaskDef(
        id="change",
        role="implementation",
        prompt="Fix the target.",
        artifact_type="patch",
        allowed_paths=("src/value.py",),
    )
    plan = _plan(tmp_path, (task,), "two-stage")
    session = Session(tmp_path / "session", plan)
    config = _config(tmp_path)
    config = SwarmConfig(
        source=config.source,
        model=config.model,
        batch=config.batch,
        artifacts_dir=config.artifacts_dir,
        worker=WorkerConfig(
            mode="reasoning-edit",
            reasoning_max_tokens=512,
        ),
    )
    backend = FakeBackend([
        ["The direct assignment in src/value.py is the causal site."],
        ['{"schemaVersion":1,"edits":[]}'],
    ])

    outputs, statistics, stages = _generate_with_worker_strategy(
        config,
        session,
        backend,
        [task],
        ["STRICT ARTIFACT PROMPT"],
        phase="generation",
    )

    assert outputs == ['{"schemaVersion":1,"edits":[]}']
    assert [stage["name"] for stage in stages] == ["reasoning", "editing"]
    assert statistics["batchSize"] == 2
    assert statistics["generationCalls"] == 2
    assert backend.calls[0][0][0].generation_override["enable_thinking"] is True
    assert backend.calls[0][0][0].generation_override["max_tokens"] == 512
    assert backend.calls[1][0][0].generation_override["enable_thinking"] is False
    assert "LOCAL REASONING EVIDENCE" in backend.calls[1][1][0]
    reasoning = session.state["tasks"]["change"]["reasoningAttempts"]
    assert len(reasoning) == 1
    record = json.loads((session.dir / reasoning[0]["path"]).read_text())
    assert record["authoritative"] is False
    assert record["output"].startswith("The direct assignment")
