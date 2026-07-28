"""Tests for Frontier Commander contracts, receipts, and skill packaging."""
# @lat: [[Tests#Commander]]

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from mlx_swarm import commander as commander_module
from mlx_swarm.commander import (
    CommanderError,
    CommanderStore,
    canonical_json_sha256,
    frontier_usage,
)
from mlx_swarm.contracts import load_config
from mlx_swarm.session import Session
from mlx_swarm.skill_install import SkillInstallError, install_bundled_skill


def _workspace(tmp_path: Path):
    model = tmp_path / "model"
    model.mkdir()
    config_path = tmp_path / "mlx-swarm.json"
    config_path.write_text(
        json.dumps({
            "schemaVersion": 1,
            "model": {
                "repository": "local/test",
                "localPath": str(model),
            },
            "batch": {"maxWorkers": 4},
            "artifacts": str(tmp_path / "artifacts"),
        }),
        encoding="utf-8",
    )
    return load_config(config_path)


def _plan(plan_id: str = "commander-plan") -> dict:
    return {
        "schemaVersion": 1,
        "planId": plan_id,
        "objective": "Produce a bounded artifact",
        "context": {
            "objective": "Produce a bounded artifact",
            "authoritativeSources": [
                {"label": "contract", "content": "Return RESULT."}
            ],
            "constraints": ["Stay local."],
            "rejectionCriteria": ["Missing RESULT."],
            "outputProtocol": "Return only the artifact.",
        },
        "tasks": [
            {
                "id": "build",
                "role": "implementation",
                "prompt": "Return RESULT.",
                "gate": {
                    "requiredPatterns": [
                        {"id": "has-result", "pattern": "RESULT"}
                    ],
                    "forbiddenPatterns": [],
                    "maxCharacters": 200,
                },
            }
        ],
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _workspace_v2(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / ".gitignore").write_text(
        "config/.swarm/\n",
        encoding="utf-8",
    )
    (repo / "src").mkdir()
    (repo / "src" / "value.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    config_dir = repo / "config"
    config_dir.mkdir()
    config_path = config_dir / "mlx-swarm.json"
    config_path.write_text(
        json.dumps({
            "schemaVersion": 2,
            "model": {"repository": "local/test"},
            "batch": {"maxWorkers": 4},
            "artifacts": ".swarm/runs",
            "workspace": {
                "writeRoots": ["src"],
                "verificationProfiles": {
                    "unit": {
                        "argv": ["python", "-m", "pytest", "-q"],
                        "cwd": ".",
                        "timeoutSeconds": 30,
                        "inheritEnv": ["PATH"],
                        "environment": {},
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo, load_config(config_path)


def _plan_v2() -> dict:
    return {
        "schemaVersion": 2,
        "planId": "workspace-command",
        "objective": "Change the approved value",
        "tasks": [{
            "id": "change",
            "role": "implementation",
            "prompt": "Return one unified diff.",
            "artifactType": "patch",
            "allowedPaths": ["src/value.py"],
            "verification": ["unit"],
        }],
    }


def _import_plan(
    store: CommanderStore,
    tmp_path: Path,
    *,
    reported_usage: bool = False,
):
    detail = store.create_request(
        "Produce a bounded artifact",
        ["Stay local."],
    )
    request_id = detail["request"]["requestId"]
    claim = store.claim_plan(request_id)
    response = tmp_path / "frontier-plan-response.json"
    response.write_text(
        "```json\n" + json.dumps(_plan()) + "\n```\n",
        encoding="utf-8",
    )
    usage = (
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
        if reported_usage
        else {}
    )
    imported = store.import_plan(
        request_id,
        response,
        claim_id=claim["claimId"],
        **usage,
    )
    return request_id, imported


def test_request_prompt_is_fixed_to_config_workspace(tmp_path: Path) -> None:
    config = _workspace(tmp_path)
    store = CommanderStore(config)
    detail = store.create_request(
        "Build the requested result",
        ["No network.", "Return JSON."],
        revision_of="old-plan/old-session",
        request_id="request-fixed",
    )

    request = detail["request"]
    assert request["workspaceRoot"] == str(config.source.parent.resolve())
    assert request["revisionOf"] == "old-plan/old-session"
    assert request["planPhase"] == {"status": "open"}
    prompt = Path(detail["planPrompt"]).read_text(encoding="utf-8")
    assert "Return JSON." in prompt
    assert str(config.source.parent.resolve()) in prompt
    assert "Return JSON only" in prompt

    with pytest.raises(CommanderError, match="already exists"):
        store.create_request("again", request_id="request-fixed")


def test_workspace_commander_emits_typed_plan_and_binds_execution_digest(
    tmp_path: Path,
) -> None:
    repo, config = _workspace_v2(tmp_path)
    store = CommanderStore(config)
    created = store.create_request(
        "Change the approved value",
        ["Do not provide worker commands."],
    )
    request_id = created["request"]["requestId"]
    assert created["request"]["workspaceRoot"] == str(repo.resolve())
    prompt = Path(created["planPrompt"]).read_text(encoding="utf-8")
    assert "schemaVersion must be 2" in prompt
    assert "artifactType" in prompt
    assert "workerOutputProtocol" in prompt
    assert "edit-manifest-v1" in prompt
    assert "verification may contain only these profile IDs: unit" in prompt
    assert "workers never receive or produce command arrays" in prompt

    claim = store.claim_plan(request_id)
    response = tmp_path / "workspace-response.json"
    response.write_text(json.dumps(_plan_v2()), encoding="utf-8")
    imported = store.import_plan(
        request_id,
        response,
        claim_id=claim["claimId"],
    )
    preview = imported["executionPreview"]
    assert preview["workspaceRoot"] == str(repo.resolve())
    assert preview["planSha256"] == imported["request"]["planDigest"]

    with pytest.raises(CommanderError, match="Execution digest mismatch"):
        store.approved_plan(
            request_id,
            imported["request"]["planDigest"],
            execution_digest="0" * 64,
        )
    plan, _path, approval, _receipt, _request = store.approved_plan(
        request_id,
        imported["request"]["planDigest"],
        execution_digest=preview["executionDigest"],
    )
    assert plan.schema_version == 2
    assert approval.execution_digest == preview["executionDigest"]
    assert approval.workspace_root == str(repo.resolve())
    assert approval.base_sha == preview["baseSha"]


def test_plan_claim_import_digest_and_immutable_slot(tmp_path: Path) -> None:
    store = CommanderStore(_workspace(tmp_path))
    request_id, detail = _import_plan(store, tmp_path, reported_usage=True)

    request = detail["request"]
    assert request["status"] == "plan_ready"
    assert request["planDigest"] == canonical_json_sha256(_plan())
    assert detail["plan"]["levels"] == [["build"]]
    assert detail["planningReceipt"]["usage"] == {
        "usageStatus": "reported",
        "promptTokens": 100,
        "completionTokens": 50,
        "totalTokens": 150,
    }

    with pytest.raises(CommanderError, match="already"):
        store.claim_plan(request_id)


def test_invalid_plan_is_recorded_and_request_is_sealed(tmp_path: Path) -> None:
    store = CommanderStore(_workspace(tmp_path))
    request_id = store.create_request("Make a plan")["request"]["requestId"]
    claim = store.claim_plan(request_id)
    response = tmp_path / "invalid-plan.json"
    response.write_text('{"schemaVersion": 1, "bad": true}', encoding="utf-8")

    with pytest.raises(CommanderError, match="sealed"):
        store.import_plan(
            request_id,
            response,
            claim_id=claim["claimId"],
        )

    detail = store.request_detail(request_id)
    assert detail["request"]["status"] == "plan_invalid"
    assert detail["validationError"]["phase"] == "plan"
    assert detail["plan"] is None
    with pytest.raises(CommanderError, match="already"):
        store.import_plan(
            request_id,
            response,
            claim_id=claim["claimId"],
        )


def test_claims_are_exclusive_and_releasable_before_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[bool, dict]] = []
    real_link = commander_module.os.link

    def inspect_publish(source: str, target: str) -> None:
        published.append((
            Path(target).exists(),
            json.loads(Path(source).read_text(encoding="utf-8")),
        ))
        real_link(source, target)

    monkeypatch.setattr(commander_module.os, "link", inspect_publish)
    store = CommanderStore(_workspace(tmp_path))
    request_id = store.create_request("Make a plan")["request"]["requestId"]
    claim = store.claim_plan(request_id)
    with pytest.raises(CommanderError, match="already claimed"):
        store.claim_plan(request_id)
    store.release_plan_claim(request_id, claim["claimId"])
    second = store.claim_plan(request_id)
    assert second["claimId"] != claim["claimId"]
    assert published[0][0] is False
    assert all(value["phase"] == "plan" for _, value in published)


def test_usage_is_never_partially_reported_or_estimated() -> None:
    assert frontier_usage().to_json() == {
        "usageStatus": "unavailable",
        "promptTokens": None,
        "completionTokens": None,
        "totalTokens": None,
    }
    with pytest.raises(CommanderError, match="must all"):
        frontier_usage(prompt_tokens=1)
    with pytest.raises(CommanderError, match="must equal"):
        frontier_usage(
            prompt_tokens=1,
            completion_tokens=2,
            total_tokens=4,
        )


def test_digest_bound_approval_and_completed_review_flow(
    tmp_path: Path,
) -> None:
    config = _workspace(tmp_path)
    store = CommanderStore(config)
    request_id, detail = _import_plan(store, tmp_path)
    digest = detail["request"]["planDigest"]

    with pytest.raises(CommanderError, match="digest mismatch"):
        store.approved_plan(request_id, "0" * 64)

    plan, plan_path, approval, receipt, request = store.approved_plan(
        request_id,
        digest,
    )
    session_dir = config.artifacts_dir / plan.plan_id / "session-one"
    session = Session(session_dir, plan, session_id="session-one")
    session.set_sources(config_source=config.source, plan_source=plan_path)
    session.attach_commander(
        request_id=request_id,
        approval=approval.to_json(),
        planning_receipt=receipt,
        revision_of=request.get("revisionOf"),
    )
    session.update_task(
        "build",
        status="completed",
        output="RESULT",
        normalizedOutput="RESULT",
        gateResult={
            "configured": True,
            "passed": True,
            "violations": [],
            "normalizations": [],
        },
    )
    session.set_status("completed")
    result_path = session.write_frontier_result()
    session.state["frontierResult"] = str(result_path)
    session._save()

    packet = json.loads(result_path.read_text(encoding="utf-8"))
    assert packet["schemaVersion"] == 2
    assert packet["requiresFrontierReview"] is True
    assert packet["planContract"] == _plan()
    assert packet["tasks"]["build"]["gateResult"]["passed"] is True
    assert packet["localUsage"]["generationCalls"] == 0

    claim = store.claim_review(session_dir)
    review_response = tmp_path / "review.json"
    review_response.write_text(
        json.dumps({
            "schemaVersion": 1,
            "sessionId": "session-one",
            "planId": "commander-plan",
            "verdict": "changes_requested",
            "summary": "The result exists but needs a follow-up.",
            "findings": [{
                "id": "clarify-result",
                "severity": "medium",
                "taskId": "build",
                "title": "Clarify result",
                "evidence": "The packet contains RESULT.",
                "recommendation": "Create a separately approved revision.",
            }],
        }),
        encoding="utf-8",
    )
    reviewed = store.import_review(
        session_dir,
        review_response,
        claim_id=claim["claimId"],
        prompt_tokens=30,
        completion_tokens=20,
        total_tokens=50,
    )

    assert reviewed["review"]["verdict"] == "changes_requested"
    assert reviewed["frontierUsage"]["planning"]["usageStatus"] == "unavailable"
    assert reviewed["frontierUsage"]["review"]["totalTokens"] == 50
    assert reviewed["frontierUsage"]["total"]["usageStatus"] == "unavailable"
    state = json.loads((session_dir / "session.json").read_text())
    assert state["status"] == "completed"
    assert state["reviewStatus"] == "changes_requested"
    with pytest.raises(CommanderError, match="already"):
        store.claim_review(session_dir)


def test_partial_session_is_not_review_eligible(tmp_path: Path) -> None:
    config = _workspace(tmp_path)
    store = CommanderStore(config)
    _request_id, detail = _import_plan(store, tmp_path)
    plan, plan_path, _approval, _receipt, _request = store.approved_plan(
        detail["request"]["requestId"],
        detail["request"]["planDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / "session-partial"
    session = Session(session_dir, plan, session_id="session-partial")
    session.set_sources(config_source=config.source, plan_source=plan_path)
    session.set_status("partial")
    session.write_frontier_result()

    with pytest.raises(CommanderError, match="Only completed"):
        store.claim_review(session_dir)


def test_invalid_review_is_recorded_and_seals_phase(tmp_path: Path) -> None:
    config = _workspace(tmp_path)
    store = CommanderStore(config)
    request_id, detail = _import_plan(store, tmp_path)
    plan, plan_path, approval, receipt, _request = store.approved_plan(
        request_id,
        detail["request"]["planDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / "session-invalid-review"
    session = Session(
        session_dir,
        plan,
        session_id="session-invalid-review",
    )
    session.set_sources(config_source=config.source, plan_source=plan_path)
    session.attach_commander(
        request_id=request_id,
        approval=approval.to_json(),
        planning_receipt=receipt,
    )
    session.update_task(
        "build",
        status="completed",
        output="RESULT",
        normalizedOutput="RESULT",
    )
    session.set_status("completed")
    session.write_frontier_result()
    claim = store.claim_review(session_dir)
    bad = tmp_path / "bad-review.json"
    bad.write_text('{"verdict":"approved"}', encoding="utf-8")

    with pytest.raises(CommanderError, match="sealed"):
        store.import_review(
            session_dir,
            bad,
            claim_id=claim["claimId"],
        )
    status = store.review_detail(session_dir)
    assert status["reviewStatus"] == "review_error"
    assert status["reviewError"]["phase"] == "review"
    with pytest.raises(CommanderError, match="sealed"):
        store.claim_review(session_dir)


def test_review_path_and_identity_are_confined_to_artifacts(
    tmp_path: Path,
) -> None:
    store = CommanderStore(_workspace(tmp_path))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "session.json").write_text(
        '{"planId":"x","sessionId":"y","status":"completed"}',
        encoding="utf-8",
    )
    with pytest.raises(CommanderError, match="escapes"):
        store.claim_review(outside)


def test_bundled_skill_installs_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    installed = install_bundled_skill(skills_dir=skills_dir)
    assert installed.name == "mlx-swarm-commander"
    assert "name: mlx-swarm-commander" in (
        installed / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert (installed / "agents" / "openai.yaml").is_file()
    with pytest.raises(SkillInstallError, match="already exists"):
        install_bundled_skill(skills_dir=skills_dir)
    assert install_bundled_skill(
        skills_dir=skills_dir,
        force=True,
    ) == installed


def test_legacy_namespace_resolves_to_canonical_modules() -> None:
    with pytest.warns(DeprecationWarning, match="deprecated"):
        legacy = importlib.import_module("swarm_agents.contracts")
    canonical = importlib.import_module("mlx_swarm.contracts")
    assert legacy is canonical
