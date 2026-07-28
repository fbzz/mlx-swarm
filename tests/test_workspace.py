"""Workspace execution boundary tests using real temporary Git repositories."""
# @lat: [[Tests#Workspace execution]]

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

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
    persist_artifact,
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
    )
    results = run_verifications(
        session_dir,
        plan.tasks[0],
        snapshot,
    )
    assert receipt["commitSha"] == snapshot["headSha"]
    assert results[0]["passed"] is True
    assert (Path(snapshot["worktreePath"]) / "src" / "value.py").read_text() == "VALUE = 2\n"
    assert (repo / "src" / "value.py").read_text() == "VALUE = 1\n"


def test_verification_uses_exact_argv_no_shell_and_sanitized_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo_path, config, plan, snapshot, session = _prepared_session(
        tmp_path,
        session_id="popen-boundary",
    )
    persist_artifact(
        session.dir,
        plan.tasks[0],
        _diff(),
        snapshot,
    )
    apply_artifact(session.dir, plan.tasks[0], snapshot)
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
    argv, kwargs = calls[0]
    assert argv == list(
        snapshot["verificationProfiles"]["syntax"]["argv"]
    )
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
    persist_artifact(session_dir, plan.tasks[0], _diff(), snapshot)
    apply_artifact(session_dir, plan.tasks[0], snapshot)
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
    persist_artifact(session_dir, plan.tasks[0], _diff(), snapshot)
    apply_artifact(session_dir, plan.tasks[0], snapshot)
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
        execution_approval={
            "planSha256": "test",
            "executionDigest": preview["executionDigest"],
        },
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
        execution_approval={
            "planSha256": preview["planSha256"],
            "executionDigest": preview["executionDigest"],
        },
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
        execution_approval={
            "planSha256": preview["planSha256"],
            "executionDigest": preview["executionDigest"],
        },
    )
    return repo, config, plan, snapshot, session


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
        execution_approval={
            "planSha256": preview["planSha256"],
            "executionDigest": preview["executionDigest"],
        },
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
        execution_approval={
            "planSha256": preview["planSha256"],
            "executionDigest": preview["executionDigest"],
        },
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
        execution_approval={
            "planSha256": preview["planSha256"],
            "executionDigest": preview["executionDigest"],
        },
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
    )
    assert recovered["state"] == "applied"
    assert recovered["receipt"]["commitSha"] == receipt["commitSha"]
    assert stale["headSha"] == receipt["commitSha"]


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
    persisted = json.loads(
        (
            session.dir
            / "artifacts"
            / "change"
            / "decision.json"
        ).read_text()
    )
    assert persisted == accepted[0]
