"""Tests for session persistence and state management."""
# @lat: [[Tests#Session]]

from __future__ import annotations

import json
from pathlib import Path

from swarm_agents.contracts import Plan, TaskDef, load_config, load_plan
from swarm_agents.session import Session


def _make_plan() -> Plan:
    tasks = (
        TaskDef(id="a", role="implementation", prompt="code"),
        TaskDef(id="b", role="test", prompt="test", depends_on=("a",)),
    )
    return Plan(
        source=Path("/tmp/plan.json"),
        plan_id="test-plan",
        objective="Test",
        context=None,
        tasks=tasks,
        raw={},
    )


def test_session_init(tmp_path: Path) -> None:
    plan = _make_plan()
    session = Session(tmp_path / "s1", plan)
    assert session.session_id.startswith("20")
    assert session.state["planId"] == "test-plan"
    assert "a" in session.state["tasks"]
    assert "b" in session.state["tasks"]
    assert session.state["tasks"]["a"]["status"] == "pending"
    assert (tmp_path / "s1" / "session.json").is_file()


def test_session_update_task(tmp_path: Path) -> None:
    plan = _make_plan()
    session = Session(tmp_path / "s1", plan)
    session.update_task("a", status="completed", output="def foo(): pass")
    assert session.get_task_status("a") == "completed"
    assert session.get_task_output("a") == "def foo(): pass"


def test_session_get_task_output(tmp_path: Path) -> None:
    plan = _make_plan()
    session = Session(tmp_path / "s1", plan)
    assert session.get_task_output("a") is None
    session.update_task(
        "a",
        status="completed",
        output="code",
        normalizedOutput="normalized code",
    )
    assert session.get_task_output("a") == "normalized code"


def test_session_summary(tmp_path: Path) -> None:
    plan = _make_plan()
    session = Session(tmp_path / "s1", plan)
    session.update_task("a", status="completed")
    summary = session.summary()
    assert summary["total"] == 2
    assert summary["completed"] == 1
    assert summary["pending"] == 1


def test_session_export_results(tmp_path: Path) -> None:
    plan = _make_plan()
    session = Session(tmp_path / "s1", plan)
    session.update_task("a", status="completed", output="code", normalizedOutput="def foo(): pass")
    results = session.export_results()
    assert "a" in results["tasks"]
    assert results["tasks"]["a"]["status"] == "completed"
    assert results["tasks"]["a"]["output"] == "def foo(): pass"


def test_session_add_batch_record(tmp_path: Path) -> None:
    plan = _make_plan()
    session = Session(tmp_path / "s1", plan)
    session.add_batch_record({"levelIndex": 0, "phase": "generation"})
    assert len(session.state["batches"]) == 1
    assert session.state["batches"][0]["levelIndex"] == 0


def test_session_set_status(tmp_path: Path) -> None:
    plan = _make_plan()
    session = Session(tmp_path / "s1", plan)
    session.set_status("running")
    assert session.state["status"] == "running"
    assert "finishedAt" not in session.state
    session.set_status("completed")
    assert "finishedAt" in session.state


def test_session_persist_and_reload(tmp_path: Path) -> None:
    plan = _make_plan()
    session_dir = tmp_path / "s1"
    session = Session(session_dir, plan)
    session.state["configSource"] = "/tmp/swarm.json"
    session.state["planSource"] = str(plan.source)
    session._save()
    session.update_task("a", status="completed", output="code")

    state = json.loads((session_dir / "session.json").read_text())
    assert state["tasks"]["a"]["status"] == "completed"


def test_session_snapshot_survives_source_plan_removal(
    tmp_path: Path,
) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    config_path = tmp_path / "swarm.json"
    config_path.write_text(json.dumps({
        "schemaVersion": 1,
        "model": {
            "repository": "local/test",
            "localPath": str(model_dir),
        },
        "batch": {},
        "artifacts": str(tmp_path / "runs"),
    }))
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "schemaVersion": 1,
        "planId": "snapshot-plan",
        "objective": "Keep history",
        "tasks": [
            {"id": "a", "role": "general", "prompt": "Do the work"}
        ],
    }))
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    session = Session(tmp_path / "run", plan)
    session.set_sources(config_source=config_path, plan_source=plan_path)
    plan_path.unlink()

    loaded = Session.load(session.dir, config)
    assert loaded.plan.plan_id == "snapshot-plan"
    assert loaded.plan.tasks[0].prompt == "Do the work"


def test_session_loads_legacy_state_without_snapshot(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    config_path = tmp_path / "swarm.json"
    config_path.write_text(json.dumps({
        "schemaVersion": 1,
        "model": {"repository": "local/test", "localPath": str(model_dir)},
        "batch": {},
        "artifacts": str(tmp_path / "runs"),
    }))
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "schemaVersion": 1,
        "planId": "legacy-plan",
        "objective": "Load old state",
        "tasks": [{"id": "a", "role": "general", "prompt": "Work"}],
    }))
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    session = Session(tmp_path / "legacy", plan)
    session.set_sources(config_source=config_path, plan_source=plan_path)
    session.state.pop("planSnapshot")
    (session.dir / "plan.snapshot.json").unlink()
    session.state.pop("launchSource")
    session._save()

    loaded = Session.load(session.dir)
    assert loaded.plan.plan_id == "legacy-plan"
    assert loaded.state.get("launchSource", "cli") == "cli"
