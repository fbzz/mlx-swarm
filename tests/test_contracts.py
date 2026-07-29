"""Tests for strict JSON contract validation — config and plan loading."""
# @lat: [[Tests#Contracts]]

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mlx_swarm.contracts import (
    ContractError,
    Plan,
    SwarmConfig,
    TaskDef,
    load_config,
    load_plan,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, overrides: dict | None = None) -> Path:
    base = {
        "schemaVersion": 1,
        "model": {
            "repository": "mlx-community/test-model",
        },
        "batch": {
            "maxWorkers": 16,
            "prefillStepSize": 512,
            "maxPromptCharacters": 120000,
        },
        "artifacts": ".swarm/runs",
    }
    if overrides:
        base.update(overrides)
    p = tmp_path / "swarm.json"
    p.write_text(json.dumps(base))
    return p


def test_load_config_minimal(tmp_path: Path) -> None:
    p = _write_config(tmp_path)
    config = load_config(p)
    assert config.model.repository == "mlx-community/test-model"
    assert config.model.revision == ""
    assert config.batch.max_workers == 16
    assert config.artifacts_dir == (tmp_path / ".swarm/runs").resolve()
    assert config.enable_thinking is False
    assert config.seed == 20260727


def test_default_parallelism_is_interactive_four_workers(
    tmp_path: Path,
) -> None:
    raw = json.loads(_write_config(tmp_path).read_text())
    raw["batch"].pop("maxWorkers")
    path = tmp_path / "default-workers.json"
    path.write_text(json.dumps(raw))

    assert load_config(path).batch.max_workers == 4


def test_load_config_with_local_path(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}")
    p = _write_config(tmp_path, {"model": {"repository": "repo", "localPath": str(model_dir)}})
    config = load_config(p)
    assert config.model.local_path == str(model_dir)


def test_load_config_bad_schema_version(tmp_path: Path) -> None:
    p = _write_config(tmp_path, {"schemaVersion": 99})
    with pytest.raises(ContractError, match="Unsupported config schema version"):
        load_config(p)


def test_load_config_unknown_field(tmp_path: Path) -> None:
    p = _write_config(tmp_path, {"extraField": True})
    with pytest.raises(ContractError, match="unknown fields"):
        load_config(p)


def test_load_config_missing_model(tmp_path: Path) -> None:
    raw = json.loads(_write_config(tmp_path).read_text())
    del raw["model"]
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(ContractError, match="missing"):
        load_config(p)


def test_load_config_bad_max_workers(tmp_path: Path) -> None:
    p = _write_config(tmp_path, {"batch": {"maxWorkers": 0}})
    with pytest.raises(ContractError, match="maxWorkers"):
        load_config(p)


def test_load_config_rejects_non_boolean_thinking(tmp_path: Path) -> None:
    p = _write_config(tmp_path, {"enableThinking": "false"})
    with pytest.raises(ContractError, match="must be a boolean"):
        load_config(p)


def test_load_config_reasoning_edit_worker_is_strict(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, {
        "worker": {
            "mode": "reasoning-edit",
            "reasoningMaxTokens": 768,
        },
    }))
    assert config.worker.mode == "reasoning-edit"
    assert config.worker.reasoning_max_tokens == 768

    invalid = _write_config(tmp_path, {
        "worker": {
            "mode": "code-wizard",
            "reasoningMaxTokens": 768,
        },
    })
    with pytest.raises(ContractError, match="worker.mode"):
        load_config(invalid)


