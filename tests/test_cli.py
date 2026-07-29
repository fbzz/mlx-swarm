"""Tests for the CLI entrypoint — doctor, run (mocked), inspect, list."""
# @lat: [[Tests#CLI]]

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from mlx_swarm.cli import main
from mlx_swarm.contracts import load_config, load_plan
from mlx_swarm.session import Session
from mlx_swarm.workspace import execution_preview, persist_artifact


def test_legacy_swarm_help_warns_before_argparse_exits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["swarm", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "deprecated" in captured.err
    assert "mlx-swarm" in captured.err


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
        "context": {
            "objective": "Test",
            "authoritativeSources": [{
                "label": "request",
                "content": "The requested greeting is hello.",
            }],
            "constraints": [],
            "rejectionCriteria": ["The greeting is missing."],
            "outputProtocol": "Return the greeting.",
            "diagnosis": {
                "observedFailure": "No greeting has been produced.",
                "causalHypothesis": "The greeting task has not run.",
                "validationMethod": "source-trace",
                "validationEvidence": (
                    "The request source requires a hello greeting."
                ),
                "falsificationCondition": (
                    "A greeting is already present in the task output."
                ),
                "evidenceSources": ["request"],
                "changeValidation": {
                    "candidateChange": (
                        "Produce the requested hello greeting exactly once."
                    ),
                    "failingPathPrediction": (
                        "The empty output gains the required greeting."
                    ),
                    "preservedControlPrediction": (
                        "No unrelated output or behavior is changed."
                    ),
                    "minimalityEvidence": (
                        "A single greeting is the smallest change satisfying "
                        "the request."
                    ),
                    "evidenceSources": ["request"],
                },
            },
        },
        "tasks": [{"id": "t1", "role": "general", "prompt": "Say hello"}],
    }
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan))
    return p


def _write_workspace_v2(
    tmp_path: Path,
) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "config",
            "user.email",
            "test@example.invalid",
        ],
        check=True,
    )
    (repo / ".gitignore").write_text("config/.swarm/\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    config_dir = repo / "config"
    config_dir.mkdir()
    config_path = config_dir / "swarm.json"
    config_path.write_text(
        json.dumps({
            "schemaVersion": 2,
            "model": {"repository": "local/test"},
            "batch": {"maxWorkers": 2},
            "artifacts": ".swarm/runs",
            "workspace": {
                "writeRoots": ["src"],
                "verificationProfiles": {},
            },
        }),
        encoding="utf-8",
    )
    plan_path = config_dir / "plan.json"
    plan_path.write_text(
        json.dumps({
            "schemaVersion": 2,
            "planId": "cli-workspace",
            "objective": "Change one file",
            "tasks": [{
                "id": "change",
                "role": "implementation",
                "prompt": "Return a diff.",
                "artifactType": "patch",
                "allowedPaths": ["src/value.py"],
                "verification": [],
            }],
        }),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "."],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "base"],
        check=True,
    )
    return repo, config_path, plan_path


