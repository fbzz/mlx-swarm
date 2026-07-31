"""Tests for Frontier Commander contracts, receipts, and skill packaging."""
# @lat: [[Tests#Commander]]

from __future__ import annotations

import importlib
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from mlx_swarm import commander as commander_module
from mlx_swarm.commander import (
    CommanderError,
    CommanderStore,
    build_plan_prompt,
    build_review_input,
    canonical_json_sha256,
    frontier_usage,
)
from mlx_swarm.contracts import load_config, load_plan
from mlx_swarm.session import Session
from mlx_swarm.skill_install import SkillInstallError, install_bundled_skill
from mlx_swarm.workspace import (
    checkout_lease,
    execution_preview,
    prepare_workspace,
)


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
            "diagnosis": {
                "observedFailure": "The requested RESULT artifact is absent.",
                "causalHypothesis": (
                    "No bounded implementation task has produced RESULT."
                ),
                "validationMethod": "source-trace",
                "validationEvidence": (
                    "The authoritative contract explicitly requires RESULT "
                    "and the task output is the only production surface."
                ),
                "falsificationCondition": (
                    "A source path outside the task output already produces "
                    "the required RESULT."
                ),
                "evidenceSources": ["contract"],
                "changeValidation": {
                    "candidateChange": (
                        "The build task emits the missing RESULT artifact."
                    ),
                    "failingPathPrediction": (
                        "The previously absent task output becomes RESULT."
                    ),
                    "preservedControlPrediction": (
                        "The surrounding persistence path remains unchanged."
                    ),
                    "minimalityEvidence": (
                        "Only the missing task output is introduced."
                    ),
                    "evidenceSources": ["contract"],
                },
            },
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


