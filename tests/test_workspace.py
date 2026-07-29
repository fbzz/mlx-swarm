"""Workspace execution boundary tests using real temporary Git repositories."""
# @lat: [[Tests#Workspace execution]]

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from mlx_swarm import executor as executor_module
from mlx_swarm import workspace as workspace_module
from mlx_swarm.contracts import (
    ContractError,
    MUTATING_ARTIFACT_TYPES,
    TaskDef,
    load_config,
    load_plan,
)
from mlx_swarm.executor import execute_plan
from mlx_swarm.session import Session
from mlx_swarm.workspace import (
    WorkspaceError,
    apply_artifact,
    cleanup_worktree,
    execution_preview,
    load_artifact,
    load_completed_artifact_evidence,
    materialize_edit_manifest,
    persist_artifact,
    prepare_workspace,
    prepare_worktree,
    recover_artifact_application,
    run_verifications,
    submit_artifact_decision,
    workspace_readiness,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / ".gitignore").write_text(".swarm/\nconfig/.swarm/\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    config_dir = repo / "config"
    config_dir.mkdir()
    config_path = config_dir / "swarm.json"
    config_path.write_text(
        json.dumps({
            "schemaVersion": 2,
            "model": {"repository": "local/test", "localPath": ""},
            "batch": {"maxWorkers": 4},
            "artifacts": ".swarm/runs",
            "workspace": {
                "writeRoots": ["src", "tests"],
                "verificationProfiles": {
                    "syntax": {
                        "argv": [
                            "python",
                            "-c",
                            "from pathlib import Path; assert 'VALUE = 2' in Path('src/value.py').read_text()",
                        ],
                        "cwd": ".",
                        "timeoutSeconds": 10,
                        "inheritEnv": ["PATH"],
                        "environment": {},
                    }
                },
            },
        }),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo, config_path


def _plan_file(repo: Path) -> Path:
    path = repo / "config" / "plan.json"
    path.write_text(
        json.dumps({
            "schemaVersion": 2,
            "planId": "workspace-plan",
            "objective": "Change a value safely",
            "tasks": [{
                "id": "change",
                "role": "implementation",
                "prompt": "Return the approved diff.",
                "artifactType": "patch",
                "allowedPaths": ["src/value.py"],
                "verification": ["syntax"],
                "maxRepairAttempts": 0,
            }],
        }),
        encoding="utf-8",
    )
    return path


def _diff() -> str:
    return (
        "diff --git a/src/value.py b/src/value.py\n"
        "index 6f1a1d0..7c4f6d2 100644\n"
        "--- a/src/value.py\n"
        "+++ b/src/value.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )


def _approval(preview: dict[str, Any]) -> dict[str, Any]:
    return {
        "planSha256": preview["planSha256"],
        "executionDigest": preview["executionDigest"],
        "workspaceRoot": preview["workspaceRoot"],
        "baseSha": preview["baseSha"],
        "approvalMode": preview["executionPolicy"]["approvalMode"],
        "workspaceTarget": preview["executionPolicy"]["workspaceTarget"],
        "executionPolicySha256": preview["executionPolicySha256"],
    }


def test_schema_v2_workspace_contract_and_plan(tmp_path: Path) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)

    assert config.schema_version == 2
    assert config.workspace is not None
    assert config.workspace.write_roots == ("src", "tests")
    assert plan.workspace_execution is True
    assert plan.tasks[0].artifact_type in MUTATING_ARTIFACT_TYPES
    assert plan.tasks[0].verification == ("syntax",)


def _edit_manifest_plan_file(repo: Path) -> Path:
    raw = json.loads(_plan_file(repo).read_text(encoding="utf-8"))
    task = raw["tasks"][0]
    task["workerOutputProtocol"] = "edit-manifest-v1"
    task["outputProtocol"] = "Return only the strict edit manifest JSON."
    task["gate"] = {
        "requiredPatterns": [],
        "forbiddenPatterns": [],
        "maxCharacters": 4000,
        "format": "json",
        "stripSingleCodeFence": False,
        "pythonSyntax": False,
        "jsonRequiredKeys": ["edits"],
        "jsonAllowedKeys": ["edits"],
        "jsonFieldEnums": {},
    }
    path = repo / "config" / "edit-plan.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_edit_manifest_plan_contract_requires_exact_json_gate(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    plan_path = _edit_manifest_plan_file(repo)
    plan = load_plan(plan_path, config)
    assert plan.tasks[0].worker_output_protocol == "edit-manifest-v1"

    raw = json.loads(plan_path.read_text(encoding="utf-8"))
    raw["tasks"][0]["gate"]["jsonAllowedKeys"] = ["edits", "extra"]
    plan_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ContractError, match="contain exactly edits"):
        load_plan(plan_path, config)


def test_edit_manifest_can_create_one_bounded_new_text_file(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    raw = json.loads(_edit_manifest_plan_file(repo).read_text())
    raw["tasks"][0]["allowedPaths"] = ["src/new_value.py"]
    raw["tasks"][0]["verification"] = []
    plan_path = repo / "config" / "new-file-plan.json"
    plan_path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    preview = execution_preview(config, plan)
    snapshot = prepare_worktree(
        config,
        plan,
        session_id="new-file",
        expected_execution_digest=preview["executionDigest"],
    )

    diff = materialize_edit_manifest(
        json.dumps({"edits": [{
            "path": "src/new_value.py",
            "old": "",
            "new": "VALUE = 2\n",
        }]}),
        task=plan.tasks[0],
        workspace=snapshot,
    )

    assert "diff --git a/src/new_value.py b/src/new_value.py" in diff
    assert "+VALUE = 2" in diff
    assert not Path(
        snapshot["worktreePath"],
        "src/new_value.py",
    ).exists()
    session_dir = config.artifacts_dir / plan.plan_id / "new-file"
    manifest = persist_artifact(
        session_dir,
        plan.tasks[0],
        diff,
        snapshot,
    )
    assert manifest["affectedPaths"] == ["src/new_value.py"]


def test_edit_manifest_materializes_and_executes_as_reviewable_diff(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    plan = load_plan(_edit_manifest_plan_file(repo), config)
    preview = execution_preview(config, plan)
    session_id = "edit-manifest"
    snapshot = prepare_worktree(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
    )
    raw_output = json.dumps({
        "edits": [{
            "path": "src/value.py",
            "old": "VALUE = 1",
            "new": "VALUE = 2",
        }]
    })
    diff = materialize_edit_manifest(
        raw_output,
        task=plan.tasks[0],
        workspace=snapshot,
    )
    assert diff.startswith("diff --git a/src/value.py b/src/value.py\n")
    assert "-VALUE = 1\n+VALUE = 2\n" in diff
    assert Path(snapshot["worktreePath"], "src/value.py").read_text() == (
        "VALUE = 1\n"
    )

    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(config_source=config.source, plan_source=plan.source)
    session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )
    backend = _SequenceBackend([raw_output])
    outcome: list[Session] = []
    thread = threading.Thread(
        target=lambda: outcome.append(execute_plan(
            config,
            plan,
            session_dir=session_dir,
            backend=backend,
            approval_poll_seconds=0.01,
        )),
        daemon=True,
    )
    thread.start()
    manifest_path = session_dir / "artifacts" / "change" / "manifest.json"
    deadline = time.time() + 5
    while time.time() < deadline and not manifest_path.is_file():
        time.sleep(0.01)
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _manifest, stored_diff = load_artifact(session_dir, "change")
    assert stored_diff == diff
    submit_artifact_decision(
        session_dir,
        "change",
        action="apply",
        artifact_sha256=manifest["sha256"],
        source="test",
    )
    thread.join(timeout=10)
    assert not thread.is_alive()
    task_state = outcome[0].state["tasks"]["change"]
    assert task_state["output"] == raw_output
    assert task_state["normalizedOutput"] == diff
    assert (
        "edit-manifest-v1-to-unified-diff"
        in task_state["gateResult"]["normalizations"]
    )
    assert outcome[0].state["status"] == "completed"


def test_v2_plan_rejects_unknown_profile_and_path(tmp_path: Path) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    raw = json.loads(_plan_file(repo).read_text())
    raw["tasks"][0]["verification"] = ["unknown"]
    invalid = repo / "config" / "invalid.json"
    invalid.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ContractError, match="unknown profiles"):
        load_plan(invalid, config)

    raw["tasks"][0]["verification"] = []
    raw["tasks"][0]["allowedPaths"] = ["docs"]
    invalid.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ContractError, match="outside configured"):
        load_plan(invalid, config)


def test_workspace_config_is_strict_and_v1_stays_generation_only(
    tmp_path: Path,
) -> None:
    _repo_path, config_path = _repo(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["workspace"]["verificationProfiles"]["syntax"]["command"] = "pytest"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ContractError, match="unknown fields: command"):
        load_config(config_path)

    raw["workspace"]["verificationProfiles"]["syntax"].pop("command")
    raw["workspace"]["verificationProfiles"]["syntax"]["cwd"] = "../outside"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ContractError, match="workspace root"):
        load_config(config_path)

    raw["workspace"]["verificationProfiles"]["syntax"]["cwd"] = "."
    raw["workspace"]["verificationProfiles"]["syntax"]["inheritEnv"] = [
        "NOT-AN-ENV",
    ]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ContractError, match="environment variable"):
        load_config(config_path)

    raw["schemaVersion"] = 1
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ContractError, match="unknown fields: workspace"):
        load_config(config_path)