def test_load_config_worker_capability_contract_is_strict(
    tmp_path: Path,
) -> None:
    evidence = "a" * 64
    config = load_config(_write_config(tmp_path, {
        "worker": {
            "mode": "direct",
            "reasoningMaxTokens": 512,
            "capabilities": {
                "parameterScale": "4B",
                "contextWindowTokens": 262144,
                "maxGenerationTokens": 800,
                "specialization": "general",
                "delegationLevel": "exact-edit",
                "strengths": ["Exact bounded replacements."],
                "limitations": ["Unreliable independent diagnosis."],
                "calibration": {
                    "status": "failed",
                    "passedCases": 0,
                    "totalCases": 2,
                    "evidenceSha256": evidence,
                },
            },
        },
    }))
    capability = config.worker.capabilities
    assert capability.parameter_scale == "4B"
    assert capability.context_window_tokens == 262144
    assert capability.max_generation_tokens == 800
    assert capability.delegation_level == "exact-edit"
    assert capability.calibration.evidence_sha256 == evidence

    invalid = _write_config(tmp_path, {
        "worker": {
            "capabilities": {
                "calibration": {
                    "status": "passed",
                    "passedCases": 1,
                    "totalCases": 2,
                    "evidenceSha256": evidence,
                },
            },
        },
    })
    with pytest.raises(ContractError, match="every case"):
        load_config(invalid)


def test_plan_generation_cannot_exceed_worker_capability(
    tmp_path: Path,
) -> None:
    config = load_config(_write_config(tmp_path, {
        "worker": {
            "capabilities": {
                "maxGenerationTokens": 700,
            },
        },
    }))
    plan_path = _write_plan(tmp_path, {
        "tasks": [{
            "id": "task-a",
            "role": "implementation",
            "prompt": "Do something",
            "generationOverride": {"max_tokens": 701},
        }],
    })
    with pytest.raises(ContractError, match="maxGenerationTokens"):
        load_plan(plan_path, config)


def test_exact_edit_default_fits_eight_hundred_token_cap(
    tmp_path: Path,
) -> None:
    config = load_config(_write_config(tmp_path, {
        "schemaVersion": 2,
        "worker": {
            "capabilities": {
                "maxGenerationTokens": 800,
            },
        },
        "workspace": {
            "writeRoots": ["src"],
            "verificationProfiles": {},
        },
    }))
    plan_path = _write_plan(tmp_path, {
        "schemaVersion": 2,
        "tasks": [{
            "id": "task-a",
            "role": "implementation",
            "prompt": "Return one exact edit.",
            "artifactType": "patch",
            "workerOutputProtocol": "edit-manifest-v1",
            "allowedPaths": ["src/value.py"],
            "verification": [],
            "gate": {
                "requiredPatterns": [],
                "forbiddenPatterns": [],
                "maxCharacters": 4000,
                "format": "json",
                "jsonRequiredKeys": ["edits"],
                "jsonAllowedKeys": ["edits"],
            },
        }],
    })

    plan = load_plan(plan_path, config)

    assert plan.tasks[0].generation_override == {}


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def _write_plan(tmp_path: Path, overrides: dict | None = None) -> Path:
    base = {
        "schemaVersion": 1,
        "planId": "test-plan",
        "objective": "Test objective",
        "tasks": [
            {
                "id": "task-a",
                "role": "implementation",
                "prompt": "Do something",
            },
            {
                "id": "task-b",
                "role": "test",
                "prompt": "Test it",
                "dependsOn": ["task-a"],
            },
        ],
    }
    if overrides:
        base.update(overrides)
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(base))
    return p


def _config(tmp_path: Path) -> SwarmConfig:
    return load_config(_write_config(tmp_path))


def test_load_plan_basic(tmp_path: Path) -> None:
    config = _config(tmp_path)
    plan = load_plan(_write_plan(tmp_path), config)
    assert plan.plan_id == "test-plan"
    assert len(plan.tasks) == 2
    assert plan.tasks[0].id == "task-a"
    assert plan.tasks[1].depends_on == ("task-a",)


def test_load_plan_duplicate_task_id(tmp_path: Path) -> None:
    config = _config(tmp_path)
    p = _write_plan(tmp_path, {
        "tasks": [
            {"id": "dup", "role": "implementation", "prompt": "a"},
            {"id": "dup", "role": "test", "prompt": "b"},
        ]
    })
    with pytest.raises(ContractError, match="Duplicate task id"):
        load_plan(p, config)