def _workspace_diff() -> str:
    return (
        "diff --git a/src/value.py b/src/value.py\n"
        "--- a/src/value.py\n"
        "+++ b/src/value.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )


def test_cli_doctor_ready(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_config(tmp_path)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}")
    (model_dir / "model.safetensors").write_text("")
    (model_dir / "tokenizer.json").write_text("{}")

    # Patch _resolve_model_path to return our fake dir
    with patch("mlx_swarm.backend._resolve_model_path", return_value=model_dir):
        result = main(["--config", str(config_path), "doctor"])
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["batch"]["maxBatchPromptTokens"] == 32768
    assert payload["worker"]["capabilities"]["delegationLevel"] == "exact-edit"
    assert payload["model"]["metadata"]["metadataReady"] is True


def test_cli_doctor_not_ready(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    with patch("mlx_swarm.backend._resolve_model_path", side_effect=RuntimeError("not found")):
        result = main(["--config", str(config_path), "doctor"])
    assert result == 1


def test_cli_run_success(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    plan_path = _write_plan(tmp_path)

    mock_session = MagicMock()
    mock_session.summary.return_value = {"status": "completed", "total": 1, "completed": 1}
    mock_session.export_results.return_value = {"tasks": {}}

    with patch("mlx_swarm.cli.execute_plan", return_value=mock_session):
        result = main(["--config", str(config_path), "run", str(plan_path)])
    assert result == 0


def test_cli_run_partial(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    plan_path = _write_plan(tmp_path)

    mock_session = MagicMock()
    mock_session.summary.return_value = {"status": "partial", "total": 1, "completed": 0}

    with patch("mlx_swarm.cli.execute_plan", return_value=mock_session):
        result = main(["--config", str(config_path), "run", str(plan_path)])
    assert result == 1


def test_cli_workspace_preview_run_artifact_and_cleanup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, config_path, plan_path = _write_workspace_v2(tmp_path)
    assert main([
        "--config",
        str(config_path),
        "workspace",
        "preview",
        str(plan_path),
    ]) == 0
    preview_payload = json.loads(capsys.readouterr().out)
    preview = preview_payload["execution"]
    assert preview["planSha256"] == preview_payload["planDigest"]
    assert preview["workspaceRoot"] == str(repo.resolve())

    assert main([
        "--config",
        str(config_path),
        "run",
        str(plan_path),
    ]) == 1
    assert "requires the displayed canonical plan digest" in (
        capsys.readouterr().err
    )

    mock_session = MagicMock()
    mock_session.summary.return_value = {
        "status": "completed",
        "total": 1,
        "completed": 1,
    }
    with patch("mlx_swarm.cli.execute_plan", return_value=mock_session):
        assert main([
            "--config",
            str(config_path),
            "run",
            str(plan_path),
            "--approve-plan-digest",
            preview_payload["planDigest"],
            "--approve-execution-digest",
            preview["executionDigest"],
        ]) == 0
    capsys.readouterr()

    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    run_dirs = [
        value
        for value in (config.artifacts_dir / plan.plan_id).iterdir()
        if (value / "session.json").is_file()
    ]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    session = Session.load(run_dir, config)
    snapshot = session.workspace_snapshot()
    assert snapshot is not None
    manifest = persist_artifact(
        run_dir,
        plan.tasks[0],
        _workspace_diff(),
        snapshot,
    )
    session.update_task(
        "change",
        status="awaiting_approval",
        artifact=manifest,
        output=_workspace_diff(),
        normalizedOutput=_workspace_diff(),
    )
    session.set_status("awaiting_approval")
    assert main([
        "--config",
        str(config_path),
        "artifact",
        "apply",
        str(run_dir),
        "change",
        "--digest",
        manifest["sha256"],
    ]) == 0
    decision = json.loads(
        (
            run_dir
            / "artifacts"
            / "change"
            / "decision.json"
        ).read_text()
    )
    assert decision["action"] == "apply"
    capsys.readouterr()

    assert main([
        "--config",
        str(config_path),
        "workspace",
        "status",
        str(run_dir),
    ]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["workspace"]["branch"] == snapshot["branch"]

    terminal = Session.load(run_dir, config)
    terminal.set_status("partial")
    assert main([
        "--config",
        str(config_path),
        "workspace",
        "cleanup",
        str(run_dir),
    ]) == 1
    assert "Resumable workspace work" in capsys.readouterr().err
    terminal.update_task(
        "change",
        status="failed",
        error="Synthetic terminal state for cleanup.",
    )
    terminal.set_status("failed")
    assert main([
        "--config",
        str(config_path),
        "workspace",
        "cleanup",
        str(run_dir),
    ]) == 0
    cleaned = json.loads(capsys.readouterr().out)
    assert cleaned["cleanedUp"] is True
    assert not Path(snapshot["worktreePath"]).exists()
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "show-ref",
            "--verify",
            f"refs/heads/{snapshot['branch']}",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )


def test_cli_main_checkout_yolo_preview_and_launch_policy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, config_path, plan_path = _write_workspace_v2(tmp_path)
    assert main([
        "--config",
        str(config_path),
        "workspace",
        "preview",
        str(plan_path),
        "--approval-mode",
        "yolo",
        "--workspace-target",
        "checkout",
    ]) == 0
    preview_payload = json.loads(capsys.readouterr().out)
    preview = preview_payload["execution"]
    assert preview["executionPolicy"]["approvalMode"] == "yolo"
    assert preview["executionPolicy"]["workspaceTarget"] == "checkout"

    mock_session = MagicMock()
    mock_session.summary.return_value = {
        "status": "completed",
        "total": 1,
        "completed": 1,
    }
    with patch("mlx_swarm.cli.execute_plan", return_value=mock_session):
        assert main([
            "--config",
            str(config_path),
            "run",
            str(plan_path),
            "--approval-mode",
            "yolo",
            "--workspace-target",
            "checkout",
            "--approve-plan-digest",
            preview_payload["planDigest"],
            "--approve-execution-digest",
            preview["executionDigest"],
        ]) == 0
    capsys.readouterr()

    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    run_dirs = [
        value
        for value in (config.artifacts_dir / plan.plan_id).iterdir()
        if (value / "session.json").is_file()
    ]
    assert len(run_dirs) == 1
    state = json.loads((run_dirs[0] / "session.json").read_text())
    snapshot = json.loads(
        (run_dirs[0] / "workspace.snapshot.json").read_text()
    )
    assert state["approvalMode"] == "yolo"
    assert state["workspaceTarget"] == "checkout"
    assert snapshot["executionPath"] == str(repo.resolve())


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

    with patch("mlx_swarm.session.Session.load") as mock_load:
        mock_session = MagicMock()
        mock_session.summary.return_value = {"status": "completed"}
        mock_session.state = {"tasks": {"t1": {"id": "t1", "status": "completed", "output": "hello"}}}
        mock_load.return_value = mock_session
        result = main(["--config", str(config_path), "inspect", str(session_dir)])
    assert result == 0


def test_cli_inspect_task_output(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)

    with patch("mlx_swarm.session.Session.load") as mock_load:
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
    with patch("mlx_swarm.ui.serve_ui") as serve:
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


def test_cli_commander_create_claim_and_import(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_config(tmp_path)
    assert main([
        "--config",
        str(config_path),
        "commander",
        "create",
        "--objective",
        "Prepare one local task",
        "--constraint",
        "Stay local.",
    ]) == 0
    created = json.loads(capsys.readouterr().out)
    request_id = created["request"]["requestId"]

    assert main([
        "--config",
        str(config_path),
        "commander",
        "claim-plan",
        request_id,
    ]) == 0
    claim = json.loads(capsys.readouterr().out)

    response = _write_plan(tmp_path)
    assert main([
        "--config",
        str(config_path),
        "commander",
        "import-plan",
        request_id,
        str(response),
        "--claim-id",
        claim["claimId"],
    ]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["request"]["status"] == "plan_ready"
    assert imported["planningReceipt"]["usage"]["usageStatus"] == "unavailable"


def test_cli_commander_imports_exact_codex_jsonl_usage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_config(tmp_path)
    assert main([
        "--config",
        str(config_path),
        "commander",
        "create",
        "--objective",
        "Prepare one local task",
    ]) == 0
    request_id = json.loads(capsys.readouterr().out)["request"]["requestId"]
    assert main([
        "--config",
        str(config_path),
        "commander",
        "claim-plan",
        request_id,
    ]) == 0
    claim_id = json.loads(capsys.readouterr().out)["claimId"]
    usage_path = tmp_path / "codex.jsonl"
    usage_path.write_text(
        json.dumps({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 125,
                "cached_input_tokens": 75,
                "output_tokens": 25,
                "total_tokens": 150,
            },
        }) + "\n",
        encoding="utf-8",
    )

    assert main([
        "--config",
        str(config_path),
        "commander",
        "import-plan",
        request_id,
        str(_write_plan(tmp_path)),
        "--claim-id",
        claim_id,
        "--usage-jsonl",
        str(usage_path),
    ]) == 0

    receipt = json.loads(capsys.readouterr().out)["planningReceipt"]
    assert receipt["usage"] == {
        "usageStatus": "reported",
        "promptTokens": 125,
        "completionTokens": 25,
        "totalTokens": 150,
    }


def test_cli_rejects_malformed_codex_usage_stream(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_config(tmp_path)
    usage_path = tmp_path / "codex.jsonl"
    usage_path.write_text(
        "not-json\n"
        + json.dumps({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        })
        + "\n",
        encoding="utf-8",
    )
    assert main([
        "--config",
        str(config_path),
        "commander",
        "create",
        "--objective",
        "Prepare one local task",
    ]) == 0
    request_id = json.loads(capsys.readouterr().out)["request"]["requestId"]
    assert main([
        "--config",
        str(config_path),
        "commander",
        "claim-plan",
        request_id,
    ]) == 0
    claim_id = json.loads(capsys.readouterr().out)["claimId"]

    assert main([
        "--config",
        str(config_path),
        "commander",
        "import-plan",
        request_id,
        str(_write_plan(tmp_path)),
        "--claim-id",
        claim_id,
        "--usage-jsonl",
        str(usage_path),
    ]) == 1

    assert "malformed lines" in capsys.readouterr().err


def test_cli_skill_install_does_not_require_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    skills_dir = tmp_path / "skills"
    assert main([
        "skill",
        "install",
        "--skills-dir",
        str(skills_dir),
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["installed"] is True
    assert Path(payload["path"]).is_dir()


def test_cli_evaluation_prepare_status_and_run_use_pinned_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_config(tmp_path)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}", encoding="utf-8")
    profile = MagicMock()
    profile_payload_value = {"schemaVersion": 1, "profileId": "study"}
    detail = {
        "environment": {"profileSha256": "digest"},
        "evaluation": {"evaluationId": "study-1"},
    }
    store = MagicMock()
    store.prepare.return_value = detail
    store.detail.return_value = detail
    runner = MagicMock()
    runner.run_phase.return_value = detail

    with (
        patch("mlx_swarm.cli.EvaluationStore", return_value=store),
        patch(
            "mlx_swarm.cli.load_evaluation_profile",
            return_value=profile,
        ),
        patch(
            "mlx_swarm.cli.profile_payload",
            return_value=profile_payload_value,
        ),
        patch(
            "mlx_swarm.cli.canonical_json_sha256",
            return_value="digest",
        ),
        patch("mlx_swarm.cli.EvaluationRunner", return_value=runner),
    ):
        assert main([
            "--config",
            str(config_path),
            "eval",
            "prepare",
            str(profile_path),
        ]) == 0
        capsys.readouterr()
        assert main([
            "--config",
            str(config_path),
            "eval",
            "status",
            "study-1",
        ]) == 0
        capsys.readouterr()
        assert main([
            "--config",
            str(config_path),
            "eval",
            "run",
            "study-1",
            "--phase",
            "pilot",
            "--profile",
            str(profile_path),
        ]) == 0

    store.prepare.assert_called_once_with(profile)
    runner.run_phase.assert_called_once_with("study-1", "pilot")


def test_cli_evaluation_prepare_can_resume_unsealed_study(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_config(tmp_path)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}", encoding="utf-8")
    profile = MagicMock()
    store = MagicMock()
    store.prepare.return_value = {
        "evaluation": {"evaluationId": "interrupted-study"},
    }

    with (
        patch("mlx_swarm.cli.EvaluationStore", return_value=store),
        patch(
            "mlx_swarm.cli.load_evaluation_profile",
            return_value=profile,
        ),
    ):
        assert main([
            "--config",
            str(config_path),
            "eval",
            "prepare",
            str(profile_path),
            "--resume",
            "interrupted-study",
        ]) == 0

    capsys.readouterr()
    store.prepare.assert_called_once_with(
        profile,
        resume_evaluation_id="interrupted-study",
    )


def test_cli_evaluation_local_replay_selects_local_worker_strategy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_config(tmp_path)
    store = MagicMock()
    replay = {
        "evaluationId": "study-1",
        "frontierCalls": 0,
        "promotionGate": {"status": "failed"},
    }
    with (
        patch("mlx_swarm.cli.EvaluationStore", return_value=store),
        patch(
            "mlx_swarm.cli.run_local_replay_calibration",
            return_value=replay,
        ) as run_replay,
    ):
        assert main([
            "--config",
            str(config_path),
            "eval",
            "replay-local",
            "study-1",
            "--worker-mode",
            "reasoning-edit",
            "--reasoning-max-tokens",
            "768",
            "--adapted-plan-dir",
            str(tmp_path / "adapted"),
        ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["frontierCalls"] == 0
    run_replay.assert_called_once()
    assert run_replay.call_args.kwargs == {
        "worker_mode": "reasoning-edit",
        "reasoning_max_tokens": 768,
        "adapted_plan_dir": tmp_path / "adapted",
    }
