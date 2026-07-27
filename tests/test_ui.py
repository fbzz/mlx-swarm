"""Tests for the localhost work cockpit server and API."""
# @lat: [[Tests#UI]]

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from swarm_agents.contracts import load_config, load_plan
from swarm_agents.session import Session
from swarm_agents.ui import APIError, CockpitApp, make_handler


def test_packaged_styles_preserve_native_hidden_state() -> None:
    styles = (
        Path(__file__).parents[1]
        / "src"
        / "swarm_agents"
        / "ui_static"
        / "styles.css"
    ).read_text(encoding="utf-8")

    assert "[hidden] { display: none !important; }" in styles


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


def _app(tmp_path: Path, recorder: _PopenRecorder | None = None) -> CockpitApp:
    config_path, _ = _write_workspace(tmp_path)
    return CockpitApp(
        load_config(config_path),
        tmp_path,
        popen_factory=recorder or _PopenRecorder(),
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
    assert b"Swarm Work Cockpit" in content
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert _get(base + "/styles.css")[0] == 200
    assert _get(base + "/app.js")[0] == 200
    status_payload = json.loads(_get(base + "/api/status")[1])
    assert status_payload["reviewMode"] == "frontier-final-only"
    plans_payload = json.loads(_get(base + "/api/plans")[1])
    assert plans_payload["plans"][0]["planId"] == "cockpit-plan"


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
        ("/secret.txt", 404),
    ):
        try:
            urlopen(base + path, timeout=2)
        except HTTPError as exc:
            assert exc.code == expected
        else:
            pytest.fail(f"{path} unexpectedly succeeded")