def test_load_plan_bad_depends_on(tmp_path: Path) -> None:
    config = _config(tmp_path)
    p = _write_plan(tmp_path, {
        "tasks": [
            {"id": "task-a", "role": "implementation", "prompt": "a", "dependsOn": ["nonexistent"]},
        ]
    })
    with pytest.raises(ContractError, match="unknown task"):
        load_plan(p, config)


def test_load_plan_circular_dependency(tmp_path: Path) -> None:
    config = _config(tmp_path)
    p = _write_plan(tmp_path, {
        "tasks": [
            {"id": "a", "role": "implementation", "prompt": "a", "dependsOn": ["b"]},
            {"id": "b", "role": "test", "prompt": "b", "dependsOn": ["a"]},
        ]
    })
    with pytest.raises(ContractError, match="Circular dependency"):
        load_plan(p, config)


def test_topological_order(tmp_path: Path) -> None:
    config = _config(tmp_path)
    plan = load_plan(_write_plan(tmp_path), config)
    levels = plan.topological_order()
    assert len(levels) == 2
    assert levels[0][0].id == "task-a"
    assert levels[1][0].id == "task-b"


def test_load_plan_bad_role(tmp_path: Path) -> None:
    config = _config(tmp_path)
    p = _write_plan(tmp_path, {
        "tasks": [{"id": "x", "role": "invalid-role", "prompt": "hi"}]
    })
    with pytest.raises(ContractError, match="role must be one of"):
        load_plan(p, config)


def test_load_plan_with_gate(tmp_path: Path) -> None:
    config = _config(tmp_path)
    p = _write_plan(tmp_path, {
        "tasks": [
            {
                "id": "impl",
                "role": "implementation",
                "prompt": "code",
                "gate": {
                    "requiredPatterns": [{"id": "has-def", "pattern": "def "}],
                    "forbiddenPatterns": [{"id": "no-import", "pattern": "^import "}],
                    "maxCharacters": 5000,
                },
            },
        ],
    })
    plan = load_plan(p, config)
    assert plan.tasks[0].gate is not None
    assert len(plan.tasks[0].gate.required_patterns) == 1
    assert len(plan.tasks[0].gate.forbidden_patterns) == 1


def test_load_plan_rejects_unknown_generation_override(tmp_path: Path) -> None:
    config = _config(tmp_path)
    p = _write_plan(tmp_path, {
        "tasks": [{
            "id": "x",
            "role": "general",
            "prompt": "hi",
            "generationOverride": {"typo": 1},
        }]
    })
    with pytest.raises(ContractError, match="unknown fields"):
        load_plan(p, config)


def test_load_plan_rejects_invalid_gate_format(tmp_path: Path) -> None:
    config = _config(tmp_path)
    p = _write_plan(tmp_path, {
        "tasks": [{
            "id": "x",
            "role": "general",
            "prompt": "hi",
            "gate": {
                "requiredPatterns": [],
                "forbiddenPatterns": [],
                "maxCharacters": 100,
                "format": "yaml",
            },
        }]
    })
    with pytest.raises(ContractError, match="must be one of"):
        load_plan(p, config)