def test_v2_plan_limits_each_level_to_one_mutating_artifact(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    raw = json.loads(_plan_file(repo).read_text(encoding="utf-8"))
    raw["tasks"].append({
        "id": "change-tests",
        "role": "test",
        "prompt": "Return a test diff.",
        "artifactType": "test-suite",
        "allowedPaths": ["tests"],
        "verification": [],
    })
    path = repo / "config" / "parallel-mutating.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ContractError, match="at most one mutating"):
        load_plan(path, config)


def test_worktree_uses_head_and_excludes_dirty_source(tmp_path: Path) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    (repo / "src" / "value.py").write_text("VALUE = 99\n", encoding="utf-8")

    preview = execution_preview(config, plan)
    assert preview["workspaceRoot"] == str(repo.resolve())
    assert preview["dirty"] is True
    snapshot = prepare_worktree(
        config,
        plan,
        session_id="session-one",
        expected_execution_digest=preview["executionDigest"],
    )

    worktree = Path(snapshot["worktreePath"])
    assert (worktree / "src" / "value.py").read_text() == "VALUE = 1\n"
    assert snapshot["branch"] == "mlx-swarm/workspace-plan/session-one"
    source_status = _git(repo, "status", "--short")
    assert "src/value.py" in source_status


def test_execution_digest_binds_yolo_and_selected_target(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    plan_path = _plan_file(repo)
    _git(repo, "add", "config/plan.json")
    _git(repo, "commit", "-qm", "add plan")
    config = load_config(config_path)
    plan = load_plan(plan_path, config)

    supervised = execution_preview(config, plan)
    yolo_worktree = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="worktree",
    )
    yolo_checkout = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="checkout",
    )

    assert len({
        supervised["executionDigest"],
        yolo_worktree["executionDigest"],
        yolo_checkout["executionDigest"],
    }) == 3
    assert yolo_checkout["executionPolicy"] == {
        "schemaVersion": 2,
        "approvalMode": "yolo",
        "workspaceTarget": "checkout",
        "onVerificationFailure": "pause",
    }
    assert yolo_worktree["executionPolicy"][
        "onVerificationFailure"
    ] == "repair-once"
    with pytest.raises(WorkspaceError, match="only with explicit YOLO"):
        execution_preview(
            config,
            plan,
            workspace_target="checkout",
        )


