"""Tests for the localhost work cockpit server and API."""
# @lat: [[Tests#UI]]

from __future__ import annotations

import json
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from mlx_swarm.contracts import load_config, load_plan
from mlx_swarm.executor import execute_plan
from mlx_swarm.session import Session
from mlx_swarm.ui import APIError, CockpitApp, make_handler
from mlx_swarm.workspace import execution_preview, persist_artifact


def test_packaged_styles_preserve_native_hidden_state() -> None:
    styles = (
        Path(__file__).parents[1]
        / "src"
        / "mlx_swarm"
        / "ui_static"
        / "styles.css"
    ).read_text(encoding="utf-8")

    assert "[hidden] { display: none !important; }" in styles


def test_packaged_cockpit_exposes_incremental_revision_lineage() -> None:
    static = (
        Path(__file__).parents[1]
        / "src"
        / "mlx_swarm"
        / "ui_static"
    )
    html = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")

    assert 'id="commander-revision"' in html
    assert 'revisionOf = el("commander-revision")' in script
    assert "...(revisionOf ? {revisionOf} : {})" in script
    assert "revision?.inspectionRoot" in script
    assert "schemaVersion === 2" not in script
    assert script.count("schemaVersion >= 2") == 4


def _write_workspace(tmp_path: Path) -> tuple[Path, Path]:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.safetensors").write_bytes(b"")
    config = {
        "schemaVersion": 1,
        "model": {
            "repository": "local/test-model",
            "localPath": str(model_dir),
        },
        "batch": {"maxWorkers": 4},
        "artifacts": str(tmp_path / "runs"),
    }
    config_path = tmp_path / "swarm.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    plan = {
        "schemaVersion": 1,
        "planId": "cockpit-plan",
        "objective": "Build and verify a local result",
        "context": {
            "objective": "Build and verify a local result",
            "authoritativeSources": [{
                "label": "request",
                "content": "Implement def result and verify it.",
            }],
            "constraints": [],
            "rejectionCriteria": ["def result is absent."],
            "outputProtocol": "Return the requested artifact.",
            "diagnosis": {
                "observedFailure": "The requested result is absent.",
                "causalHypothesis": (
                    "The implementation task has not produced def result."
                ),
                "validationMethod": "source-trace",
                "validationEvidence": (
                    "The request source explicitly requires def result."
                ),
                "falsificationCondition": (
                    "An authoritative source already contains def result."
                ),
                "evidenceSources": ["request"],
                "changeValidation": {
                    "candidateChange": (
                        "Add only the requested def result implementation."
                    ),
                    "failingPathPrediction": (
                        "The missing-definition path now exposes def result."
                    ),
                    "preservedControlPrediction": (
                        "Existing unrelated definitions remain unchanged."
                    ),
                    "minimalityEvidence": (
                        "Adding the one requested definition is the narrowest "
                        "change that satisfies the request."
                    ),
                    "evidenceSources": ["request"],
                },
            },
        },
        "tasks": [
            {
                "id": "implement",
                "role": "implementation",
                "prompt": "Implement the result.",
                "gate": {
                    "requiredPatterns": [
                        {"id": "definition", "pattern": "def result"}
                    ],
                    "forbiddenPatterns": [],
                    "maxCharacters": 2000,
                    "pythonSyntax": True,
                },
            },
            {
                "id": "test",
                "role": "test",
                "prompt": "Test the implementation.",
                "dependsOn": ["implement"],
            },
            {
                "id": "review",
                "role": "review",
                "prompt": "Review the implementation.",
                "dependsOn": ["implement"],
            },
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return config_path, plan_path


class _FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid

    def poll(self) -> None:
        return None


class _PopenRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> _FakeProcess:
        self.calls.append((argv, kwargs))
        return _FakeProcess(41_000 + len(self.calls))


class _FakeBackend:
    def __init__(self, responses: list[list[str]]):
        self.responses = list(responses)

    def generate(self, tasks, prompts):
        response = self.responses.pop(0)
        return response, {
            "batchSize": len(tasks),
            "promptTokens": len(prompts) * 10,
            "generationTokens": len(response) * 5,
            "groups": [{"size": len(tasks)}],
        }

    def close(self) -> None:
        return


def _app(tmp_path: Path, recorder: _PopenRecorder | None = None) -> CockpitApp:
    config_path, _ = _write_workspace(tmp_path)
    return CockpitApp(
        load_config(config_path),
        tmp_path,
        popen_factory=recorder or _PopenRecorder(),
    )


def test_evaluation_api_payload_is_read_only_and_path_safe(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    evaluation_id = "bugsinpy-v1-example"
    root = app.evaluations.root / evaluation_id
    root.mkdir(parents=True)
    (root / "evaluation.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "evaluationId": evaluation_id,
            "status": "prepared",
            "pilotStatus": "pending",
            "measuredStatus": "locked",
            "createdAt": "2026-07-28T00:00:00+00:00",
            "updatedAt": "2026-07-28T00:00:00+00:00",
            "results": {},
        }),
        encoding="utf-8",
    )
    (root / "suite.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "suiteId": evaluation_id,
            "profileId": "bugsinpy-v1",
            "benchmark": {
                "name": "BugsInPy",
                "repository": "https://example.invalid/benchmark.git",
                "revision": "a" * 40,
            },
            "seed": 20260728,
            "createdAt": "2026-07-28T00:00:00+00:00",
            "cases": [],
        }),
        encoding="utf-8",
    )
    (root / "environment.json").write_text(
        json.dumps({"schemaVersion": 1}),
        encoding="utf-8",
    )

    listing = app.evaluations_payload()
    assert listing["evaluations"][0]["evaluationId"] == evaluation_id
    detail = app.evaluation_detail(evaluation_id)
    assert detail["evaluation"]["measuredStatus"] == "locked"
    with pytest.raises(APIError):
        app.evaluation_detail("../outside")