def test_plan_diagnosis_must_reference_authoritative_evidence(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    context = {
        "objective": "Repair the observed failure",
        "authoritativeSources": [{
            "label": "trace",
            "content": "src/value.py:1 failed",
        }],
        "constraints": [],
        "rejectionCriteria": ["The failure remains."],
        "outputProtocol": "Return the artifact.",
        "diagnosis": {
            "observedFailure": "The value assertion fails.",
            "causalHypothesis": "src/value.py returns the wrong value.",
            "validationMethod": "source-trace",
            "validationEvidence": "The trace identifies src/value.py:1.",
            "falsificationCondition": "The failing value comes from elsewhere.",
            "evidenceSources": ["trace"],
            "changeValidation": {
                "candidateChange": "Replace the wrong returned value.",
                "failingPathPrediction": (
                    "The assertion observes the corrected return value."
                ),
                "preservedControlPrediction": (
                    "The control path returning the other value is unchanged."
                ),
                "minimalityEvidence": (
                    "Only the exact failing return expression changes."
                ),
                "evidenceSources": ["trace"],
            },
        },
    }
    plan = load_plan(
        _write_plan(tmp_path, {"context": context}),
        config,
    )
    assert plan.context is not None
    assert plan.context.diagnosis is not None
    assert plan.context.diagnosis.validation_method == "source-trace"
    assert plan.context.diagnosis.change_validation is not None
    assert (
        plan.context.diagnosis.change_validation.candidate_change
        == "Replace the wrong returned value."
    )

    context["diagnosis"]["evidenceSources"] = ["invented"]
    with pytest.raises(ContractError, match="authoritative source"):
        load_plan(_write_plan(tmp_path, {"context": context}), config)

    context["diagnosis"]["evidenceSources"] = ["trace"]
    context["diagnosis"]["changeValidation"]["evidenceSources"] = [
        "invented"
    ]
    with pytest.raises(ContractError, match="changeValidation"):
        load_plan(_write_plan(tmp_path, {"context": context}), config)


def test_historical_plan_diagnosis_without_change_validation_is_readable(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    context = {
        "objective": "Inspect an existing result",
        "authoritativeSources": [{
            "label": "result",
            "content": "The existing artifact was accepted.",
        }],
        "constraints": [],
        "rejectionCriteria": ["The accepted result is ignored."],
        "outputProtocol": "Return a report.",
        "diagnosis": {
            "observedFailure": "The result has not been summarized.",
            "causalHypothesis": "No report task has consumed the result.",
            "validationMethod": "source-trace",
            "validationEvidence": "The source contains only the result.",
            "falsificationCondition": "A report already exists.",
            "evidenceSources": ["result"],
        },
    }

    plan = load_plan(_write_plan(tmp_path, {"context": context}), config)

    assert plan.context is not None
    assert plan.context.diagnosis is not None
    assert plan.context.diagnosis.change_validation is None


def test_load_plan_task_output_protocol(tmp_path: Path) -> None:
    config = _config(tmp_path)
    p = _write_plan(tmp_path, {
        "tasks": [{
            "id": "x",
            "role": "review",
            "prompt": "Review",
            "outputProtocol": "Return JSON only.",
        }]
    })
    plan = load_plan(p, config)
    assert plan.tasks[0].output_protocol == "Return JSON only."


def test_load_plan_too_many_tasks(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tasks = [{"id": f"t{i}", "role": "general", "prompt": "x"} for i in range(200)]
    p = _write_plan(tmp_path, {"tasks": tasks})
    with pytest.raises(ContractError, match="must contain 1 to"):
        load_plan(p, config)


def test_load_plan_invalid_json(tmp_path: Path) -> None:
    config = _config(tmp_path)
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(ContractError, match="Invalid JSON"):
        load_plan(p, config)


def test_load_plan_context(tmp_path: Path) -> None:
    config = _config(tmp_path)
    p = _write_plan(tmp_path, {
        "context": {
            "objective": "Build something",
            "authoritativeSources": [
                {"label": "API Spec", "content": "def foo(): pass"}
            ],
            "constraints": ["Must be pure Python"],
            "rejectionCriteria": ["No external deps"],
            "outputProtocol": "Return code only.",
        },
    })
    plan = load_plan(p, config)
    assert plan.context is not None
    assert plan.context.objective == "Build something"
    assert len(plan.context.authoritative_sources) == 1
    assert plan.context.authoritative_sources[0].label == "API Spec"
    assert plan.context.constraints == ("Must be pure Python",)