def test_main_checkout_yolo_requires_clean_tree_and_commits_in_place(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    plan_path = _plan_file(repo)
    _git(repo, "add", "config/plan.json")
    _git(repo, "commit", "-qm", "add plan")
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="checkout",
    )
    snapshot = prepare_workspace(
        config,
        plan,
        session_id="checkout-yolo",
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="checkout",
    )
    session_dir = config.artifacts_dir / plan.plan_id / "checkout-yolo"
    manifest = persist_artifact(
        session_dir,
        plan.tasks[0],
        _diff(),
        snapshot,
    )
    receipt = apply_artifact(
        session_dir,
        plan.tasks[0],
        snapshot,
        expected_artifact_sha256=manifest["sha256"],
    )

    assert snapshot["executionPath"] == str(repo.resolve())
    assert snapshot["worktreePath"] == str(repo.resolve())
    assert snapshot["branch"] == _git(repo, "branch", "--show-current")
    assert receipt["commitSha"] == _git(repo, "rev-parse", "HEAD")
    assert (repo / "src" / "value.py").read_text() == "VALUE = 2\n"
    assert manifest["baseCommit"] == preview["baseSha"]
    with pytest.raises(WorkspaceError, match="cannot be removed"):
        cleanup_worktree(snapshot)

    (repo / "src" / "value.py").write_text("VALUE = 3\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="completely clean"):
        execution_preview(
            config,
            plan,
            approval_mode="yolo",
            workspace_target="checkout",
        )


def test_checkout_verification_never_restores_detected_changes(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    plan_path = _plan_file(repo)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["workspace"]["verificationProfiles"]["syntax"]["argv"] = [
        "python",
        "-c",
        "from pathlib import Path; Path('src/value.py').write_text('VALUE = 99\\n')",
    ]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    _git(repo, "add", "config/swarm.json", "config/plan.json")
    _git(repo, "commit", "-qm", "add mutating verification")
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="checkout",
    )
    snapshot = prepare_workspace(
        config,
        plan,
        session_id="checkout-verification",
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="checkout",
    )
    session_dir = (
        config.artifacts_dir / plan.plan_id / "checkout-verification"
    )
    manifest = persist_artifact(
        session_dir,
        plan.tasks[0],
        _diff(),
        snapshot,
    )
    apply_artifact(
        session_dir,
        plan.tasks[0],
        snapshot,
        expected_artifact_sha256=manifest["sha256"],
    )
    results = run_verifications(
        session_dir,
        plan.tasks[0],
        snapshot,
    )

    assert results[0]["passed"] is False
    assert results[0]["trackedChangesRejected"] == ["src/value.py"]
    assert results[0]["trackedChangesRestored"] == []
    assert (repo / "src" / "value.py").read_text() == "VALUE = 99\n"


def test_main_checkout_runner_lock_is_repository_wide(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    plan_path = _plan_file(repo)
    _git(repo, "add", "config/plan.json")
    _git(repo, "commit", "-qm", "add plan")
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="checkout",
    )
    session_id = "checkout-lock-one"
    snapshot = prepare_workspace(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="checkout",
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(
        config_source=config.source,
        plan_source=plan.source,
    )
    session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )

    with executor_module._workspace_execution_lock(
        config,
        session_dir,
    ):
        with pytest.raises(RuntimeError, match="already owns"):
            with executor_module._workspace_execution_lock(
                config,
                session_dir,
            ):
                pass


def test_checkout_branch_switch_at_same_head_is_rejected(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    plan_path = _plan_file(repo)
    _git(repo, "add", "config/plan.json")
    _git(repo, "commit", "-qm", "add plan")
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="checkout",
    )
    snapshot = prepare_workspace(
        config,
        plan,
        session_id="checkout-branch-switch",
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="checkout",
    )
    _git(repo, "switch", "-qc", "same-head-other-branch")

    with pytest.raises(WorkspaceError, match="branch differs"):
        persist_artifact(
            config.artifacts_dir / plan.plan_id / "checkout-branch-switch",
            plan.tasks[0],
            _diff(),
            snapshot,
        )


def test_detached_head_allows_worktree_but_not_checkout(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    plan_path = _plan_file(repo)
    _git(repo, "add", "config/plan.json")
    _git(repo, "commit", "-qm", "add plan")
    _git(repo, "checkout", "--detach", "-q")
    config = load_config(config_path)
    plan = load_plan(plan_path, config)

    preview = execution_preview(config, plan)
    assert preview["startingBranch"] is None
    snapshot = prepare_workspace(
        config,
        plan,
        session_id="detached-worktree",
        expected_execution_digest=preview["executionDigest"],
    )
    assert Path(snapshot["worktreePath"]).is_dir()
    with pytest.raises(WorkspaceError, match="checked-out Git branch"):
        execution_preview(
            config,
            plan,
            approval_mode="yolo",
            workspace_target="checkout",
        )


def test_unignored_artifacts_root_is_not_workspace_ready(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    plan_path = _plan_file(repo)
    (repo / ".gitignore").write_text(
        "config/.swarm/runs/_worktrees/\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".gitignore", "config/plan.json")
    _git(repo, "commit", "-qm", "ignore only worktrees")
    config = load_config(config_path)
    plan = load_plan(plan_path, config)

    with pytest.raises(WorkspaceError, match="not ignored"):
        execution_preview(config, plan)


def test_two_artifact_roots_share_one_checkout_lock(
    tmp_path: Path,
) -> None:
    repo, first_path = _repo(tmp_path)
    plan_path = _plan_file(repo)
    second_path = repo / "config" / "second-swarm.json"
    second_raw = json.loads(first_path.read_text(encoding="utf-8"))
    second_raw["artifacts"] = ".swarm/other-runs"
    second_path.write_text(json.dumps(second_raw), encoding="utf-8")
    _git(repo, "add", "config/plan.json", "config/second-swarm.json")
    _git(repo, "commit", "-qm", "add second config")
    first = load_config(first_path)
    second = load_config(second_path)
    plan_first = load_plan(plan_path, first)
    plan_second = load_plan(plan_path, second)
    preview = execution_preview(
        first,
        plan_first,
        approval_mode="yolo",
        workspace_target="checkout",
    )
    snapshot = prepare_workspace(
        first,
        plan_first,
        session_id="first-root",
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="checkout",
    )
    first_session = Session(
        first.artifacts_dir / plan_first.plan_id / "first-root",
        plan_first,
        session_id="first-root",
    )
    first_session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )

    with pytest.raises(WorkspaceError, match="reserved by unresolved"):
        execution_preview(
            second,
            plan_second,
            approval_mode="yolo",
            workspace_target="checkout",
        )


def test_patch_apply_and_allowlisted_verification(tmp_path: Path) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(config, plan)
    snapshot = prepare_worktree(
        config,
        plan,
        session_id="session-two",
        expected_execution_digest=preview["executionDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / "session-two"
    session_dir.mkdir(parents=True)
    manifest = persist_artifact(
        session_dir,
        plan.tasks[0],
        _diff(),
        snapshot,
    )
    stored, payload = load_artifact(session_dir, "change")
    assert stored["sha256"] == manifest["sha256"]
    assert stored["affectedPaths"] == ["src/value.py"]
    assert payload == _diff()

    receipt = apply_artifact(
        session_dir,
        plan.tasks[0],
        snapshot,
        expected_artifact_sha256=manifest["sha256"],
    )
    results = run_verifications(
        session_dir,
        plan.tasks[0],
        snapshot,
    )
    assert receipt["commitSha"] == snapshot["headSha"]
    assert results[0]["passed"] is True
    assert results[0]["schemaVersion"] == 2
    verification_log = session_dir / results[0]["output"]
    assert results[0]["outputBytes"] == verification_log.stat().st_size
    assert results[0]["outputSha256"] == hashlib.sha256(
        verification_log.read_bytes()
    ).hexdigest()
    evidence = load_completed_artifact_evidence(
        session_dir,
        plan.tasks[0],
        snapshot,
    )
    assert evidence["verificationReceipts"] == results
    verification_log.write_bytes(b"tampered\n")
    with pytest.raises(
        WorkspaceError,
        match="verification log binding is invalid",
    ):
        load_completed_artifact_evidence(
            session_dir,
            plan.tasks[0],
            snapshot,
        )
    assert (Path(snapshot["worktreePath"]) / "src" / "value.py").read_text() == "VALUE = 2\n"
    assert (repo / "src" / "value.py").read_text() == "VALUE = 1\n"


def test_forged_passing_verification_receipt_is_rejected(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["workspace"]["verificationProfiles"]["syntax"]["argv"] = [
        "python",
        "-c",
        "raise SystemExit(7)",
    ]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(config, plan)
    snapshot = prepare_worktree(
        config,
        plan,
        session_id="forged-verification",
        expected_execution_digest=preview["executionDigest"],
    )
    session_dir = (
        config.artifacts_dir / plan.plan_id / "forged-verification"
    )
    session_dir.mkdir(parents=True)
    manifest = persist_artifact(
        session_dir,
        plan.tasks[0],
        _diff(),
        snapshot,
    )
    apply_artifact(
        session_dir,
        plan.tasks[0],
        snapshot,
        expected_artifact_sha256=manifest["sha256"],
    )
    results = run_verifications(
        session_dir,
        plan.tasks[0],
        snapshot,
    )
    assert results[0]["passed"] is False
    receipt_path = (
        session_dir
        / "artifacts"
        / "change"
        / "verification"
        / "syntax"
        / "attempt-001.json"
    )
    forged = json.loads(receipt_path.read_text(encoding="utf-8"))
    forged["passed"] = True
    receipt_path.write_text(json.dumps(forged), encoding="utf-8")

    with pytest.raises(
        WorkspaceError,
        match="verification result is inconsistent",
    ):
        load_completed_artifact_evidence(
            session_dir,
            plan.tasks[0],
            snapshot,
        )


def test_git_recount_accepts_correct_edit_with_bad_worker_hunk_metadata(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(config, plan)
    snapshot = prepare_worktree(
        config,
        plan,
        session_id="recount-hunk",
        expected_execution_digest=preview["executionDigest"],
    )
    malformed_metadata = (
        "diff --git a/src/value.py b/src/value.py\n"
        "--- a/src/value.py\n"
        "+++ b/src/value.py\n"
        "@@ -99,7 +99,7 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )
    worktree = Path(snapshot["worktreePath"])
    without_recount = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(worktree),
            "apply",
            "--check",
            "-",
        ],
        input=malformed_metadata,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert without_recount.returncode != 0

    session_dir = config.artifacts_dir / plan.plan_id / "recount-hunk"
    session_dir.mkdir(parents=True)
    manifest = persist_artifact(
        session_dir,
        plan.tasks[0],
        malformed_metadata,
        snapshot,
    )
    apply_artifact(
        session_dir,
        plan.tasks[0],
        snapshot,
        expected_artifact_sha256=manifest["sha256"],
    )
    assert (worktree / "src" / "value.py").read_text() == "VALUE = 2\n"


def test_artifact_payload_tamper_is_rejected_before_apply(
    tmp_path: Path,
) -> None:
    _repo_path, _config, plan, snapshot, session = _prepared_session(
        tmp_path,
        session_id="payload-tamper",
    )
    manifest = persist_artifact(
        session.dir,
        plan.tasks[0],
        _diff(),
        snapshot,
    )
    payload_path = session.dir / "artifacts" / "change" / "payload.diff"
    payload_path.write_text(
        _diff().replace("VALUE = 2", "VALUE = 3"),
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="digest is invalid"):
        apply_artifact(
            session.dir,
            plan.tasks[0],
            snapshot,
            expected_artifact_sha256=manifest["sha256"],
        )


def test_replaced_manifest_cannot_escape_approved_artifact_digest(
    tmp_path: Path,
) -> None:
    _repo_path, _config, plan, snapshot, session = _prepared_session(
        tmp_path,
        session_id="manifest-replacement",
    )
    original = persist_artifact(
        session.dir,
        plan.tasks[0],
        _diff(),
        snapshot,
    )
    replacement = _diff().replace("VALUE = 2", "VALUE = 3")
    artifact_dir = session.dir / "artifacts" / "change"
    (artifact_dir / "payload.diff").write_text(
        replacement,
        encoding="utf-8",
    )
    manifest_path = artifact_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = workspace_module.canonical_sha256({
        "taskId": "change",
        "artifactType": "patch",
        "baseCommit": manifest["baseCommit"],
        "payload": replacement,
    })
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(WorkspaceError, match="approved digest"):
        apply_artifact(
            session.dir,
            plan.tasks[0],
            snapshot,
            expected_artifact_sha256=original["sha256"],
        )


def test_headerless_extra_patch_section_is_rejected(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    (repo / "tests" / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
    _git(repo, "add", "tests/other.py")
    _git(repo, "commit", "-qm", "add second file")
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(config, plan)
    snapshot = prepare_worktree(
        config,
        plan,
        session_id="headerless-extra",
        expected_execution_digest=preview["executionDigest"],
    )
    payload = (
        _diff()
        + "--- a/tests/other.py\n"
        + "+++ b/tests/other.py\n"
        + "@@ -1 +1 @@\n"
        + "-OTHER = 1\n"
        + "+OTHER = 2\n"
    )

    with pytest.raises(WorkspaceError, match="not represented|git apply"):
        persist_artifact(
            config.artifacts_dir / plan.plan_id / "headerless-extra",
            plan.tasks[0],
            payload,
            snapshot,
        )


def test_runtime_artifact_root_is_never_an_allowed_patch_target(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    raw_config["workspace"]["writeRoots"] = ["."]
    config_path.write_text(json.dumps(raw_config), encoding="utf-8")
    raw_plan = json.loads(_plan_file(repo).read_text(encoding="utf-8"))
    raw_plan["tasks"][0]["allowedPaths"] = ["."]
    plan_path = repo / "config" / "broad-plan.json"
    plan_path.write_text(json.dumps(raw_plan), encoding="utf-8")
    _git(
        repo,
        "add",
        "config/swarm.json",
        "config/plan.json",
        "config/broad-plan.json",
    )
    _git(repo, "commit", "-qm", "broad path authority")
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="checkout",
    )
    snapshot = prepare_workspace(
        config,
        plan,
        session_id="runtime-root-target",
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="checkout",
    )
    payload = (
        "diff --git a/config/.swarm/runs/evidence.txt "
        "b/config/.swarm/runs/evidence.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/config/.swarm/runs/evidence.txt\n"
        "@@ -0,0 +1 @@\n"
        "+tamper\n"
    )

    with pytest.raises(WorkspaceError, match="runtime data"):
        persist_artifact(
            config.artifacts_dir / plan.plan_id / "runtime-root-target",
            plan.tasks[0],
            payload,
            snapshot,
        )


def test_verification_uses_exact_argv_no_shell_and_sanitized_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo_path, config, plan, snapshot, session = _prepared_session(
        tmp_path,
        session_id="popen-boundary",
    )
    manifest = persist_artifact(
        session.dir,
        plan.tasks[0],
        _diff(),
        snapshot,
    )
    apply_artifact(
        session.dir,
        plan.tasks[0],
        snapshot,
        expected_artifact_sha256=manifest["sha256"],
    )
    original_popen = subprocess.Popen
    calls: list[tuple[Any, dict[str, Any]]] = []

    def recording_popen(argv: Any, **kwargs: Any):
        calls.append((argv, kwargs))
        return original_popen(argv, **kwargs)

    monkeypatch.setenv("MLX_SWARM_SECRET_TEST", "must-not-leak")
    monkeypatch.setattr(
        "mlx_swarm.workspace.subprocess.Popen",
        recording_popen,
    )
    results = run_verifications(
        session.dir,
        plan.tasks[0],
        snapshot,
    )

    assert results[0]["passed"] is True
    expected_argv = list(
        snapshot["verificationProfiles"]["syntax"]["argv"]
    )
    argv, kwargs = next(
        (argv, kwargs)
        for argv, kwargs in calls
        if argv == expected_argv
    )
    assert argv == expected_argv
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == Path(snapshot["worktreePath"])
    assert "MLX_SWARM_SECRET_TEST" not in kwargs["env"]
    assert kwargs["env"]["HOME"].startswith(str(session.dir))


def test_verification_output_is_bounded(tmp_path: Path) -> None:
    repo, config_path = _repo(tmp_path)
    _set_profile_argv(
        config_path,
        ["python", "-c", "import sys; sys.stdout.write('x' * 1100000)"],
    )
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(config, plan)
    snapshot = prepare_worktree(
        config,
        plan,
        session_id="bounded-output",
        expected_execution_digest=preview["executionDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / "bounded-output"
    session_dir.mkdir(parents=True)
    manifest = persist_artifact(
        session_dir,
        plan.tasks[0],
        _diff(),
        snapshot,
    )
    apply_artifact(
        session_dir,
        plan.tasks[0],
        snapshot,
        expected_artifact_sha256=manifest["sha256"],
    )
    result = run_verifications(
        session_dir,
        plan.tasks[0],
        snapshot,
    )[0]

    assert result["passed"] is True
    assert result["outputTruncated"] is True
    assert (session_dir / result["output"]).stat().st_size == 1_000_000


def test_verification_timeout_terminates_process_group(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    profile = raw["workspace"]["verificationProfiles"]["syntax"]
    profile["argv"] = ["python", "-c", "import time; time.sleep(30)"]
    profile["timeoutSeconds"] = 1
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(config, plan)
    snapshot = prepare_worktree(
        config,
        plan,
        session_id="verification-timeout",
        expected_execution_digest=preview["executionDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / "verification-timeout"
    session_dir.mkdir(parents=True)
    manifest = persist_artifact(
        session_dir,
        plan.tasks[0],
        _diff(),
        snapshot,
    )
    apply_artifact(
        session_dir,
        plan.tasks[0],
        snapshot,
        expected_artifact_sha256=manifest["sha256"],
    )
    started = time.monotonic()
    result = run_verifications(
        session_dir,
        plan.tasks[0],
        snapshot,
    )[0]

    assert result["passed"] is False
    assert result["timedOut"] is True
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    "payload,match",
    [
        (
            "diff --git a/../secret b/../secret\n--- a/../secret\n+++ b/../secret\n",
            "escapes",
        ),
        (
            "diff --git a/src/blob b/src/blob\nGIT binary patch\n",
            "forbidden",
        ),
        (
            "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n",
            "allowedPaths",
        ),
    ],
)
def test_patch_boundary_rejections(
    tmp_path: Path,
    payload: str,
    match: str,
) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(config, plan)
    snapshot = prepare_worktree(
        config,
        plan,
        session_id=f"bad-{match.lower()}",
        expected_execution_digest=preview["executionDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / f"bad-{match.lower()}"
    session_dir.mkdir(parents=True)
    with pytest.raises(WorkspaceError, match=match):
        persist_artifact(session_dir, plan.tasks[0], payload, snapshot)


class _Backend:
    def __init__(self):
        self.closed = False
        self.calls = 0

    def generate(
        self,
        tasks: list[TaskDef],
        prompts: list[str],
    ) -> tuple[list[str], dict[str, Any]]:
        self.calls += 1
        return [_diff()], {"batchSize": 1}

    def close(self) -> None:
        self.closed = True


class _SequenceBackend(_Backend):
    def __init__(self, outputs: list[str]):
        super().__init__()
        self.outputs = list(outputs)

    def generate(
        self,
        tasks: list[TaskDef],
        prompts: list[str],
    ) -> tuple[list[str], dict[str, Any]]:
        self.calls += 1
        return [self.outputs.pop(0)], {"batchSize": 1}


def _v3_edit_task(
    task_id: str,
    path: str,
    source_label: str,
    *,
    execution_mode: str = "local-agent",
    edits: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "role": "implementation",
        "prompt": f"Update {path} with one exact edit manifest.",
        "artifactType": "patch",
        "workerOutputProtocol": "edit-manifest-v1",
        "executionMode": execution_mode,
        "contextRefs": [source_label],
        "interfaceContract": "The module continues to expose one integer constant.",
        "expectedOutputTokens": 400,
        "allowedPaths": [path],
        "verification": [],
        "maxRepairAttempts": 1,
        "generationOverride": {
            "temperature": 0.0,
            "top_p": 1.0,
            "max_tokens": 800,
        },
        "gate": {
            "requiredPatterns": [],
            "forbiddenPatterns": [],
            "maxCharacters": 4000,
            "format": "json",
            "stripSingleCodeFence": False,
            "pythonSyntax": False,
            "jsonRequiredKeys": ["edits"],
            "jsonAllowedKeys": ["edits"],
            "jsonFieldEnums": {},
        },
    }
    if execution_mode == "deterministic-edit":
        task["expectedOutputTokens"] = 0
        task["maxRepairAttempts"] = 0
        task["deterministicEdits"] = edits or []
        task.pop("generationOverride")
    return task


def test_schema_v3_deterministic_edit_bypasses_local_model(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    plan_path = repo / "config" / "deterministic-plan.json"
    plan_path.write_text(json.dumps({
        "schemaVersion": 3,
        "planId": "deterministic-plan",
        "objective": "Apply already-known exact bytes.",
        "integrationVerification": ["syntax"],
        "context": {
            "objective": "Change VALUE to 2.",
            "authoritativeSources": [{
                "label": "value-source",
                "content": "src/value.py contains VALUE = 1.",
            }],
            "constraints": ["Do not load a local model."],
            "rejectionCriteria": ["VALUE is not 2."],
            "outputProtocol": "Return only the contracted artifact.",
        },
        "tasks": [_v3_edit_task(
            "change",
            "src/value.py",
            "value-source",
            execution_mode="deterministic-edit",
            edits=[{
                "path": "src/value.py",
                "old": "VALUE = 1",
                "new": "VALUE = 2",
            }],
        )],
    }), encoding="utf-8")
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_id = "deterministic"
    snapshot = prepare_workspace(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(config_source=config.source, plan_source=plan.source)
    session.attach_workspace(snapshot, execution_approval=_approval(preview))

    with patch(
        "mlx_swarm.executor.MLXBatchBackend",
        side_effect=AssertionError("deterministic edit must not load MLX"),
    ) as model:
        outcome = execute_plan(
            config,
            plan,
            session_dir=session_dir,
            approval_poll_seconds=0.01,
        )

    model.assert_not_called()
    assert outcome.state["status"] == "completed"
    assert outcome.local_usage()["generationCalls"] == 0
    assert outcome.state["integrationVerificationResults"][0]["passed"]
    assert Path(snapshot["worktreePath"], "src/value.py").read_text() == (
        "VALUE = 2\n"
    )


def test_schema_v3_batches_disjoint_mutations_and_verifies_final_state(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    (repo / "src" / "left.py").write_text("LEFT = 1\n", encoding="utf-8")
    (repo / "src" / "right.py").write_text("RIGHT = 1\n", encoding="utf-8")
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    raw_config["workspace"]["verificationProfiles"]["syntax"]["argv"] = [
        "python",
        "-c",
        (
            "from pathlib import Path; "
            "assert 'LEFT = 2' in Path('src/left.py').read_text(); "
            "assert 'RIGHT = 2' in Path('src/right.py').read_text()"
        ),
    ]
    config_path.write_text(json.dumps(raw_config), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "add parallel fixtures")
    plan_path = repo / "config" / "parallel-plan.json"
    plan_path.write_text(json.dumps({
        "schemaVersion": 3,
        "planId": "parallel-plan",
        "objective": "Update two disjoint modules.",
        "integrationVerification": ["syntax"],
        "context": {
            "objective": "Update independent constants.",
            "authoritativeSources": [
                {"label": "left-source", "content": "LEFT = 1"},
                {"label": "right-source", "content": "RIGHT = 1"},
            ],
            "constraints": ["Keep paths disjoint."],
            "rejectionCriteria": ["Either constant remains 1."],
            "outputProtocol": "Return only strict JSON.",
        },
        "tasks": [
            _v3_edit_task("left", "src/left.py", "left-source"),
            _v3_edit_task("right", "src/right.py", "right-source"),
        ],
    }), encoding="utf-8")
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_id = "parallel"
    snapshot = prepare_workspace(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(config_source=config.source, plan_source=plan.source)
    session.attach_workspace(snapshot, execution_approval=_approval(preview))

    class ParallelBackend(_Backend):
        def generate(self, tasks, prompts):
            self.calls += 1
            assert [task.id for task in tasks] == ["left", "right"]
            assert "RIGHT = 1" not in prompts[0]
            assert "LEFT = 1" not in prompts[1]
            return [
                json.dumps({"edits": [{
                    "path": "src/left.py",
                    "old": "LEFT = 1",
                    "new": "LEFT = 2",
                }]}),
                json.dumps({"edits": [{
                    "path": "src/right.py",
                    "old": "RIGHT = 1",
                    "new": "RIGHT = 2",
                }]}),
            ], {"batchSize": 2, "generationCalls": 1}

    backend = ParallelBackend()
    outcome = execute_plan(
        config,
        plan,
        session_dir=session_dir,
        backend=backend,
        approval_poll_seconds=0.01,
    )

    assert outcome.state["status"] == "completed"
    assert backend.calls == 1
    assert outcome.state["integrationVerificationResults"][0]["passed"]
    assert Path(snapshot["worktreePath"], "src/left.py").read_text() == (
        "LEFT = 2\n"
    )
    assert Path(snapshot["worktreePath"], "src/right.py").read_text() == (
        "RIGHT = 2\n"
    )


def test_yolo_auto_applies_digest_bound_artifact_and_verifies(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_id = "yolo-success"
    snapshot = prepare_workspace(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(config_source=config.source, plan_source=plan.source)
    session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )
    backend = _Backend()

    outcome = execute_plan(
        config,
        plan,
        session_dir=session_dir,
        backend=backend,
        approval_poll_seconds=0.01,
    )

    assert outcome.state["status"] == "completed"
    assert outcome.state["approvalMode"] == "yolo"
    assert outcome.state["workspaceTarget"] == "worktree"
    assert backend.calls == 1
    decision = json.loads(
        (
            session_dir
            / "artifacts"
            / "change"
            / "decision.json"
        ).read_text()
    )
    assert decision["source"] == "yolo"
    assert (
        decision["executionPolicySha256"]
        == preview["executionPolicySha256"]
    )
    assert Path(snapshot["worktreePath"], "src/value.py").read_text() == (
        "VALUE = 2\n"
    )


def test_corrupt_completed_evidence_seals_partial_diagnostic_packet(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_id = "finalization-diagnostic"
    yolo_snapshot = prepare_workspace(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(
        config_source=config.source,
        plan_source=plan.source,
    )
    session.attach_workspace(
        yolo_snapshot,
        execution_approval=_approval(preview),
    )
    first = execute_plan(
        config,
        plan,
        session_dir=session_dir,
        backend=_Backend(),
        approval_poll_seconds=0.01,
    )
    assert first.state["status"] == "completed"
    receipt_path = (
        session_dir / "artifacts" / "change" / "apply-receipt.json"
    )
    corrupt = json.loads(receipt_path.read_text(encoding="utf-8"))
    corrupt["forged"] = True
    receipt_path.write_text(json.dumps(corrupt), encoding="utf-8")

    with patch(
        "mlx_swarm.executor.MLXBatchBackend",
        side_effect=AssertionError("model must not load"),
    ) as model:
        resumed = execute_plan(
            config,
            plan,
            session_dir=session_dir,
            approval_poll_seconds=0.01,
        )

    model.assert_not_called()
    assert resumed.state["status"] == "partial"
    assert resumed.state["pauseReason"] == (
        "finalization_validation_failed"
    )
    packet = json.loads(
        (session_dir / "frontier-result.json").read_text(
            encoding="utf-8"
        )
    )
    assert packet["status"] == "partial"
    assert packet["requiresFrontierReview"] is False
    assert packet["pauseReason"] == "finalization_validation_failed"
    assert "receipt is invalid" in packet[
        "finalizationError"
    ].lower()
    assert packet["workspace"]["appliedArtifacts"][0][
        "evidenceStatus"
    ] == "unvalidated-diagnostic"
    assert "finalDiff" not in packet["workspace"]


def test_yolo_verification_failure_pauses_without_another_generation(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["workspace"]["verificationProfiles"]["syntax"]["argv"] = [
        "python",
        "-c",
        "raise SystemExit(7)",
    ]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_id = "yolo-failed-check"
    snapshot = prepare_workspace(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(config_source=config.source, plan_source=plan.source)
    session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )
    backend = _Backend()

    outcome = execute_plan(
        config,
        plan,
        session_dir=session_dir,
        backend=backend,
        approval_poll_seconds=0.01,
    )

    assert outcome.state["status"] == "partial"
    assert outcome.get_task_status("change") == "verification_failed"
    assert outcome.state["pauseReason"] == "verification_failed"
    assert outcome.state["reviewStatus"] == "not_eligible"
    assert backend.calls == 1


def test_yolo_worktree_reverts_repairs_once_and_preserves_failed_artifact(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    raw_config["workspace"]["verificationProfiles"]["syntax"]["argv"] = [
        "python",
        "-c",
        (
            "from pathlib import Path; "
            "assert 'VALUE = 3' in Path('src/value.py').read_text()"
        ),
    ]
    config_path.write_text(json.dumps(raw_config), encoding="utf-8")
    plan_path = _edit_manifest_plan_file(repo)
    raw_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    raw_plan["tasks"][0]["maxRepairAttempts"] = 1
    plan_path.write_text(json.dumps(raw_plan), encoding="utf-8")
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_id = "yolo-repair"
    snapshot = prepare_workspace(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(config_source=config.source, plan_source=plan.source)
    session.attach_workspace(snapshot, execution_approval=_approval(preview))
    backend = _SequenceBackend([
        json.dumps({"edits": [{
            "path": "src/value.py",
            "old": "VALUE = 1",
            "new": "VALUE = 2",
        }]}),
        json.dumps({"edits": [{
            "path": "src/value.py",
            "old": "VALUE = 1",
            "new": "VALUE = 3",
        }]}),
    ])

    outcome = execute_plan(
        config,
        plan,
        session_dir=session_dir,
        backend=backend,
        approval_poll_seconds=0.01,
    )

    task = outcome.state["tasks"]["change"]
    assert outcome.state["status"] == "completed"
    assert backend.calls == 2
    assert task["repairAttempts"] == 1
    assert task["verificationRecoveryAttempts"] == 1
    assert len(task["artifactHistory"]) == 1
    history = task["artifactHistory"][0]
    assert history["revertReceipt"]["revertCommit"]
    assert (session_dir / history["path"] / "manifest.json").is_file()
    assert Path(snapshot["worktreePath"], "src/value.py").read_text() == (
        "VALUE = 3\n"
    )


def test_existing_session_rejects_changed_same_id_plan(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    plan_path = _plan_file(repo)
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_id = "immutable-plan"
    snapshot = prepare_workspace(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )
    changed_raw = json.loads(plan_path.read_text(encoding="utf-8"))
    changed_raw["tasks"][0]["prompt"] = "A different same-ID task."
    changed_path = repo / "config" / "changed-plan.json"
    changed_path.write_text(json.dumps(changed_raw), encoding="utf-8")
    changed_plan = load_plan(changed_path, config)
    backend = _Backend()

    with pytest.raises(RuntimeError, match="immutable session snapshot"):
        execute_plan(
            config,
            changed_plan,
            session_dir=session_dir,
            backend=backend,
            approval_poll_seconds=0.01,
        )
    assert backend.calls == 0


def test_tampered_execution_snapshot_cannot_become_resume_authority(
    tmp_path: Path,
) -> None:
    _repo_path, config, plan, snapshot, session = _prepared_session(
        tmp_path,
        session_id="snapshot-tamper",
    )
    snapshot_path = session.dir / "workspace.snapshot.json"
    tampered = json.loads(snapshot_path.read_text(encoding="utf-8"))
    tampered["executionPolicy"]["onVerificationFailure"] = "continue"
    snapshot_path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(WorkspaceError, match="policy snapshot is invalid"):
        execute_plan(
            config,
            plan,
            session_dir=session.dir,
            backend=_Backend(),
            approval_poll_seconds=0.01,
        )


def test_worktree_branch_field_cannot_replace_deterministic_identity(
    tmp_path: Path,
) -> None:
    _repo_path, config, plan, _snapshot, session = _prepared_session(
        tmp_path,
        session_id="branch-identity-tamper",
    )
    snapshot_path = session.dir / "workspace.snapshot.json"
    tampered = json.loads(snapshot_path.read_text(encoding="utf-8"))
    tampered["branch"] = "mlx-swarm/workspace-plan/another-session"
    snapshot_path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(WorkspaceError, match="branch identity"):
        execute_plan(
            config,
            plan,
            session_dir=session.dir,
            backend=_Backend(),
            approval_poll_seconds=0.01,
        )


def test_session_load_recovers_durable_workspace_attachment(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(config, plan)
    session_id = "attachment-recovery"
    snapshot = prepare_worktree(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(config_source=config.source, plan_source=plan.source)
    snapshot["executionApproval"] = _approval(preview)
    snapshot["sessionDir"] = str(session_dir.resolve())
    (session_dir / "workspace.snapshot.json").write_text(
        json.dumps(snapshot),
        encoding="utf-8",
    )
    assert session.state.get("workspaceExecution") is not True

    recovered = Session.load(session_dir, config)

    assert recovered.state["workspaceExecution"] is True
    assert recovered.state["executionApproval"] == _approval(preview)
    assert recovered.workspace_snapshot()["branch"] == snapshot["branch"]


def test_yolo_verify_resume_unblocks_descendant(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    marker = tmp_path / "verification-ready"
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    raw_config["workspace"]["verificationProfiles"]["syntax"]["argv"] = [
        "python",
        "-c",
        f"from pathlib import Path; assert Path({str(marker)!r}).is_file()",
    ]
    config_path.write_text(json.dumps(raw_config), encoding="utf-8")
    raw_plan = json.loads(_plan_file(repo).read_text(encoding="utf-8"))
    raw_plan["tasks"].append({
        "id": "report",
        "role": "review",
        "prompt": "Return the completion report.",
        "artifactType": "report",
        "allowedPaths": [],
        "verification": [],
        "dependsOn": ["change"],
        "maxRepairAttempts": 0,
    })
    plan_path = repo / "config" / "descendant-plan.json"
    plan_path.write_text(json.dumps(raw_plan), encoding="utf-8")
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_id = "verify-unblocks"
    snapshot = prepare_workspace(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )
    first_backend = _SequenceBackend([_diff()])
    first = execute_plan(
        config,
        plan,
        session_dir=session_dir,
        backend=first_backend,
        approval_poll_seconds=0.01,
    )
    assert first.get_task_status("change") == "verification_failed"
    assert first.get_task_status("report") == "blocked"

    marker.write_text("ready\n", encoding="utf-8")
    manifest = first.state["tasks"]["change"]["artifact"]
    submit_artifact_decision(
        session_dir,
        "change",
        action="verify",
        artifact_sha256=manifest["sha256"],
        source="test",
    )
    second_backend = _SequenceBackend(["verified locally"])
    second = execute_plan(
        config,
        plan,
        session_dir=session_dir,
        backend=second_backend,
        approval_poll_seconds=0.01,
    )

    assert second.state["status"] == "completed"
    assert second.get_task_status("change") == "completed"
    assert second.get_task_status("report") == "completed"
    assert second_backend.calls == 1


def test_yolo_verification_resume_does_not_load_model(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    marker = tmp_path / "verification-ready"
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    raw_config["workspace"]["verificationProfiles"]["syntax"]["argv"] = [
        "python",
        "-c",
        f"from pathlib import Path; assert Path({str(marker)!r}).is_file()",
    ]
    config_path.write_text(json.dumps(raw_config), encoding="utf-8")
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_id = "verify-without-model"
    snapshot = prepare_workspace(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(config_source=config.source, plan_source=plan.source)
    session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )
    first = execute_plan(
        config,
        plan,
        session_dir=session_dir,
        backend=_Backend(),
        approval_poll_seconds=0.01,
    )
    assert first.get_task_status("change") == "verification_failed"
    marker.write_text("ready\n", encoding="utf-8")
    manifest = first.state["tasks"]["change"]["artifact"]
    submit_artifact_decision(
        session_dir,
        "change",
        action="verify",
        artifact_sha256=manifest["sha256"],
        source="test",
    )

    with patch(
        "mlx_swarm.executor.MLXBatchBackend",
        side_effect=AssertionError("model must not load"),
    ) as model:
        resumed = execute_plan(
            config,
            plan,
            session_dir=session_dir,
            approval_poll_seconds=0.01,
        )

    model.assert_not_called()
    assert resumed.state["status"] == "completed"
    assert resumed.get_task_status("change") == "completed"


def test_final_evidence_failure_is_recoverable_and_not_review_eligible(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    plan_path = repo / "config" / "report-plan.json"
    plan_path.write_text(
        json.dumps({
            "schemaVersion": 2,
            "planId": "report-plan",
            "objective": "Produce a report without mutating the worktree",
            "tasks": [{
                "id": "report",
                "role": "review",
                "prompt": "Return the report.",
                "artifactType": "report",
                "allowedPaths": [],
                "verification": [],
                "maxRepairAttempts": 0,
            }],
        }),
        encoding="utf-8",
    )
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_id = "final-evidence-recovery"
    snapshot = prepare_workspace(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="worktree",
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(config_source=config.source, plan_source=plan.source)
    session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )

    class DirtyingBackend(_SequenceBackend):
        def generate(
            self,
            tasks: list[TaskDef],
            prompts: list[str],
        ) -> tuple[list[str], dict[str, Any]]:
            Path(snapshot["worktreePath"], "rogue.txt").write_text(
                "outside ledger\n",
                encoding="utf-8",
            )
            return super().generate(tasks, prompts)

    first = execute_plan(
        config,
        plan,
        session_dir=session_dir,
        backend=DirtyingBackend(["local report"]),
        approval_poll_seconds=0.01,
    )
    packet = json.loads(
        (session_dir / "frontier-result.json").read_text(encoding="utf-8")
    )
    assert first.state["status"] == "partial"
    assert first.state["pauseReason"] == "finalization_validation_failed"
    assert first.get_task_status("report") == "completed"
    assert packet["status"] == "partial"
    assert packet["requiresFrontierReview"] is False
    with pytest.raises(WorkspaceError, match="Final evidence"):
        cleanup_worktree(
            snapshot,
            task_states=first.state["tasks"],
            pause_reason=first.state["pauseReason"],
        )

    Path(snapshot["worktreePath"], "rogue.txt").unlink()
    with patch(
        "mlx_swarm.executor.MLXBatchBackend",
        side_effect=AssertionError("model must not load"),
    ) as model:
        resumed = execute_plan(
            config,
            plan,
            session_dir=session_dir,
            approval_poll_seconds=0.01,
        )

    model.assert_not_called()
    assert resumed.state["status"] == "completed"
    assert resumed.state["reviewStatus"] == "awaiting_review"
    completed_packet = json.loads(
        (session_dir / "frontier-result.json").read_text(
            encoding="utf-8"
        )
    )
    output_evidence = completed_packet["workspace"][
        "nonMutatingOutputs"
    ][0]
    assert output_evidence["output"] == "local report"
    assert output_evidence["manifest"]["taskId"] == "report"
    assert output_evidence["artifactSha256"] == output_evidence[
        "manifest"
    ]["sha256"]
    assert output_evidence["payloadSha256"] == hashlib.sha256(
        b"local report"
    ).hexdigest()
    assert completed_packet["tasks"]["report"]["output"] == "local report"


def test_failed_checkout_rejection_remains_replayable_after_cleanup(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    plan_path = _plan_file(repo)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["workspace"]["verificationProfiles"]["syntax"]["argv"] = [
        "python",
        "-c",
        "from pathlib import Path; Path('src/value.py').write_text('VALUE = 99\\n')",
    ]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    _git(repo, "add", "config/swarm.json", "config/plan.json")
    _git(repo, "commit", "-qm", "add mutating verification")
    config = load_config(config_path)
    plan = load_plan(plan_path, config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="checkout",
    )
    session_id = "replay-rejection"
    snapshot = prepare_workspace(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="checkout",
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )
    first = execute_plan(
        config,
        plan,
        session_dir=session_dir,
        backend=_Backend(),
        approval_poll_seconds=0.01,
    )
    manifest = first.state["tasks"]["change"]["artifact"]
    submit_artifact_decision(
        session_dir,
        "change",
        action="reject",
        artifact_sha256=manifest["sha256"],
        source="test",
    )
    failed_reject = execute_plan(
        config,
        plan,
        session_dir=session_dir,
        backend=_Backend(),
        approval_poll_seconds=0.01,
    )
    assert failed_reject.get_task_status("change") == "verification_failed"
    assert "rejection.json" not in failed_reject.state["tasks"]["change"].get(
        "processedVerificationRequests",
        [],
    )

    _git(repo, "restore", "--source=HEAD", "--staged", "--worktree", "src/value.py")
    recovered = execute_plan(
        config,
        plan,
        session_dir=session_dir,
        backend=_Backend(),
        approval_poll_seconds=0.01,
    )
    assert recovered.get_task_status("change") == "rejected_by_operator"
    assert recovered.state["tasks"]["change"]["revertReceipt"]["revertCommit"]
    assert "rejection.json" in recovered.state["tasks"]["change"][
        "processedVerificationRequests"
    ]


def test_executor_waits_for_digest_bound_human_apply(tmp_path: Path) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(config, plan)
    session_id = "executor-wait"
    snapshot = prepare_worktree(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(config_source=config.source, plan_source=plan.source)
    session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )
    backend = _Backend()
    outcome: list[Session] = []
    thread = threading.Thread(
        target=lambda: outcome.append(execute_plan(
            config,
            plan,
            session_dir=session_dir,
            backend=backend,
            approval_poll_seconds=0.01,
        )),
        daemon=True,
    )
    thread.start()
    manifest_path = session_dir / "artifacts" / "change" / "manifest.json"
    deadline = time.time() + 5
    while time.time() < deadline and not manifest_path.is_file():
        time.sleep(0.01)
    assert manifest_path.is_file()
    assert thread.is_alive()
    assert backend.closed is False
    manifest = json.loads(manifest_path.read_text())
    submit_artifact_decision(
        session_dir,
        "change",
        action="apply",
        artifact_sha256=manifest["sha256"],
        source="test",
    )
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert outcome[0].state["status"] == "completed"
    assert outcome[0].get_task_status("change") == "completed"
    assert backend.calls == 1
    packet = json.loads(
        (session_dir / "frontier-result.json").read_text()
    )
    assert packet["schemaVersion"] == 3
    assert packet["requiresFrontierReview"] is True
    assert packet["workspace"]["baseSha"] == snapshot["baseSha"]
    assert packet["workspace"]["headSha"] != snapshot["baseSha"]
    assert packet["workspace"]["appliedArtifacts"][0]["taskId"] == "change"
    assert packet["workspace"]["verificationReceipts"][0]["passed"] is True
    assert "VALUE = 2" in packet["workspace"]["finalDiff"]


def test_structural_patch_failure_uses_bounded_local_repair(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    raw = json.loads(_plan_file(repo).read_text(encoding="utf-8"))
    raw["tasks"][0]["maxRepairAttempts"] = 1
    plan_path = repo / "config" / "repair-plan.json"
    plan_path.write_text(json.dumps(raw), encoding="utf-8")
    plan = load_plan(plan_path, config)
    preview = execution_preview(config, plan)
    session_id = "repair-boundary"
    snapshot = prepare_worktree(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(config_source=config.source, plan_source=plan.source)
    session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )
    backend = _SequenceBackend([
        "This is not a unified diff.",
        _diff(),
    ])
    outcome: list[Session] = []
    thread = threading.Thread(
        target=lambda: outcome.append(execute_plan(
            config,
            plan,
            session_dir=session_dir,
            backend=backend,
            approval_poll_seconds=0.01,
        )),
        daemon=True,
    )
    thread.start()
    manifest_path = session_dir / "artifacts" / "change" / "manifest.json"
    deadline = time.time() + 5
    while time.time() < deadline and not manifest_path.is_file():
        time.sleep(0.01)
    manifest = json.loads(manifest_path.read_text())
    submit_artifact_decision(
        session_dir,
        "change",
        action="apply",
        artifact_sha256=manifest["sha256"],
        source="test",
    )
    thread.join(timeout=10)

    task = outcome[0].state["tasks"]["change"]
    assert outcome[0].state["status"] == "completed"
    assert task["repairAttempts"] == 1
    assert backend.calls == 2


def _set_profile_argv(config_path: Path, argv: list[str]) -> None:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["workspace"]["verificationProfiles"]["syntax"]["argv"] = argv
    config_path.write_text(json.dumps(raw), encoding="utf-8")


def _prepared_session(
    tmp_path: Path,
    *,
    session_id: str,
) -> tuple[Path, Any, Any, dict[str, Any], Session]:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(config, plan)
    snapshot = prepare_worktree(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(config_source=config.source, plan_source=plan.source)
    session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )
    return repo, config, plan, snapshot, session


def test_resume_reconciles_persisted_passing_verification(
    tmp_path: Path,
) -> None:
    _repo_path, config, plan, snapshot, session = _prepared_session(
        tmp_path,
        session_id="verification-crash-recovery",
    )
    task = plan.tasks[0]
    manifest = persist_artifact(
        session.dir,
        task,
        _diff(),
        snapshot,
    )
    apply_receipt = apply_artifact(
        session.dir,
        task,
        snapshot,
        expected_artifact_sha256=manifest["sha256"],
    )
    verification_results = run_verifications(
        session.dir,
        task,
        snapshot,
    )
    assert verification_results[0]["passed"] is True
    session.update_workspace(snapshot)
    session.update_task(
        task.id,
        status="verifying",
        normalizedOutput=_diff(),
        artifact=manifest,
        applyReceipt=apply_receipt,
        verificationResults=[],
        error=None,
    )

    with patch(
        "mlx_swarm.executor.MLXBatchBackend",
        side_effect=AssertionError("model must not load"),
    ) as model:
        resumed = execute_plan(
            config,
            plan,
            session_dir=session.dir,
            approval_poll_seconds=0.01,
        )

    model.assert_not_called()
    assert resumed.state["status"] == "completed"
    recovered = resumed.state["tasks"][task.id]
    assert recovered["status"] == "completed"
    assert recovered["recoveredVerificationAfterCrash"] is True
    assert recovered["verificationResults"] == verification_results


def test_execution_digest_binds_plan_and_current_head(tmp_path: Path) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    plan_path = _plan_file(repo)
    plan = load_plan(plan_path, config)
    preview = execution_preview(config, plan)

    assert preview["planSha256"]
    raw = json.loads(plan_path.read_text(encoding="utf-8"))
    raw["objective"] = "A changed objective"
    plan_path.write_text(json.dumps(raw), encoding="utf-8")
    changed_plan = load_plan(plan_path, config)
    changed = execution_preview(config, changed_plan)
    assert changed["planSha256"] != preview["planSha256"]
    assert changed["executionDigest"] != preview["executionDigest"]
    with pytest.raises(WorkspaceError, match="Execution digest mismatch"):
        prepare_worktree(
            config,
            changed_plan,
            session_id="stale-plan",
            expected_execution_digest=preview["executionDigest"],
        )

    (repo / "head-change.txt").write_text("new\n", encoding="utf-8")
    _git(repo, "add", "head-change.txt")
    _git(repo, "commit", "-qm", "move head")
    moved = execution_preview(config, changed_plan)
    assert moved["baseSha"] != preview["baseSha"]
    assert moved["executionDigest"] != changed["executionDigest"]


def test_workspace_readiness_rejects_external_diff_driver(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    _git(repo, "config", "diff.unsafe.command", "/bin/false")

    readiness = workspace_readiness(config)
    assert readiness["ready"] is False
    assert "diff drivers" in readiness["error"]
    with pytest.raises(WorkspaceError, match="diff drivers"):
        execution_preview(config, load_plan(_plan_file(repo), config))


def test_workspace_disables_global_external_git_drivers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(
        "[filter \"lfs\"]\n"
        "\tclean = git-lfs clean -- %f\n"
        "\tsmudge = git-lfs smudge -- %f\n"
        "\tprocess = git-lfs filter-process\n"
        "[diff \"global-unsafe\"]\n"
        "\tcommand = /bin/false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.injected.command")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/bin/false")
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)

    readiness = workspace_readiness(config)
    assert readiness["ready"] is True
    preview = execution_preview(config, load_plan(_plan_file(repo), config))
    assert preview["workspaceRoot"] == str(repo.resolve())


def test_patch_rejects_symlink_and_submodule_modes(tmp_path: Path) -> None:
    repo, config_path = _repo(tmp_path)
    (repo / "src" / "linked").symlink_to("../tests")
    _git(repo, "add", "src/linked")
    _git(repo, "commit", "-qm", "add symlink")
    config = load_config(config_path)
    raw = json.loads(_plan_file(repo).read_text(encoding="utf-8"))
    raw["tasks"][0]["allowedPaths"] = ["src"]
    plan_path = repo / "config" / "symlink-plan.json"
    plan_path.write_text(json.dumps(raw), encoding="utf-8")
    plan = load_plan(plan_path, config)
    preview = execution_preview(config, plan)
    snapshot = prepare_worktree(
        config,
        plan,
        session_id="symlink-path",
        expected_execution_digest=preview["executionDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / "symlink-path"
    session_dir.mkdir(parents=True)
    symlink_diff = (
        "diff --git a/src/linked/new.py b/src/linked/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/linked/new.py\n"
        "@@ -0,0 +1 @@\n"
        "+VALUE = 2\n"
    )
    with pytest.raises(WorkspaceError, match="symlink"):
        persist_artifact(
            session_dir,
            plan.tasks[0],
            symlink_diff,
            snapshot,
        )
    submodule_diff = (
        "diff --git a/src/module b/src/module\n"
        "index 1234567..7654321 160000\n"
        "--- a/src/module\n"
        "+++ b/src/module\n"
        "@@ -1 +1 @@\n"
        "-Subproject commit 1234567\n"
        "+Subproject commit 7654321\n"
    )
    with pytest.raises(WorkspaceError, match="submodules"):
        persist_artifact(
            session_dir,
            plan.tasks[0],
            submodule_diff,
            snapshot,
        )


def test_failed_verification_waits_then_reruns_without_worker_call(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "verification-allowed"
    repo, config_path = _repo(tmp_path)
    _set_profile_argv(
        config_path,
        [
            "python",
            "-c",
            (
                "from pathlib import Path; import sys; "
                f"sys.exit(0 if Path({str(marker)!r}).is_file() else 7)"
            ),
        ],
    )
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(config, plan)
    session_id = "verification-rerun"
    snapshot = prepare_worktree(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(config_source=config.source, plan_source=plan.source)
    session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )
    backend = _Backend()
    outcome: list[Session] = []
    thread = threading.Thread(
        target=lambda: outcome.append(execute_plan(
            config,
            plan,
            session_dir=session_dir,
            backend=backend,
            approval_poll_seconds=0.01,
        )),
        daemon=True,
    )
    thread.start()
    manifest_path = session_dir / "artifacts" / "change" / "manifest.json"
    deadline = time.time() + 5
    while time.time() < deadline and not manifest_path.is_file():
        time.sleep(0.01)
    manifest = json.loads(manifest_path.read_text())
    submit_artifact_decision(
        session_dir,
        "change",
        action="apply",
        artifact_sha256=manifest["sha256"],
        source="test",
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        state = json.loads((session_dir / "session.json").read_text())
        if state["tasks"]["change"]["status"] == "verification_failed":
            break
        time.sleep(0.01)
    else:
        pytest.fail("verification failure was not persisted")
    assert thread.is_alive()
    marker.write_text("approved by operator\n", encoding="utf-8")
    submit_artifact_decision(
        session_dir,
        "change",
        action="verify",
        artifact_sha256=manifest["sha256"],
        source="test",
    )
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert outcome[0].state["status"] == "completed"
    results = outcome[0].state["tasks"]["change"]["verificationResults"]
    assert [result["exitCode"] for result in results] == [7, 0]
    assert backend.calls == 1


def test_failed_verification_reject_creates_revert_commit(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    _set_profile_argv(
        config_path,
        ["python", "-c", "raise SystemExit(9)"],
    )
    config = load_config(config_path)
    plan = load_plan(_plan_file(repo), config)
    preview = execution_preview(config, plan)
    session_id = "verification-reject"
    snapshot = prepare_worktree(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(config_source=config.source, plan_source=plan.source)
    session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )
    outcome: list[Session] = []
    thread = threading.Thread(
        target=lambda: outcome.append(execute_plan(
            config,
            plan,
            session_dir=session_dir,
            backend=_Backend(),
            approval_poll_seconds=0.01,
        )),
        daemon=True,
    )
    thread.start()
    manifest_path = session_dir / "artifacts" / "change" / "manifest.json"
    deadline = time.time() + 5
    while time.time() < deadline and not manifest_path.is_file():
        time.sleep(0.01)
    manifest = json.loads(manifest_path.read_text())
    submit_artifact_decision(
        session_dir,
        "change",
        action="apply",
        artifact_sha256=manifest["sha256"],
        source="test",
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        state = json.loads((session_dir / "session.json").read_text())
        if state["tasks"]["change"]["status"] == "verification_failed":
            break
        time.sleep(0.01)
    submit_artifact_decision(
        session_dir,
        "change",
        action="reject",
        artifact_sha256=manifest["sha256"],
        source="test",
        reason="The allowlisted check failed.",
    )
    thread.join(timeout=10)

    assert outcome[0].state["status"] == "partial"
    task = outcome[0].state["tasks"]["change"]
    assert task["status"] == "rejected_by_operator"
    assert task["revertReceipt"]["revertCommit"]
    worktree = Path(snapshot["worktreePath"])
    assert (worktree / "src" / "value.py").read_text() == "VALUE = 1\n"
    assert _git(worktree, "rev-parse", "HEAD") != snapshot["baseSha"]
    assert (repo / "src" / "value.py").read_text() == "VALUE = 1\n"
    packet = json.loads(
        (session_dir / "frontier-result.json").read_text()
    )
    assert packet["schemaVersion"] == 3
    assert packet["requiresFrontierReview"] is False
    assert "finalDiff" not in packet["workspace"]


def test_operator_rejection_blocks_descendants_without_more_generation(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    config = load_config(config_path)
    raw = json.loads(_plan_file(repo).read_text(encoding="utf-8"))
    raw["tasks"].append({
        "id": "report",
        "role": "general",
        "prompt": "Report the applied change.",
        "dependsOn": ["change"],
        "artifactType": "report",
        "allowedPaths": [],
        "verification": [],
    })
    plan_path = repo / "config" / "rejection-plan.json"
    plan_path.write_text(json.dumps(raw), encoding="utf-8")
    plan = load_plan(plan_path, config)
    preview = execution_preview(config, plan)
    session_id = "operator-rejection"
    snapshot = prepare_worktree(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=preview["executionDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
    session.set_sources(config_source=config.source, plan_source=plan.source)
    session.attach_workspace(
        snapshot,
        execution_approval=_approval(preview),
    )
    backend = _Backend()
    outcome: list[Session] = []
    thread = threading.Thread(
        target=lambda: outcome.append(execute_plan(
            config,
            plan,
            session_dir=session_dir,
            backend=backend,
            approval_poll_seconds=0.01,
        )),
        daemon=True,
    )
    thread.start()
    manifest_path = session_dir / "artifacts" / "change" / "manifest.json"
    deadline = time.time() + 5
    while time.time() < deadline and not manifest_path.is_file():
        time.sleep(0.01)
    manifest = json.loads(manifest_path.read_text())
    submit_artifact_decision(
        session_dir,
        "change",
        action="reject",
        artifact_sha256=manifest["sha256"],
        source="test",
    )
    thread.join(timeout=10)

    assert outcome[0].state["status"] == "partial"
    assert outcome[0].get_task_status("change") == "rejected_by_operator"
    assert outcome[0].get_task_status("report") == "blocked"
    assert backend.calls == 1
    assert not (session_dir / "artifacts" / "report").exists()


def test_crash_recovery_recognizes_committed_artifact(
    tmp_path: Path,
) -> None:
    _repo_path, config, plan, snapshot, session = _prepared_session(
        tmp_path,
        session_id="recover-commit",
    )
    manifest = persist_artifact(
        session.dir,
        plan.tasks[0],
        _diff(),
        snapshot,
    )
    receipt = apply_artifact(
        session.dir,
        plan.tasks[0],
        snapshot,
        expected_artifact_sha256=manifest["sha256"],
    )
    # Simulate the crash window after the receipt/commit but before the
    # updated snapshot and task state were atomically saved.
    stale = session.workspace_snapshot()
    assert stale is not None
    stale["headSha"] = manifest["baseCommit"]
    recovered = recover_artifact_application(
        session.dir,
        plan.tasks[0],
        stale,
        expected_artifact_sha256=manifest["sha256"],
    )
    assert recovered["state"] == "applied"
    assert recovered["receipt"]["commitSha"] == receipt["commitSha"]
    assert stale["headSha"] == receipt["commitSha"]


def test_crash_recovery_commits_only_exact_staged_artifact(
    tmp_path: Path,
) -> None:
    _repo_path, _config, plan, snapshot, session = _prepared_session(
        tmp_path,
        session_id="recover-staged",
    )
    manifest = persist_artifact(
        session.dir,
        plan.tasks[0],
        _diff(),
        snapshot,
    )
    worktree = Path(snapshot["worktreePath"])
    subprocess.run(
        ["git", "-C", str(worktree), "apply", "--index", "--recount", "-"],
        input=_diff(),
        text=True,
        check=True,
    )

    recovered = recover_artifact_application(
        session.dir,
        plan.tasks[0],
        snapshot,
        expected_artifact_sha256=manifest["sha256"],
    )

    assert recovered["state"] == "applied"
    assert recovered["receipt"]["recoveredAfterCrash"] is True
    assert _git(worktree, "show", "HEAD:src/value.py") == "VALUE = 2"


def test_crash_recovery_rejects_extra_staged_same_file_content(
    tmp_path: Path,
) -> None:
    _repo_path, _config, plan, snapshot, session = _prepared_session(
        tmp_path,
        session_id="recover-extra-stage",
    )
    manifest = persist_artifact(
        session.dir,
        plan.tasks[0],
        _diff(),
        snapshot,
    )
    worktree = Path(snapshot["worktreePath"])
    (worktree / "src" / "value.py").write_text(
        "VALUE = 3\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "src/value.py")

    with pytest.raises(WorkspaceError, match="sealed patch"):
        recover_artifact_application(
            session.dir,
            plan.tasks[0],
            snapshot,
            expected_artifact_sha256=manifest["sha256"],
        )


def test_crash_recovery_rejects_wrong_same_path_commit(
    tmp_path: Path,
) -> None:
    _repo_path, _config, plan, snapshot, session = _prepared_session(
        tmp_path,
        session_id="recover-wrong-commit",
    )
    manifest = persist_artifact(
        session.dir,
        plan.tasks[0],
        _diff(),
        snapshot,
    )
    worktree = Path(snapshot["worktreePath"])
    (worktree / "src" / "value.py").write_text(
        "VALUE = 3\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "src/value.py")
    _git(worktree, "commit", "-qm", "different same-file commit")

    with pytest.raises(WorkspaceError, match="lineage changed"):
        recover_artifact_application(
            session.dir,
            plan.tasks[0],
            snapshot,
            expected_artifact_sha256=manifest["sha256"],
        )


def test_cleanup_removes_only_worktree_and_retains_branch(
    tmp_path: Path,
) -> None:
    repo, config, plan, snapshot, _session = _prepared_session(
        tmp_path,
        session_id="cleanup-session",
    )
    branch = snapshot["branch"]
    worktree = Path(snapshot["worktreePath"])
    cleanup_worktree(snapshot)

    assert not worktree.exists()
    assert snapshot["cleanedUp"] is True
    assert _git(repo, "show-ref", "--verify", f"refs/heads/{branch}")
    assert (repo / "src" / "value.py").read_text() == "VALUE = 1\n"


def test_concurrent_artifact_decisions_cannot_overwrite_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo_path, _config, plan, snapshot, session = _prepared_session(
        tmp_path,
        session_id="decision-race",
    )
    manifest = persist_artifact(
        session.dir,
        plan.tasks[0],
        _diff(),
        snapshot,
    )
    published: list[tuple[bool, dict[str, Any]]] = []
    real_link = workspace_module.os.link

    def inspect_publish(source: str, target: str) -> None:
        if Path(target).name == "decision.json":
            published.append((
                Path(target).exists(),
                json.loads(Path(source).read_text(encoding="utf-8")),
            ))
        real_link(source, target)

    monkeypatch.setattr(workspace_module.os, "link", inspect_publish)
    barrier = threading.Barrier(3)
    accepted: list[dict[str, Any]] = []
    errors: list[Exception] = []

    def decide(action: str) -> None:
        barrier.wait()
        try:
            accepted.append(submit_artifact_decision(
                session.dir,
                "change",
                action=action,
                artifact_sha256=manifest["sha256"],
                source="test",
            ))
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=decide, args=("apply",)),
        threading.Thread(target=decide, args=("reject",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert len(accepted) == 1
    assert len(errors) == 1
    assert published
    assert published[0][0] is False
    assert all(value["action"] in {"apply", "reject"} for _, value in published)
    persisted = json.loads(
        (
            session.dir
            / "artifacts"
            / "change"
            / "decision.json"
        ).read_text()
    )
    assert persisted == accepted[0]