def _git_ui(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _write_v2_workspace(
    tmp_path: Path,
) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_ui(repo, "init", "-q")
    _git_ui(repo, "config", "user.name", "Test")
    _git_ui(repo, "config", "user.email", "test@example.invalid")
    (repo / ".gitignore").write_text(
        "config/.swarm/\n",
        encoding="utf-8",
    )
    (repo / "src").mkdir()
    (repo / "src" / "value.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    model_dir = repo / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.safetensors").write_bytes(b"")
    config_dir = repo / "config"
    config_dir.mkdir()
    config_path = config_dir / "swarm.json"
    config_path.write_text(
        json.dumps({
            "schemaVersion": 2,
            "model": {
                "repository": "local/test-model",
                "localPath": str(model_dir),
            },
            "batch": {"maxWorkers": 2},
            "artifacts": ".swarm/runs",
            "workspace": {
                "writeRoots": ["src"],
                "verificationProfiles": {
                    "check": {
                        "argv": [
                            "python",
                            "-c",
                            (
                                "from pathlib import Path; "
                                "assert 'VALUE = 2' in "
                                "Path('src/value.py').read_text()"
                            ),
                        ],
                        "cwd": ".",
                        "timeoutSeconds": 10,
                        "inheritEnv": ["PATH"],
                        "environment": {},
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    plan_path = config_dir / "workspace-plan.json"
    plan_path.write_text(
        json.dumps({
            "schemaVersion": 2,
            "planId": "ui-workspace",
            "objective": "Change one approved file",
            "tasks": [{
                "id": "change",
                "role": "implementation",
                "prompt": "Return the unified diff.",
                "artifactType": "patch",
                "allowedPaths": ["src/value.py"],
                "verification": ["check"],
            }],
        }),
        encoding="utf-8",
    )
    _git_ui(repo, "add", ".")
    _git_ui(repo, "commit", "-qm", "base")
    return repo, config_path, plan_path


def _v2_app(
    tmp_path: Path,
    recorder: _PopenRecorder | None = None,
) -> tuple[CockpitApp, Path, Path]:
    repo, config_path, plan_path = _write_v2_workspace(tmp_path)
    return (
        CockpitApp(
            load_config(config_path),
            config_path.parent,
            popen_factory=recorder or _PopenRecorder(),
        ),
        repo,
        plan_path,
    )


def _ui_diff() -> str:
    return (
        "diff --git a/src/value.py b/src/value.py\n"
        "--- a/src/value.py\n"
        "+++ b/src/value.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )


def test_plan_discovery_and_status(tmp_path: Path) -> None:
    app = _app(tmp_path)
    payload = app.plans_payload()
    assert [plan["planId"] for plan in payload["plans"]] == ["cockpit-plan"]
    assert payload["plans"][0]["tasks"][0]["gate"]["pythonSyntax"] is True
    assert payload["invalid"] == []
    status = app.status_payload()
    assert status["ready"] is True
    assert status["reviewMode"] == "frontier-final-only"
    assert status["batch"]["maxBatchPromptTokens"] == 49152
    assert status["worker"]["capabilities"]["delegationLevel"] == "exact-edit"


def test_plan_discovery_excludes_artifacts_and_duplicates(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    artifact_plan = app.artifacts_dir / "old" / "run" / "plan.snapshot.json"
    artifact_plan.parent.mkdir(parents=True)
    artifact_plan.write_text((tmp_path / "plan.json").read_text(), encoding="utf-8")
    assert len(app.plans_payload()["plans"]) == 1

    duplicate = tmp_path / "nested" / "duplicate.json"
    duplicate.parent.mkdir()
    duplicate.write_text((tmp_path / "plan.json").read_text(), encoding="utf-8")
    payload = app.plans_payload()
    assert payload["plans"] == []
    assert "Duplicate planId" in payload["invalid"][0]["error"]


def test_invalid_plan_is_reported_not_launched(tmp_path: Path) -> None:
    app = _app(tmp_path)
    (tmp_path / "invalid.json").write_text('{"planId": "bad"}', encoding="utf-8")
    payload = app.plans_payload()
    assert len(payload["plans"]) == 1
    assert len(payload["invalid"]) == 1
    with pytest.raises(APIError, match="Unknown or invalid plan"):
        app.launch_run("bad", 2)


def test_launch_snapshots_plan_and_never_uses_shell(tmp_path: Path) -> None:
    recorder = _PopenRecorder()
    app = _app(tmp_path, recorder)
    detail = app.launch_run("cockpit-plan", 3)
    session_dir = (
        app.artifacts_dir
        / "cockpit-plan"
        / detail["run"]["sessionId"]
    )
    state = json.loads((session_dir / "session.json").read_text())

    assert detail["run"]["active"] is True
    assert state["launchSource"] == "ui"
    assert state["planSnapshot"] == "plan.snapshot.json"
    assert (session_dir / "plan.snapshot.json").is_file()
    assert (session_dir / "runner.log").is_file()
    assert json.loads((session_dir / "runner.json").read_text())["pid"] == 41_001
    argv, kwargs = recorder.calls[0]
    assert isinstance(argv, list)
    assert argv[-2:] == ["--max-repair", "3"]
    assert str(session_dir) in argv
    assert kwargs["shell"] is False


def test_launch_rejects_bad_repair_limits(tmp_path: Path) -> None:
    app = _app(tmp_path)
    for value in (-1, 6, True, "2"):
        with pytest.raises(APIError):
            app.launch_run("cockpit-plan", value)  # type: ignore[arg-type]


def test_workspace_plan_requires_both_displayed_digests(
    tmp_path: Path,
) -> None:
    recorder = _PopenRecorder()
    app, repo, _plan_path = _v2_app(tmp_path, recorder)
    catalog = app.plans_payload()["plans"]
    assert len(catalog) == 1
    displayed = catalog[0]
    execution = displayed["execution"]
    assert execution["workspaceRoot"] == str(repo.resolve())
    assert execution["planSha256"] == displayed["digest"]

    with pytest.raises(APIError, match="Plan digest mismatch"):
        app.launch_run(
            "ui-workspace",
            2,
            plan_digest="0" * 64,
            execution_digest=execution["executionDigest"],
        )
    with pytest.raises(APIError, match="Execution digest mismatch"):
        app.launch_run(
            "ui-workspace",
            2,
            plan_digest=displayed["digest"],
            execution_digest="0" * 64,
        )

    launched = app.launch_run(
        "ui-workspace",
        2,
        plan_digest=displayed["digest"],
        execution_digest=execution["executionDigest"],
    )
    run_dir = (
        app.artifacts_dir
        / "ui-workspace"
        / launched["run"]["sessionId"]
    )
    state = json.loads((run_dir / "session.json").read_text())
    snapshot = json.loads(
        (run_dir / "workspace.snapshot.json").read_text()
    )
    assert state["workspaceExecution"] is True
    assert state["executionApproval"]["planSha256"] == displayed["digest"]
    assert snapshot["branch"].startswith("mlx-swarm/ui-workspace/")
    assert Path(snapshot["worktreePath"]).is_dir()
    argv, kwargs = recorder.calls[-1]
    assert argv[2] == "mlx_swarm.cli"
    assert kwargs["shell"] is False


def test_cockpit_launches_digest_bound_main_checkout_yolo(
    tmp_path: Path,
) -> None:
    recorder = _PopenRecorder()
    app, repo, _plan_path = _v2_app(tmp_path, recorder)
    displayed = app.plans_payload()["plans"][0]
    preview = displayed["executionPreviews"]["yolo"]["checkout"]
    assert preview["ready"] is True

    launched = app.launch_run(
        "ui-workspace",
        2,
        plan_digest=displayed["digest"],
        execution_digest=preview["executionDigest"],
        approval_mode="yolo",
        workspace_target="checkout",
    )
    session_dir = (
        app.artifacts_dir
        / "ui-workspace"
        / launched["run"]["sessionId"]
    )
    state = json.loads((session_dir / "session.json").read_text())
    snapshot = json.loads(
        (session_dir / "workspace.snapshot.json").read_text()
    )

    assert state["approvalMode"] == "yolo"
    assert state["workspaceTarget"] == "checkout"
    assert state["executionPolicy"] == preview["executionPolicy"]
    assert snapshot["executionPath"] == str(repo.resolve())
    assert snapshot["cleanupAllowed"] is False
    assert launched["actions"]["cleanupWorkspace"] is False
    argv, kwargs = recorder.calls[-1]
    assert isinstance(argv, list)
    assert kwargs["shell"] is False


def test_workspace_artifact_api_is_digest_bound_and_cleanup_retains_branch(
    tmp_path: Path,
) -> None:
    recorder = _PopenRecorder()
    app, repo, plan_path = _v2_app(tmp_path, recorder)
    plan = load_plan(plan_path, app.config)
    preview = execution_preview(app.config, plan)
    launched = app.launch_run(
        plan.plan_id,
        2,
        plan_digest=preview["planSha256"],
        execution_digest=preview["executionDigest"],
    )
    session_id = launched["run"]["sessionId"]
    session_dir = app.artifacts_dir / plan.plan_id / session_id
    session = Session.load(session_dir, app.config)
    snapshot = session.workspace_snapshot()
    assert snapshot is not None
    manifest = persist_artifact(
        session_dir,
        plan.tasks[0],
        _ui_diff(),
        snapshot,
    )
    session.update_task(
        "change",
        status="awaiting_approval",
        artifact=manifest,
        output=_ui_diff(),
        normalizedOutput=_ui_diff(),
        gateResult={"passed": True, "violations": []},
    )
    session.set_status("awaiting_approval")

    try:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(app),
        )
    except PermissionError:
        pytest.skip("The active sandbox does not allow localhost sockets.")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, payload = _post(
            (
                f"{base}/api/runs/{plan.plan_id}/{session_id}"
                "/artifacts/change/apply"
            ),
            json.dumps({"artifactDigest": "0" * 64}).encode(),
            origin=base,
        )
        assert status == 409
        assert "digest mismatch" in payload["error"].lower()

        status, detail = _post(
            (
                f"{base}/api/runs/{plan.plan_id}/{session_id}"
                "/artifacts/change/apply"
            ),
            json.dumps({
                "artifactDigest": manifest["sha256"],
            }).encode(),
            origin=base,
        )
        assert status == 202
        assert detail["artifacts"]["change"]["payload"].startswith(
            "diff --git"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    decision = json.loads(
        (
            session_dir
            / "artifacts"
            / "change"
            / "decision.json"
        ).read_text()
    )
    assert decision["action"] == "apply"
    # Mark the fake launched run terminal; cleanup removes only its worktree.
    terminal = Session.load(session_dir, app.config)
    terminal.set_status("partial")
    branch = snapshot["branch"]
    worktree = Path(snapshot["worktreePath"])

    with pytest.raises(APIError, match="Resumable workspace work"):
        app.cleanup_run_workspace(plan.plan_id, session_id)

    # Simulate the inactive runner sealing the task as terminal. A top-level
    # terminal label alone must not delete a worktree that still has resumable
    # artifact work.
    terminal.update_task("change", status="failed")
    cleaned = app.cleanup_run_workspace(plan.plan_id, session_id)
    assert cleaned["workspace"]["cleanedUp"] is True
    assert not worktree.exists()
    assert _git_ui(
        repo,
        "show-ref",
        "--verify",
        f"refs/heads/{branch}",
    )
    assert (repo / "src" / "value.py").read_text() == "VALUE = 1\n"


def test_commander_request_preview_and_digest_approval(
    tmp_path: Path,
) -> None:
    recorder = _PopenRecorder()
    app = _app(tmp_path, recorder)
    created = app.create_commander_request(
        "Build and verify a local result",
        ["Stay local."],
        None,
    )
    request_id = created["request"]["requestId"]
    claim = app.commander.claim_plan(request_id)
    response = tmp_path / "commander-response.json"
    response.write_text(
        (tmp_path / "plan.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    imported = app.commander.import_plan(
        request_id,
        response,
        claim_id=claim["claimId"],
    )
    digest = imported["request"]["planDigest"]

    assert imported["plan"]["levels"] == [
        ["implement"],
        ["test", "review"],
    ]
    with pytest.raises(APIError, match="digest mismatch"):
        app.approve_commander_run(request_id, "0" * 64, 2)

    detail = app.approve_commander_run(request_id, digest, 3)
    session_dir = (
        app.artifacts_dir
        / detail["run"]["planId"]
        / detail["run"]["sessionId"]
    )
    state = json.loads((session_dir / "session.json").read_text())
    assert state["launchSource"] == "commander"
    assert state["commanderRequestId"] == request_id
    assert state["planApproval"]["planSha256"] == digest
    assert state["reviewStatus"] == "pending_local"
    assert (session_dir / "frontier-plan-receipt.json").is_file()
    usage = json.loads((session_dir / "frontier-usage.json").read_text())
    assert usage["planning"]["acceptedResponses"] == 1
    assert usage["planning"]["usageStatus"] == "unavailable"
    assert recorder.calls[0][0][2] == "mlx_swarm.cli"
    assert recorder.calls[0][1]["shell"] is False
    request = app.commander.request_detail(request_id)["request"]
    assert request["status"] == "launched"
    assert request["sessionRef"] == (
        f"{detail['run']['planId']}/{detail['run']['sessionId']}"
    )

    old = Session.load(session_dir, app.config)
    old.set_status("partial")
    retried = app.retry_run(
        detail["run"]["planId"],
        detail["run"]["sessionId"],
        1,
    )
    retry_dir = (
        app.artifacts_dir
        / retried["run"]["planId"]
        / retried["run"]["sessionId"]
    )
    retry_state = json.loads((retry_dir / "session.json").read_text())
    assert retry_state["retryOf"] == (
        f"{detail['run']['planId']}/{detail['run']['sessionId']}"
    )
    assert retry_state["commanderRequestId"] == request_id
    assert (retry_dir / "frontier-plan-receipt.json").is_file()
    app._processes.clear()
    (retry_dir / "runner.json").unlink(missing_ok=True)
    resumed = app.resume_run(
        retried["run"]["planId"],
        retried["run"]["sessionId"],
    )
    assert resumed["run"]["sessionId"] == retried["run"]["sessionId"]
    assert "resume" in recorder.calls[-1][0]


def test_failed_commander_attachment_is_not_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(tmp_path, _PopenRecorder())
    created = app.create_commander_request(
        "Build and verify a local result",
        [],
        None,
    )
    request_id = created["request"]["requestId"]
    claim = app.commander.claim_plan(request_id)
    imported = app.commander.import_plan(
        request_id,
        tmp_path / "plan.json",
        claim_id=claim["claimId"],
    )
    digest = imported["request"]["planDigest"]
    plan, plan_path, approval, receipt, request = (
        app.commander.approved_plan(request_id, digest)
    )

    def fail_attachment(self, **_kwargs):
        raise RuntimeError("injected commander attachment failure")

    monkeypatch.setattr(Session, "attach_commander", fail_attachment)
    with pytest.raises(
        RuntimeError,
        match="injected commander attachment failure",
    ):
        app.launch_run(
            plan.plan_id,
            0,
            plan_override=(plan, plan_path),
            commander_evidence={
                "requestId": request_id,
                "approval": approval,
                "planningReceipt": receipt,
                "revisionOf": request.get("revisionOf"),
                "revisionInput": None,
                "revisionInputSha256": None,
                "revisionAuthority": None,
            },
            mark_commander_launched=True,
            plan_digest=digest,
        )

    session_dirs = [
        path
        for path in (app.artifacts_dir / plan.plan_id).iterdir()
        if path.is_dir()
    ]
    assert len(session_dirs) == 1
    failed = json.loads(
        (session_dirs[0] / "session.json").read_text(encoding="utf-8")
    )
    assert failed["status"] == "failed"
    assert failed["pauseReason"] == (
        "launch_evidence_attachment_failed"
    )
    with pytest.raises(APIError, match="attachment is incomplete"):
        app.resume_run(plan.plan_id, failed["sessionId"])


def test_commander_api_acceptance_run_exposes_completed_dag_and_review(
    tmp_path: Path,
) -> None:
    recorder = _PopenRecorder()
    app = _app(tmp_path, recorder)
    created = app.create_commander_request(
        "Build and verify a local result",
        [],
        None,
    )
    request_id = created["request"]["requestId"]
    claim = app.commander.claim_plan(request_id)
    app.commander.import_plan(
        request_id,
        tmp_path / "plan.json",
        claim_id=claim["claimId"],
    )
    request = app.commander.request_detail(request_id)["request"]
    launched = app.approve_commander_run(
        request_id,
        request["planDigest"],
        2,
    )
    session_dir = (
        app.artifacts_dir
        / launched["run"]["planId"]
        / launched["run"]["sessionId"]
    )
    plan = load_plan(session_dir / "plan.snapshot.json", app.config)
    execute_plan(
        app.config,
        plan,
        session_dir=session_dir,
        max_repair=2,
        backend=_FakeBackend([
            ["def result():\n    return 1\n"],
            ["assert result() == 1", '{"verdict":"approve"}'],
        ]),
    )

    detail = app.run_detail(plan.plan_id, launched["run"]["sessionId"])
    assert detail["run"]["completed"] == 3
    assert detail["run"]["status"] == "completed"
    assert detail["tasks"]["implement"]["normalizedOutput"].startswith("def")
    assert detail["tasks"]["test"]["normalizedOutput"].startswith("assert")
    assert detail["frontierResult"]["schemaVersion"] == 2
    assert detail["frontierResult"]["requiresFrontierReview"] is True
    assert detail["actions"]["review"] is True

    review_claim = app.commander.claim_review(session_dir)
    review = tmp_path / "acceptance-review.json"
    review.write_text(
        json.dumps({
            "schemaVersion": 1,
            "sessionId": launched["run"]["sessionId"],
            "planId": plan.plan_id,
            "verdict": "approved",
            "summary": "All completed artifacts satisfy the packet.",
            "findings": [],
        }),
        encoding="utf-8",
    )
    app.commander.import_review(
        session_dir,
        review,
        claim_id=review_claim["claimId"],
    )
    reviewed = app.run_detail(plan.plan_id, launched["run"]["sessionId"])
    assert reviewed["reviewStatus"] == "approved"
    assert reviewed["frontierReview"]["verdict"] == "approved"
    assert reviewed["actions"]["review"] is False


def test_resume_preserves_completed_tasks(tmp_path: Path) -> None:
    recorder = _PopenRecorder()
    app = _app(tmp_path, recorder)
    plan = load_plan(tmp_path / "plan.json", app.config)
    run_dir = app.artifacts_dir / plan.plan_id / "20260727T120000Z-aabbccdd"
    session = Session(run_dir, plan, session_id=run_dir.name)
    session.set_sources(
        config_source=app.config.source,
        plan_source=plan.source,
    )
    session.update_task(
        "implement",
        status="completed",
        output="def result(): return 1",
        normalizedOutput="def result(): return 1",
    )
    session.state["maxRepair"] = 4
    session._save()
    app.resume_run(plan.plan_id, session.session_id)

    reloaded = json.loads((run_dir / "session.json").read_text())
    assert reloaded["tasks"]["implement"]["status"] == "completed"
    argv, kwargs = recorder.calls[0]
    assert "resume" in argv
    assert str(run_dir) in argv
    assert argv[-2:] == ["--max-repair", "4"]
    assert kwargs["shell"] is False


def test_retry_creates_linked_immutable_run(tmp_path: Path) -> None:
    recorder = _PopenRecorder()
    app = _app(tmp_path, recorder)
    plan = load_plan(tmp_path / "plan.json", app.config)
    old_dir = app.artifacts_dir / plan.plan_id / "20260727T120000Z-aabbccdd"
    old = Session(old_dir, plan, session_id=old_dir.name)
    old.set_sources(
        config_source=app.config.source,
        plan_source=plan.source,
    )
    old.update_task("implement", status="failed", error="backend stopped")
    old.set_status("partial")
    old_state_before = (old_dir / "session.json").read_bytes()

    detail = app.retry_run(plan.plan_id, old.session_id, 1)
    new_id = detail["run"]["sessionId"]
    new_dir = app.artifacts_dir / plan.plan_id / new_id
    new_state = json.loads((new_dir / "session.json").read_text())

    assert new_id != old.session_id
    assert new_state["retryOf"] == f"{plan.plan_id}/{old.session_id}"
    assert new_state["tasks"]["implement"]["status"] == "pending"
    assert (old_dir / "session.json").read_bytes() == old_state_before
    assert recorder.calls[0][0][-2:] == ["--max-repair", "1"]


def test_run_detail_serializes_outputs_gates_batches_and_usage(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    plan = load_plan(tmp_path / "plan.json", app.config)
    run_dir = app.artifacts_dir / plan.plan_id / "20260727T120000Z-aabbccdd"
    session = Session(run_dir, plan, session_id=run_dir.name)
    session.set_sources(
        config_source=app.config.source,
        plan_source=plan.source,
    )
    session.update_task(
        "implement",
        status="completed",
        output="```python\ndef result(): return 1\n```",
        normalizedOutput="def result(): return 1",
        repairAttempts=1,
        batchIndex=0,
        gateResult={
            "configured": True,
            "passed": True,
            "violations": [],
            "normalizations": ["single-code-fence"],
        },
    )
    session.update_task("test", status="rejected")
    session.update_task("review", status="blocked", blockedBy=["test"])
    session.add_batch_record({
        "levelIndex": 0,
        "chunkIndex": 0,
        "phase": "generation",
        "taskIds": ["implement"],
        "statistics": {
            "batchSize": 1,
            "promptTokens": 20,
            "generationTokens": 7,
            "generationSeconds": 0.5,
            "loadSeconds": 1.0,
            "groups": [{"size": 1}],
        },
        "repairs": [{
            "round": 1,
            "taskIds": ["implement"],
            "statistics": {
                "batchSize": 1,
                "promptTokens": 10,
                "generationTokens": 3,
                "loadSeconds": 0,
                "groups": [{"size": 1}],
            },
        }],
    })
    session.set_status("partial")
    frontier = session.write_frontier_result()
    session.state["frontierResult"] = str(frontier)
    session._save()

    detail = app.run_detail(plan.plan_id, session.session_id)
    assert detail["levels"] == [["implement"], ["test", "review"]]
    assert detail["tasks"]["implement"]["normalizedOutput"].startswith("def")
    assert detail["tasks"]["implement"]["gateResult"]["normalizations"] == [
        "single-code-fence"
    ]
    assert detail["tasks"]["test"]["status"] == "rejected"
    assert detail["tasks"]["review"]["status"] == "blocked"
    assert detail["localUsage"] == {
        "promptTokens": 30,
        "generationTokens": 10,
        "generationCalls": 2,
        "modelLoads": 1,
    }
    assert detail["frontierResult"]["reviewMode"] == "frontier-final-only"
    assert detail["actions"]["retry"] is True


def test_path_traversal_and_identity_mismatch_are_rejected(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    with pytest.raises(APIError, match="Invalid planId"):
        app.run_detail("..", "session")
    with pytest.raises(APIError, match="Run not found"):
        app.run_detail("cockpit-plan", "missing")


@pytest.fixture
def http_cockpit(tmp_path: Path):
    app = _app(tmp_path)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    except PermissionError:
        pytest.skip("The active sandbox does not allow localhost sockets.")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield app, base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get(url: str) -> tuple[int, bytes, Any]:
    with urlopen(url, timeout=2) as response:
        return response.status, response.read(), response.headers


def _post(
    url: str,
    body: bytes,
    *,
    origin: str | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    if origin:
        headers["Origin"] = origin
    request = Request(url, method="POST", data=body, headers=headers)
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_static_assets_and_api(http_cockpit) -> None:
    _app_instance, base = http_cockpit
    status, content, headers = _get(base + "/")
    assert status == 200
    assert b"MLX Swarm Cockpit" in content
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert _get(base + "/styles.css")[0] == 200
    assert _get(base + "/app.js")[0] == 200
    status_payload = json.loads(_get(base + "/api/status")[1])
    assert status_payload["reviewMode"] == "frontier-final-only"
    plans_payload = json.loads(_get(base + "/api/plans")[1])
    assert plans_payload["plans"][0]["planId"] == "cockpit-plan"
    commander_payload = json.loads(
        _get(base + "/api/commander/requests")[1]
    )
    assert commander_payload == {"requests": []}


def test_http_commander_request_and_unknown_fields(http_cockpit) -> None:
    _app_instance, base = http_cockpit
    status, created = _post(
        base + "/api/commander/requests",
        json.dumps({
            "objective": "Build a local artifact",
            "constraints": ["No network."],
        }).encode(),
        origin=base,
    )
    assert status == 201
    request_id = created["request"]["requestId"]
    detail = json.loads(
        _get(
            f"{base}/api/commander/requests/{request_id}"
        )[1]
    )
    assert detail["request"]["status"] == "awaiting_plan"
    assert "$mlx-swarm-commander" in detail["handoff"]["planCommand"]

    status, payload = _post(
        base + "/api/commander/requests",
        json.dumps({
            "objective": "Build a local artifact",
            "unknown": True,
        }).encode(),
        origin=base,
    )
    assert status == 400
    assert "Unknown fields" in payload["error"]


def test_http_malformed_oversized_and_cross_origin_requests(
    http_cockpit,
) -> None:
    _app_instance, base = http_cockpit
    status, payload = _post(base + "/api/runs", b"{bad")
    assert status == 400
    assert payload["error"] == "Invalid JSON body."

    status, payload = _post(
        base + "/api/runs",
        json.dumps({"planId": "cockpit-plan"}).encode(),
        origin="http://evil.example",
    )
    assert status == 403
    assert "Cross-origin" in payload["error"]

    port = base.rsplit(":", 1)[1]
    status, payload = _post(
        base + "/api/runs",
        json.dumps({"planId": "cockpit-plan"}).encode(),
        origin=f"http://localhost:{port}",
    )
    assert status == 403
    assert "Cross-origin" in payload["error"]

    status, payload = _post(base + "/api/runs", b"x" * 16_385)
    assert status == 413
    assert "too large" in payload["error"]


def test_http_launch_and_retry_lineage(http_cockpit) -> None:
    app, base = http_cockpit
    status, launched = _post(
        base + "/api/runs",
        json.dumps({
            "planId": "cockpit-plan",
            "maxRepair": 3,
        }).encode(),
        origin=base,
    )
    assert status == 202
    assert launched["run"]["launchSource"] == "ui"
    assert launched["run"]["maxRepair"] == 3

    plan = load_plan(app.plans_dir / "plan.json", app.config)
    old_dir = (
        app.artifacts_dir
        / plan.plan_id
        / "20260727T120000Z-retry123"
    )
    old = Session(old_dir, plan, session_id=old_dir.name)
    old.set_sources(
        config_source=app.config.source,
        plan_source=plan.source,
    )
    old.set_status("partial")

    status, retried = _post(
        (
            f"{base}/api/runs/{plan.plan_id}/{old.session_id}/retry"
        ),
        b'{"maxRepair":1}',
        origin=base,
    )
    assert status == 202
    assert retried["run"]["sessionId"] != old.session_id
    assert retried["run"]["retryOf"] == (
        f"{plan.plan_id}/{old.session_id}"
    )


def test_http_path_traversal_and_missing_static_are_rejected(
    http_cockpit,
) -> None:
    _app_instance, base = http_cockpit
    for path, expected in (
        ("/api/runs/%2e%2e/session", 400),
        ("/api/commander/requests/%2e%2e", 400),
        ("/secret.txt", 404),
    ):
        try:
            urlopen(base + path, timeout=2)
        except HTTPError as exc:
            assert exc.code == expected
        else:
            pytest.fail(f"{path} unexpectedly succeeded")