def test_plan_prompt_exposes_worker_capability_and_delegation_boundary(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "mlx-swarm.json"
    config_path.write_text(json.dumps({
        "schemaVersion": 1,
        "model": {
            "repository": "local/four-billion",
            "revision": "quantized-6bit",
        },
        "batch": {
            "maxWorkers": 4,
            "maxPromptCharacters": 120000,
        },
        "artifacts": ".swarm/runs",
        "worker": {
            "mode": "direct",
            "reasoningMaxTokens": 512,
            "capabilities": {
                "parameterScale": "4B",
                "contextWindowTokens": 262144,
                "maxGenerationTokens": 800,
                "specialization": "general",
                "delegationLevel": "exact-edit",
                "strengths": ["Renders small exact edits."],
                "limitations": ["Does not reliably diagnose unfamiliar code."],
                "calibration": {
                    "status": "failed",
                    "passedCases": 0,
                    "totalCases": 2,
                    "evidenceSha256": "b" * 64,
                },
            },
        },
    }), encoding="utf-8")
    prompt = build_plan_prompt({
        "objective": "Repair the demonstrated defect.",
        "constraints": [],
        "workspaceRoot": str(tmp_path),
    }, load_config(config_path))

    assert "WORKER CAPABILITY CONTRACT" in prompt
    assert "parameter scale: 4B" in prompt
    assert "model context window: 262144 tokens" in prompt
    assert "maximum generation per agent: 800 tokens" in prompt
    assert "calibration: failed (0/2 passed" in prompt
    assert "Do not delegate discovery" in prompt
    assert "This describes local generation capability, not agent concurrency" in prompt
    assert "CANDIDATE CHANGE SPECIFICITY GATE" in prompt
    assert "preservedControlPrediction" in prompt
    assert "narrowest distinguishing property" in prompt
    assert '"integrationVerification"' not in prompt


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
        "context": {
            "objective": "Change the approved value",
            "authoritativeSources": [{
                "label": "workspace-source",
                "content": "src/value.py currently contains VALUE = 1.",
            }],
            "constraints": ["Modify only src/value.py."],
            "rejectionCriteria": ["VALUE remains 1."],
            "outputProtocol": "Return an edit manifest.",
            "diagnosis": {
                "observedFailure": "The approved value is still 1.",
                "causalHypothesis": (
                    "src/value.py defines the stale value directly."
                ),
                "validationMethod": "source-trace",
                "validationEvidence": (
                    "The authoritative source shows VALUE = 1 in "
                    "src/value.py."
                ),
                "falsificationCondition": (
                    "The runtime value is supplied by another authoritative "
                    "source."
                ),
                "evidenceSources": ["workspace-source"],
                "changeValidation": {
                    "candidateChange": "Replace VALUE = 1 with VALUE = 2.",
                    "failingPathPrediction": (
                        "The direct module value becomes the requested 2."
                    ),
                    "preservedControlPrediction": (
                        "No file or symbol other than VALUE changes."
                    ),
                    "minimalityEvidence": (
                        "The exact defining assignment is the narrowest target."
                    ),
                    "evidenceSources": ["workspace-source"],
                },
            },
        },
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


def test_claude_claim_adapter_is_inherited_by_plan_receipt(
    tmp_path: Path,
) -> None:
    store = CommanderStore(_workspace(tmp_path))
    detail = store.create_request("Produce a bounded artifact")
    request_id = detail["request"]["requestId"]
    claim = store.claim_plan(
        request_id,
        adapter="claude-code-skill",
    )
    response = tmp_path / "claude-plan.json"
    response.write_text(json.dumps(_plan()), encoding="utf-8")

    with pytest.raises(CommanderError, match="sealed by the claim"):
        store.import_plan(
            request_id,
            response,
            claim_id=claim["claimId"],
            adapter="codex-skill",
        )

    imported = store.import_plan(
        request_id,
        response,
        claim_id=claim["claimId"],
    )

    assert imported["planningReceipt"]["adapter"] == "claude-code-skill"


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


def test_linked_revision_supersedes_safe_predecessor_checkout_lease(
    tmp_path: Path,
) -> None:
    repo, config = _workspace_v2(tmp_path)
    plan_path = repo / "config" / "predecessor.json"
    plan_path.write_text(json.dumps(_plan_v2()), encoding="utf-8")
    _git(repo, "add", "config/predecessor.json")
    _git(repo, "commit", "-qm", "add predecessor plan")
    plan = load_plan(plan_path, config)
    preview = execution_preview(
        config,
        plan,
        approval_mode="yolo",
        workspace_target="checkout",
    )
    session_id = "predecessor"
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
    session.set_sources(config_source=config.source, plan_source=plan.source)
    session.attach_workspace(
        snapshot,
        execution_approval={
            "planSha256": preview["planSha256"],
            "executionDigest": preview["executionDigest"],
            "workspaceRoot": preview["workspaceRoot"],
            "baseSha": preview["baseSha"],
            "approvalMode": "yolo",
            "workspaceTarget": "checkout",
            "executionPolicySha256": preview["executionPolicySha256"],
        },
    )
    session.update_task(
        "change",
        status="rejected",
        error="Superseded before application.",
    )
    assert checkout_lease(repo) is not None

    store = CommanderStore(config)
    detail = store.create_request(
        "Create the corrected linked plan",
        revision_of=f"{plan.plan_id}/{session_id}",
        request_id="successor",
    )

    assert detail["request"]["revisionOf"] == (
        f"{plan.plan_id}/{session_id}"
    )
    assert checkout_lease(repo) is None
    predecessor = json.loads(
        (session_dir / "session.json").read_text(encoding="utf-8")
    )
    assert predecessor["supersededByRequestId"] == "successor"
    assert predecessor["supersessionLeaseStatus"] == "released"
    assert predecessor["checkoutLeaseReleaseReason"] == "superseded"


def test_commander_rejects_unvalidated_causal_hypothesis(
    tmp_path: Path,
) -> None:
    store = CommanderStore(_workspace(tmp_path))
    created = store.create_request("Produce a bounded artifact")
    request_id = created["request"]["requestId"]
    claim = store.claim_plan(request_id)
    response = tmp_path / "missing-diagnosis.json"
    plan = _plan()
    del plan["context"]["diagnosis"]
    response.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(CommanderError, match="context.diagnosis"):
        store.import_plan(
            request_id,
            response,
            claim_id=claim["claimId"],
            prompt_tokens=40,
            completion_tokens=10,
            total_tokens=50,
        )
    detail = store.request_detail(request_id)
    assert detail["request"]["status"] == "awaiting_plan"
    assert detail["request"]["planPhase"]["importAttempts"] == 1


def test_commander_rejects_diagnosis_without_candidate_change_validation(
    tmp_path: Path,
) -> None:
    store = CommanderStore(_workspace(tmp_path))
    created = store.create_request("Produce a bounded artifact")
    request_id = created["request"]["requestId"]
    claim = store.claim_plan(request_id)
    response = tmp_path / "missing-change-validation.json"
    plan = _plan()
    del plan["context"]["diagnosis"]["changeValidation"]
    response.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(CommanderError, match="changeValidation"):
        store.import_plan(
            request_id,
            response,
            claim_id=claim["claimId"],
            prompt_tokens=30,
            completion_tokens=20,
            total_tokens=50,
        )


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
    assert "schemaVersion must be 3" in prompt
    assert "artifactType" in prompt
    assert "workerOutputProtocol" in prompt
    assert "edit-manifest-v1" in prompt
    assert "verification may contain only these profile IDs: unit" in prompt
    assert "workers never receive or produce command arrays" in prompt
    assert "target 350 to 700 expected output" in prompt
    assert "most 1024" in prompt
    assert "deterministic-edit" in prompt
    assert "contextRefs" in prompt
    assert "pairwise disjoint" in prompt
    assert "maxRepairAttempts 1 for local-agent tasks" in prompt
    assert "wide and shallow" in prompt
    assert "propagates failure" in prompt
    assert "gate.maxCharacters must cover the full expected artifact" in prompt
    assert "3.5 characters per" in prompt
    assert '"maxCharacters": 3500' in prompt
    assert "20000" not in prompt
    assert "aggregate rendered prompt budget per physical batch: 49152" in prompt
    assert "never divide it into fixed per-agent" in prompt
    assert "For review tasks, normally set max_tokens to at most 768" in prompt
    assert "For report tasks, normally set max_tokens to at most 1536" in prompt

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
    assert approval.approval_mode == "supervised"
    assert approval.workspace_target == "worktree"
    assert approval.execution_policy_sha256 == preview[
        "executionPolicySha256"
    ]
    store.mark_launched(
        request_id,
        approval,
        plan_id=plan.plan_id,
        session_id="round-trip",
    )
    launched = store.request_detail(request_id)
    assert launched["request"]["approval"]["approvalMode"] == "supervised"
    assert launched["request"]["approval"]["workspaceTarget"] == "worktree"


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


def test_invalid_plan_seals_only_after_bounded_reimports(
    tmp_path: Path,
) -> None:
    store = CommanderStore(_workspace(tmp_path))
    request_id = store.create_request("Make a plan")["request"]["requestId"]
    claim = store.claim_plan(request_id)
    request_dir = Path(
        store.request_detail(request_id)["planPrompt"]
    ).parent

    for attempt in (1, 2):
        response = tmp_path / f"invalid-plan-{attempt}.json"
        response.write_text(
            json.dumps({"schemaVersion": 1, "bad": attempt}),
            encoding="utf-8",
        )
        with pytest.raises(
            CommanderError,
            match=rf"attempt {attempt} is invalid; {3 - attempt} corrected",
        ):
            store.import_plan(
                request_id,
                response,
                claim_id=claim["claimId"],
                prompt_tokens=30,
                completion_tokens=20,
                total_tokens=50,
            )
        detail = store.request_detail(request_id)
        assert detail["request"]["status"] == "awaiting_plan"
        assert detail["request"]["planPhase"]["importAttempts"] == attempt
        assert detail["validationError"]["attempt"] == attempt
        assert detail["validationError"]["attemptsRemaining"] == 3 - attempt

    final = tmp_path / "invalid-plan-3.json"
    final.write_text(
        json.dumps({"schemaVersion": 1, "bad": 3}),
        encoding="utf-8",
    )
    with pytest.raises(CommanderError, match="sealed"):
        store.import_plan(
            request_id,
            final,
            claim_id=claim["claimId"],
            prompt_tokens=30,
            completion_tokens=20,
            total_tokens=50,
        )

    detail = store.request_detail(request_id)
    assert detail["request"]["status"] == "plan_invalid"
    assert detail["request"]["planPhase"]["importAttempts"] == 3
    assert detail["validationError"]["phase"] == "plan"
    assert detail["validationError"]["attemptsRemaining"] == 0
    assert detail["plan"] is None
    assert detail["planningAttemptReceipt"]["acceptedResponses"] == 0
    assert detail["planningAttemptReceipt"]["attemptedResponses"] == 1
    assert detail["planningAttemptReceipt"]["usage"]["totalTokens"] == 50
    assert (request_dir / "frontier-plan.raw.txt").is_file()
    assert (request_dir / "frontier-plan.raw.attempt-2.txt").is_file()
    assert (request_dir / "frontier-plan.raw.attempt-3.txt").is_file()
    for attempt in (1, 2, 3):
        assert (
            request_dir
            / f"frontier-plan-attempt-{attempt}-receipt.json"
        ).is_file()
    raw_response = (
        request_dir / "frontier-plan.raw.attempt-3.txt"
    ).read_bytes()
    assert detail["planningAttemptReceipt"]["responseSha256"] == (
        hashlib.sha256(raw_response).hexdigest()
    )
    with pytest.raises(CommanderError, match="already"):
        store.import_plan(
            request_id,
            final,
            claim_id=claim["claimId"],
        )


def test_corrected_reimport_accepts_plan_on_same_claim(
    tmp_path: Path,
) -> None:
    store = CommanderStore(_workspace(tmp_path))
    request_id = store.create_request("Make a plan")["request"]["requestId"]
    claim = store.claim_plan(request_id)
    invalid = tmp_path / "invalid-first.json"
    invalid.write_text(
        '{"schemaVersion": 1, "bad": true}',
        encoding="utf-8",
    )
    with pytest.raises(CommanderError, match="corrected re-import"):
        store.import_plan(
            request_id,
            invalid,
            claim_id=claim["claimId"],
        )

    corrected = tmp_path / "corrected.json"
    corrected.write_text(json.dumps(_plan()), encoding="utf-8")
    imported = store.import_plan(
        request_id,
        corrected,
        claim_id=claim["claimId"],
    )

    assert imported["request"]["status"] == "plan_ready"
    assert imported["request"]["planPhase"]["importAttempts"] == 2
    assert imported["planningReceipt"] is not None
    # The stale validation error must not surface on the accepted plan.
    assert imported["validationError"] is None


def test_replayed_invalid_response_does_not_spend_an_attempt(
    tmp_path: Path,
) -> None:
    store = CommanderStore(_workspace(tmp_path))
    request_id = store.create_request("Make a plan")["request"]["requestId"]
    claim = store.claim_plan(request_id)
    invalid = tmp_path / "invalid-replay.json"
    invalid.write_text(
        '{"schemaVersion": 1, "bad": true}',
        encoding="utf-8",
    )
    with pytest.raises(CommanderError, match="attempt 1 is invalid"):
        store.import_plan(
            request_id,
            invalid,
            claim_id=claim["claimId"],
        )

    with pytest.raises(CommanderError, match="attempt 1 is invalid"):
        store.import_plan(
            request_id,
            invalid,
            claim_id=claim["claimId"],
        )

    detail = store.request_detail(request_id)
    assert detail["request"]["status"] == "awaiting_plan"
    assert detail["request"]["planPhase"]["importAttempts"] == 1


def test_replayed_earlier_invalid_response_does_not_spend_an_attempt(
    tmp_path: Path,
) -> None:
    store = CommanderStore(_workspace(tmp_path))
    request_id = store.create_request("Make a plan")["request"]["requestId"]
    claim = store.claim_plan(request_id)
    first = tmp_path / "invalid-first.json"
    first.write_text('{"schemaVersion": 1, "bad": 1}', encoding="utf-8")
    second = tmp_path / "invalid-second.json"
    second.write_text('{"schemaVersion": 1, "bad": 2}', encoding="utf-8")
    with pytest.raises(CommanderError, match="attempt 1 is invalid"):
        store.import_plan(request_id, first, claim_id=claim["claimId"])
    with pytest.raises(CommanderError, match="attempt 2 is invalid"):
        store.import_plan(request_id, second, claim_id=claim["claimId"])

    with pytest.raises(
        CommanderError,
        match="already rejected as import attempt 1",
    ):
        store.import_plan(request_id, first, claim_id=claim["claimId"])

    detail = store.request_detail(request_id)
    assert detail["request"]["status"] == "awaiting_plan"
    assert detail["request"]["planPhase"]["importAttempts"] == 2


def test_invalid_plan_reports_every_validation_error(
    tmp_path: Path,
) -> None:
    store = CommanderStore(_workspace(tmp_path))
    request_id = store.create_request("Make a plan")["request"]["requestId"]
    claim = store.claim_plan(request_id)
    plan = _plan()
    plan["tasks"] = [
        dict(plan["tasks"][0], role="unknown-role"),
        dict(plan["tasks"][0], id="second", maxRepairAttempts=99),
    ]
    response = tmp_path / "two-error-plan.json"
    response.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(CommanderError) as exc_info:
        store.import_plan(
            request_id,
            response,
            claim_id=claim["claimId"],
        )

    message = str(exc_info.value)
    assert "role must be one of" in message
    assert "maxRepairAttempts" in message
    errors = store.request_detail(request_id)["validationError"]["errors"]
    assert len(errors) == 2


def test_unreadable_plan_response_still_records_spent_usage(
    tmp_path: Path,
) -> None:
    store = CommanderStore(_workspace(tmp_path))
    request_id = store.create_request("Make a plan")["request"]["requestId"]
    claim = store.claim_plan(request_id)

    with pytest.raises(CommanderError, match="No import attempt was spent"):
        store.import_plan(
            request_id,
            tmp_path / "missing-response.json",
            claim_id=claim["claimId"],
            prompt_tokens=18,
            completion_tokens=7,
            total_tokens=25,
        )

    detail = store.request_detail(request_id)
    assert detail["request"]["status"] == "awaiting_plan"
    assert detail["request"]["planPhase"].get("importAttempts", 0) == 0
    assert detail["planningAttemptReceipt"]["usage"]["totalTokens"] == 25
    request_dir = Path(detail["planPrompt"]).parent
    assert not (request_dir / "frontier-plan.raw.txt").exists()

    # A local read failure leaves the claim releasable.
    store.release_plan_claim(request_id, claim["claimId"])


def test_plan_import_recovers_after_raw_response_was_published(
    tmp_path: Path,
) -> None:
    store = CommanderStore(_workspace(tmp_path))
    detail = store.create_request("Make a plan")
    request_id = detail["request"]["requestId"]
    claim = store.claim_plan(request_id)
    response = tmp_path / "plan-response.json"
    response.write_text(json.dumps(_plan()), encoding="utf-8")
    request_dir = Path(detail["planPrompt"]).parent
    (request_dir / "frontier-plan.raw.txt").write_text(
        response.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    imported = store.import_plan(
        request_id,
        response,
        claim_id=claim["claimId"],
    )

    assert imported["request"]["status"] == "plan_ready"
    assert imported["planningReceipt"]["artifactSha256"] == (
        imported["request"]["planDigest"]
    )
    receipt_before = imported["planningReceipt"]
    request_state = json.loads(
        (request_dir / "request.json").read_text(encoding="utf-8")
    )
    request_state["status"] = "awaiting_plan"
    request_state["planPhase"]["status"] = "claimed"
    (request_dir / "request.json").write_text(
        json.dumps(request_state),
        encoding="utf-8",
    )

    recovered = store.import_plan(
        request_id,
        response,
        claim_id=claim["claimId"],
    )

    assert recovered["request"]["status"] == "plan_ready"
    assert recovered["planningReceipt"] == receipt_before


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
    compact = json.loads(
        (session_dir / "frontier-review-input.json").read_text(
            encoding="utf-8"
        )
    )
    assert packet["schemaVersion"] == 2
    assert packet["requiresFrontierReview"] is True
    assert packet["planContract"] == _plan()
    assert packet["tasks"]["build"]["gateResult"]["passed"] is True
    assert packet["localUsage"]["generationCalls"] == 0
    assert compact == build_review_input(packet)
    assert compact["sourceArtifact"]["sha256"] == (
        canonical_json_sha256(packet)
    )
    assert compact["tasks"][0]["output"] == "RESULT"
    assert "planContract" not in compact
    assert "Return RESULT." not in json.dumps(compact, sort_keys=True)

    claim = store.claim_review(
        session_dir,
        adapter="claude-code-skill",
    )
    assert claim["frontierReviewInput"].endswith(
        "frontier-review-input.json"
    )
    assert claim["inputArtifactSha256"] == canonical_json_sha256(
        compact
    )
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
    with pytest.raises(CommanderError, match="sealed by the claim"):
        store.import_review(
            session_dir,
            review_response,
            claim_id=claim["claimId"],
            adapter="codex-skill",
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
    assert (
        reviewed["receipt"]["inputArtifactSha256"]
        == canonical_json_sha256(compact)
    )
    assert reviewed["receipt"]["adapter"] == "claude-code-skill"
    state = json.loads((session_dir / "session.json").read_text())
    assert state["status"] == "completed"
    assert state["reviewStatus"] == "changes_requested"

    # Recover both crash windows: normalized review durable before its receipt,
    # and receipt durable before the session state transition.
    (session_dir / "frontier-review-receipt.json").unlink()
    (session_dir / "frontier-usage.json").unlink()
    state["reviewStatus"] = "review_claimed"
    state.pop("frontierReviewReceipt", None)
    (session_dir / "session.json").write_text(
        json.dumps(state),
        encoding="utf-8",
    )
    recovered = store.import_review(
        session_dir,
        review_response,
        claim_id=claim["claimId"],
        prompt_tokens=30,
        completion_tokens=20,
        total_tokens=50,
    )
    assert recovered["review"]["verdict"] == "changes_requested"
    state = json.loads((session_dir / "session.json").read_text())
    state["reviewStatus"] = "review_claimed"
    state.pop("frontierReviewReceipt", None)
    (session_dir / "session.json").write_text(
        json.dumps(state),
        encoding="utf-8",
    )
    recovered_again = store.import_review(
        session_dir,
        review_response,
        claim_id=claim["claimId"],
    )
    assert recovered_again["frontierUsage"]["review"]["totalTokens"] == 50
    assert json.loads(
        (session_dir / "session.json").read_text()
    )["reviewStatus"] == "changes_requested"

    with pytest.raises(CommanderError, match="already"):
        store.claim_review(session_dir)


@pytest.mark.parametrize("tampered_artifact", ["compact", "full"])
def test_review_rejects_compact_or_full_packet_tampering_after_claim(
    tmp_path: Path,
    tampered_artifact: str,
) -> None:
    config = _workspace(tmp_path)
    store = CommanderStore(config)
    request_id, detail = _import_plan(store, tmp_path)
    plan, plan_path, approval, receipt, _request = store.approved_plan(
        request_id,
        detail["request"]["planDigest"],
    )
    session_id = f"tamper-{tampered_artifact}"
    session_dir = config.artifacts_dir / plan.plan_id / session_id
    session = Session(session_dir, plan, session_id=session_id)
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

    artifact_path = session_dir / (
        "frontier-review-input.json"
        if tampered_artifact == "compact"
        else "frontier-result.json"
    )
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["objective"] = "tampered after claim"
    artifact_path.write_text(
        json.dumps(artifact),
        encoding="utf-8",
    )
    response = tmp_path / f"{tampered_artifact}-review.json"
    response.write_text(json.dumps({
        "schemaVersion": 1,
        "sessionId": session_id,
        "planId": plan.plan_id,
        "verdict": "approved",
        "summary": "Looks good.",
        "findings": [],
    }), encoding="utf-8")

    with pytest.raises(CommanderError, match="sealed"):
        store.import_review(
            session_dir,
            response,
            claim_id=claim["claimId"],
        )
    assert store.review_detail(session_dir)["reviewStatus"] == (
        "review_error"
    )


def test_legacy_review_prompt_remains_bound_to_full_result(
    tmp_path: Path,
) -> None:
    config = _workspace(tmp_path)
    store = CommanderStore(config)
    request_id, detail = _import_plan(store, tmp_path)
    plan, plan_path, approval, receipt, _request = store.approved_plan(
        request_id,
        detail["request"]["planDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / "legacy-review"
    session = Session(session_dir, plan, session_id="legacy-review")
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
    result_path = session.write_frontier_result()
    result = json.loads(result_path.read_text(encoding="utf-8"))
    (session_dir / "frontier-review-input.json").unlink()
    (session_dir / "frontier-review-prompt.txt").write_text(
        commander_module._build_legacy_review_prompt(result),
        encoding="utf-8",
    )

    claim = store.claim_review(session_dir)

    assert claim["frontierReviewInput"].endswith(
        "frontier-result.json"
    )
    assert claim["inputArtifactSha256"] == canonical_json_sha256(
        result
    )


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
            prompt_tokens=12,
            completion_tokens=8,
            total_tokens=20,
        )
    status = store.review_detail(session_dir)
    assert status["reviewStatus"] == "review_error"
    assert status["reviewError"]["phase"] == "review"
    assert status["reviewAttemptReceipt"]["acceptedResponses"] == 0
    assert status["reviewAttemptReceipt"]["attemptedResponses"] == 1
    assert status["frontierUsage"]["review"]["totalTokens"] == 20
    raw_review = (session_dir / "frontier-review.raw.txt").read_bytes()
    assert status["reviewAttemptReceipt"]["responseSha256"] == (
        hashlib.sha256(raw_review).hexdigest()
    )
    (session_dir / "frontier-review.raw.txt").write_text(
        '{"different":true}\n',
        encoding="utf-8",
    )
    with pytest.raises(CommanderError, match="raw evidence"):
        store.review_detail(session_dir)
    with pytest.raises(CommanderError, match="sealed"):
        store.claim_review(session_dir)


def test_review_packet_loss_after_claim_records_spent_usage(
    tmp_path: Path,
) -> None:
    config = _workspace(tmp_path)
    store = CommanderStore(config)
    request_id, detail = _import_plan(store, tmp_path, reported_usage=True)
    plan, plan_path, approval, receipt, _request = store.approved_plan(
        request_id,
        detail["request"]["planDigest"],
    )
    session_dir = config.artifacts_dir / plan.plan_id / "packet-loss"
    session = Session(session_dir, plan, session_id="packet-loss")
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
    result_path = session.write_frontier_result()
    claim = store.claim_review(session_dir)
    result_path.unlink()
    response = tmp_path / "packet-loss-review.json"
    response.write_text(
        json.dumps({
            "schemaVersion": 1,
            "sessionId": "packet-loss",
            "planId": plan.plan_id,
            "verdict": "approved",
            "summary": "Would have approved.",
            "findings": [],
        }),
        encoding="utf-8",
    )

    with pytest.raises(CommanderError, match="sealed"):
        store.import_review(
            session_dir,
            response,
            claim_id=claim["claimId"],
            prompt_tokens=9,
            completion_tokens=6,
            total_tokens=15,
        )

    detail_after = store.review_detail(session_dir)
    attempt = detail_after["reviewAttemptReceipt"]
    assert attempt["inputArtifactSha256"] == claim[
        "inputArtifactSha256"
    ]
    assert attempt["usage"]["totalTokens"] == 15
    assert detail_after["frontierUsage"]["planning"]["totalTokens"] == 150
    assert detail_after["frontierUsage"]["review"]["totalTokens"] == 15
    assert detail_after["frontierUsage"]["total"]["totalTokens"] == 165


def test_session_rejects_tampered_planning_receipt(tmp_path: Path) -> None:
    config = _workspace(tmp_path)
    store = CommanderStore(config)
    request_id, detail = _import_plan(store, tmp_path, reported_usage=True)
    plan, plan_path, approval, receipt, _request = store.approved_plan(
        request_id,
        detail["request"]["planDigest"],
    )
    tampered = json.loads(json.dumps(receipt))
    tampered["artifactSha256"] = "0" * 64
    session = Session(
        config.artifacts_dir / plan.plan_id / "receipt-tamper",
        plan,
        session_id="receipt-tamper",
    )
    session.set_sources(config_source=config.source, plan_source=plan_path)

    with pytest.raises(CommanderError, match="artifact digest"):
        session.attach_commander(
            request_id=request_id,
            approval=approval.to_json(),
            planning_receipt=tampered,
        )


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
    skills_dir = tmp_path / "codex-skills"
    installed = install_bundled_skill(
        skills_dir=skills_dir,
        host="codex",
    )
    assert installed.name == "mlx-swarm-commander"
    assert "name: mlx-swarm-commander" in (
        installed / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert (installed / "agents" / "openai.yaml").is_file()
    with pytest.raises(SkillInstallError, match="already exists"):
        install_bundled_skill(skills_dir=skills_dir, host="codex")
    assert install_bundled_skill(
        skills_dir=skills_dir,
        force=True,
        host="codex",
    ) == installed


def test_bundled_skill_installs_for_claude_without_codex_metadata(
    tmp_path: Path,
) -> None:
    installed = install_bundled_skill(
        skills_dir=tmp_path / "claude-skills",
        host="claude",
    )
    skill_text = (installed / "SKILL.md").read_text(encoding="utf-8")
    assert "Claude Code: `claude-code-skill`" in skill_text
    assert not (installed / "agents").exists()


def test_bundled_skill_teaches_topology_sizing_and_reimport() -> None:
    skill_text = (
        Path(__file__).parent.parent
        / "src"
        / "mlx_swarm"
        / "bundled_skills"
        / "mlx-swarm-commander"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "## Shape the DAG" in skill_text
    assert "wide and shallow" in skill_text
    assert "blast radius" in skill_text
    assert "Never delegate discovery" in skill_text
    assert "3.5 characters per token" in skill_text
    assert "gate.maxCharacters" in skill_text
    assert "`maxRepairAttempts` to 1 for local-agent tasks" in skill_text
    assert "every validation error at once" in skill_text
    assert "--approve-preview" in skill_text
    assert "calibration: unmeasured" in skill_text
    assert "Set `maxRepairAttempts` to zero" not in skill_text
    assert "Do not retry after an imported invalid response" not in skill_text


def test_claude_skill_install_honors_config_dir_and_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom_config))

    configured = install_bundled_skill(host="claude")
    assert configured == (
        custom_config / "skills" / "mlx-swarm-commander"
    ).resolve()

    explicit_root = tmp_path / "explicit-skills"
    explicit = install_bundled_skill(
        host="claude",
        skills_dir=explicit_root,
    )
    assert explicit == (
        explicit_root / "mlx-swarm-commander"
    ).resolve()


def test_bundled_skill_rejects_unknown_host_and_symlink_destination(
    tmp_path: Path,
) -> None:
    with pytest.raises(SkillInstallError, match="Unsupported skill host"):
        install_bundled_skill(
            skills_dir=tmp_path / "skills",
            host="unknown",
        )

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    sibling = skills_dir / "existing-skill"
    sibling.mkdir()
    marker = sibling / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    (skills_dir / "mlx-swarm-commander").symlink_to(
        sibling,
        target_is_directory=True,
    )
    with pytest.raises(SkillInstallError, match="symlinked"):
        install_bundled_skill(
            skills_dir=skills_dir,
            host="claude",
            force=True,
        )
    assert marker.read_text(encoding="utf-8") == "keep"


def test_legacy_namespace_resolves_to_canonical_modules() -> None:
    with pytest.warns(DeprecationWarning, match="deprecated"):
        legacy = importlib.import_module("swarm_agents.contracts")
    canonical = importlib.import_module("mlx_swarm.contracts")
    assert legacy is canonical
