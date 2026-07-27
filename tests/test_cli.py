"""Tests for the CLI entrypoint — doctor, run (mocked), inspect, list."""
# @lat: [[Tests#CLI]]

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from swarm_agents.cli import main


def _write_config(tmp_path: Path) -> Path:
    config = {
        "schemaVersion": 1,
        "model": {"repository": "mlx-community/test"},
        "batch": {"maxWorkers": 8},
        "artifacts": str(tmp_path / "runs"),
    }
    p = tmp_path / "swarm.json"
    p.write_text(json.dumps(config))
    return p


def _write_plan(tmp_path: Path) -> Path:
    plan = {
        "schemaVersion": 1,
        "planId": "cli-test",
        "objective": "Test",
        "tasks": [{"id": "t1", "role": "general", "prompt": "Say hello"}],
    }
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan))
    return p


def test_cli_doctor_ready(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}")
    (model_dir / "model.safetensors").write_text("")
    (model_dir / "tokenizer.json").write_text("{}")

    # Patch _resolve_model_path to return our fake dir
    with patch("swarm_agents.backend._resolve_model_path", return_value=model_dir):
        result = main(["--config", str(config_path), "doctor"])
    assert result == 0


def test_cli_doctor_not_ready(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    with patch("swarm_agents.backend._resolve_model_path", side_effect=RuntimeError("not found")):
        result = main(["--config", str(config_path), "doctor"])
    assert result == 1


def test_cli_run_success(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    plan_path = _write_plan(tmp_path)

    mock_session = MagicMock()
    mock_session.summary.return_value = {"status": "completed", "total": 1, "completed": 1}
    mock_session.export_results.return_value = {"tasks": {}}

    with patch("swarm_agents.cli.execute_plan", return_value=mock_session):
        result = main(["--config", str(config_path), "run", str(plan_path)])
    assert result == 0


def test_cli_run_partial(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    plan_path = _write_plan(tmp_path)

    mock_session = MagicMock()
    mock_session.summary.return_value = {"status": "partial", "total": 1, "completed": 0}

    with patch("swarm_agents.cli.execute_plan", return_value=mock_session):
        result = main(["--config", str(config_path), "run", str(plan_path)])
    assert result == 1


def test_cli_list_empty(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    result = main(["--config", str(config_path), "list"])
    assert result == 0


def test_cli_list_with_sessions(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    artifacts = tmp_path / "runs"
    plan_dir = artifacts / "plan-1"
    session_dir = plan_dir / "20260727T120000Z-abcdef12"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(json.dumps({
        "sessionId": "20260727T120000Z-abcdef12",
        "planId": "plan-1",
        "status": "completed",
    }))

    result = main(["--config", str(config_path), "list"])
    assert result == 0


def test_cli_inspect(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    artifacts = tmp_path / "runs"
    session_dir = artifacts / "plan-1" / "20260727T120000Z-abcdef12"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(json.dumps({
        "sessionId": "20260727T120000Z-abcdef12",
        "planId": "plan-1",
        "status": "completed",
        "tasks": {"t1": {"id": "t1", "status": "completed", "output": "hello"}},
    }))

    with patch("swarm_agents.session.Session.load") as mock_load:
        mock_session = MagicMock()
        mock_session.summary.return_value = {"status": "completed"}
        mock_session.state = {"tasks": {"t1": {"id": "t1", "status": "completed", "output": "hello"}}}
        mock_load.return_value = mock_session
        result = main(["--config", str(config_path), "inspect", str(session_dir)])
    assert result == 0


def test_cli_inspect_task_output(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)

    with patch("swarm_agents.session.Session.load") as mock_load:
        mock_session = MagicMock()
        mock_session.state = {"tasks": {"t1": {"output": "hello world", "normalizedOutput": "hello world"}}}
        mock_load.return_value = mock_session
        result = main(["--config", str(config_path), "inspect", "/tmp/fake", "--task", "t1", "--output"])
    assert result == 0


def test_cli_contract_error(tmp_path: Path) -> None:
    config_path = tmp_path / "bad.json"
    config_path.write_text(json.dumps({"schemaVersion": 99, "model": {"repository": "x"}, "batch": {}, "artifacts": "."}))
    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(config_path), "doctor"])
    assert exc_info.value.code == 2


def test_cli_ui_uses_config_directory_and_no_open(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    with patch("swarm_agents.ui.serve_ui") as serve:
        result = main([
            "--config",
            str(config_path),
            "ui",
            "--port",
            "0",
            "--no-open",
        ])
    assert result == 0
    config, plans_dir = serve.call_args.args
    assert config.source == config_path.resolve()
    assert plans_dir == config_path.resolve().parent
    assert serve.call_args.kwargs == {
        "host": "127.0.0.1",
        "port": 0,
        "open_browser": False,
    }


def test_cli_ui_rejects_invalid_port(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main([
            "--config",
            str(config_path),
            "ui",
            "--port",
            "70000",
            "--no-open",
        ])
    assert exc_info.value.code == 2
