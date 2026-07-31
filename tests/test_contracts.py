"""Tests for strict JSON contract validation — config and plan loading."""
# @lat: [[Tests#Contracts]]

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mlx_swarm.contracts import (
    ContractError,
    Plan,
    PlanValidationError,
    SwarmConfig,
    TaskDef,
    load_config,
    load_plan,
    serialized_deterministic_edits,
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


def test_default_batch_profile_is_two_agents(
    tmp_path: Path,
) -> None:
    raw = json.loads(_write_config(tmp_path).read_text())
    raw["batch"] = {}
    path = tmp_path / "default-workers.json"
    path.write_text(json.dumps(raw))

    config = load_config(path)
    assert config.batch.max_workers == 2
    assert config.batch.prefill_step_size == 1024
    assert config.batch.max_prompt_characters == 80_000
    assert config.batch.max_batch_prompt_tokens == 49_152


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


def _schema_v3_config(
    tmp_path: Path,
    *,
    max_generation_tokens: int = 800,
) -> SwarmConfig:
    return load_config(_write_config(tmp_path, {
        "schemaVersion": 2,
        "worker": {
            "capabilities": {
                "maxGenerationTokens": max_generation_tokens,
            },
        },
        "workspace": {
            "writeRoots": ["src", "tests"],
            "verificationProfiles": {
                "unit": {
                    "argv": ["python", "-m", "pytest", "-q"],
                },
            },
        },
    }))


def _schema_v3_task(
    task_id: str,
    path: str,
    *,
    context_ref: str,
) -> dict:
    return {
        "id": task_id,
        "role": "implementation",
        "prompt": f"Update {path}.",
        "artifactType": "patch",
        "workerOutputProtocol": "edit-manifest-v1",
        "executionMode": "local-agent",
        "contextRefs": [context_ref],
        "interfaceContract": "Keep public function result() -> int.",
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
            "jsonRequiredKeys": ["edits"],
            "jsonAllowedKeys": ["edits"],
        },
    }


def test_schema_v3_allows_disjoint_mutating_agents_with_minimal_context(
    tmp_path: Path,
) -> None:
    config = _schema_v3_config(tmp_path)
    plan_path = _write_plan(tmp_path, {
        "schemaVersion": 3,
        "integrationVerification": ["unit"],
        "context": {
            "objective": "Update two independent modules.",
            "authoritativeSources": [
                {"label": "left", "content": "src/left.py has LEFT = 1."},
                {"label": "right", "content": "src/right.py has RIGHT = 1."},
            ],
            "constraints": ["Keep the frozen interface."],
            "rejectionCriteria": ["A task changes the other task's path."],
            "outputProtocol": "Return only the requested artifact.",
        },
        "tasks": [
            _schema_v3_task("left", "src/left.py", context_ref="left"),
            _schema_v3_task("right", "src/right.py", context_ref="right"),
        ],
    })

    plan = load_plan(plan_path, config)

    assert plan.schema_version == 3
    assert plan.integration_verification == ("unit",)
    assert len(plan.topological_order()[0]) == 2
    assert plan.tasks[0].context_refs == ("left",)
    assert plan.tasks[1].expected_output_tokens == 400


def test_schema_v3_rejects_overlapping_paths_and_oversized_output(
    tmp_path: Path,
) -> None:
    config = _schema_v3_config(tmp_path)
    context = {
        "objective": "Update modules.",
        "authoritativeSources": [
            {"label": "source", "content": "src/value.py has VALUE = 1."},
        ],
        "constraints": [],
        "rejectionCriteria": ["Paths overlap."],
        "outputProtocol": "Return only JSON.",
    }
    left = _schema_v3_task("left", "src", context_ref="source")
    right = _schema_v3_task(
        "right",
        "src/value.py",
        context_ref="source",
    )
    with pytest.raises(ContractError, match="disjoint allowedPaths"):
        load_plan(_write_plan(tmp_path, {
            "schemaVersion": 3,
            "integrationVerification": ["unit"],
            "context": context,
            "tasks": [left, right],
        }), config)

    right["allowedPaths"] = ["src/right.py"]
    right["expectedOutputTokens"] = 561
    with pytest.raises(ContractError, match="preflight budget"):
        load_plan(_write_plan(tmp_path, {
            "schemaVersion": 3,
            "integrationVerification": ["unit"],
            "context": context,
            "tasks": [left, right],
        }), config)


def test_schema_v3_rejects_expected_output_above_exact_edit_ceiling(
    tmp_path: Path,
) -> None:
    config = _schema_v3_config(
        tmp_path,
        max_generation_tokens=2048,
    )
    task = _schema_v3_task(
        "bounded-edit",
        "src/value.py",
        context_ref="source",
    )
    task["expectedOutputTokens"] = 701
    task["generationOverride"]["max_tokens"] = 1024

    with pytest.raises(ContractError, match="preflight budget of 700 tokens"):
        load_plan(_write_plan(tmp_path, {
            "schemaVersion": 3,
            "integrationVerification": ["unit"],
            "context": {
                "objective": "Update one bounded module.",
                "authoritativeSources": [{
                    "label": "source",
                    "content": "src/value.py has VALUE = 1.",
                }],
                "constraints": [],
                "rejectionCriteria": ["The output exceeds its envelope."],
                "outputProtocol": "Return only JSON.",
            },
            "tasks": [task],
        }), config)


def test_schema_v3_requires_final_integration_profile(
    tmp_path: Path,
) -> None:
    config = _schema_v3_config(tmp_path)
    with pytest.raises(
        ContractError,
        match="integrationVerification must contain 1",
    ):
        load_plan(_write_plan(tmp_path, {
            "schemaVersion": 3,
            "integrationVerification": [],
            "tasks": [],
        }), config)


def test_schema_v3_deterministic_edit_requires_no_local_generation(
    tmp_path: Path,
) -> None:
    config = _schema_v3_config(tmp_path)
    task = _schema_v3_task(
        "known-edit",
        "src/value.py",
        context_ref="source",
    )
    task.update({
        "executionMode": "deterministic-edit",
        "expectedOutputTokens": 0,
        "maxRepairAttempts": 0,
        "deterministicEdits": [{
            "path": "src/value.py",
            "old": "VALUE = 1",
            "new": "VALUE = 2",
        }],
    })
    task.pop("generationOverride")
    plan = load_plan(_write_plan(tmp_path, {
        "schemaVersion": 3,
        "integrationVerification": ["unit"],
        "context": {
            "objective": "Apply a known edit.",
            "authoritativeSources": [{
                "label": "source",
                "content": "src/value.py has VALUE = 1.",
            }],
            "constraints": [],
            "rejectionCriteria": ["The exact edit is not applied."],
            "outputProtocol": "Return only JSON.",
        },
        "tasks": [task],
    }), config)

    assert plan.tasks[0].execution_mode == "deterministic-edit"
    assert plan.tasks[0].deterministic_edits[0]["new"] == "VALUE = 2"


def test_schema_v3_deterministic_edit_preserves_exact_whitespace(
    tmp_path: Path,
) -> None:
    config = _schema_v3_config(tmp_path)
    task = _schema_v3_task(
        "known-edit",
        "src/value.py",
        context_ref="source",
    )
    task.update({
        "executionMode": "deterministic-edit",
        "expectedOutputTokens": 0,
        "maxRepairAttempts": 0,
        "deterministicEdits": [{
            "path": "src/value.py",
            "old": "  VALUE = 1\n",
            "new": "\n  VALUE = 2\n\n",
        }],
    })
    task.pop("generationOverride")
    plan = load_plan(_write_plan(tmp_path, {
        "schemaVersion": 3,
        "integrationVerification": ["unit"],
        "context": {
            "objective": "Apply exact whitespace.",
            "authoritativeSources": [{
                "label": "source",
                "content": "The source has an indented constant.",
            }],
            "constraints": [],
            "rejectionCriteria": ["Whitespace changes unexpectedly."],
            "outputProtocol": "Return only JSON.",
        },
        "tasks": [task],
    }), config)

    assert plan.tasks[0].deterministic_edits[0] == {
        "path": "src/value.py",
        "old": "  VALUE = 1\n",
        "new": "\n  VALUE = 2\n\n",
    }


def _deterministic_edit_plan_data(edits: list[dict], max_characters: int) -> dict:
    task = _schema_v3_task(
        "known-edit",
        "src/value.py",
        context_ref="source",
    )
    task.update({
        "executionMode": "deterministic-edit",
        "expectedOutputTokens": 0,
        "maxRepairAttempts": 0,
        "deterministicEdits": edits,
    })
    task["gate"]["maxCharacters"] = max_characters
    task.pop("generationOverride")
    return {
        "schemaVersion": 3,
        "integrationVerification": ["unit"],
        "context": {
            "objective": "Apply a known edit.",
            "authoritativeSources": [{
                "label": "source",
                "content": "src/value.py has VALUE = 1.",
            }],
            "constraints": [],
            "rejectionCriteria": ["The exact edit is not applied."],
            "outputProtocol": "Return only JSON.",
        },
        "tasks": [task],
    }


def test_schema_v3_deterministic_edits_exceeding_gate_size_are_rejected(
    tmp_path: Path,
) -> None:
    config = _schema_v3_config(tmp_path)
    edits = [{
        "path": "src/value.py",
        "old": "VALUE = 1",
        "new": "VALUE = 2\n" + ("# padding line\n" * 40),
    }]
    payload_length = len(serialized_deterministic_edits(edits))
    plan_path = _write_plan(
        tmp_path,
        _deterministic_edit_plan_data(edits, payload_length - 1),
    )

    with pytest.raises(
        ContractError,
        match="deterministicEdits serialize",
    ):
        load_plan(plan_path, config)


def test_plan_validation_reports_all_task_errors_at_once(
    tmp_path: Path,
) -> None:
    config = _schema_v3_config(tmp_path)
    bad_role = _schema_v3_task("left", "src/left.py", context_ref="left")
    bad_role["role"] = "unknown-role"
    bad_budget = _schema_v3_task("right", "src/right.py", context_ref="right")
    bad_budget["expectedOutputTokens"] = 700
    plan_path = _write_plan(tmp_path, {
        "schemaVersion": 3,
        "integrationVerification": ["unit"],
        "context": {
            "objective": "Update two independent modules.",
            "authoritativeSources": [
                {"label": "left", "content": "src/left.py has LEFT = 1."},
                {"label": "right", "content": "src/right.py has RIGHT = 1."},
            ],
            "constraints": [],
            "rejectionCriteria": ["A task fails validation."],
            "outputProtocol": "Return only the requested artifact.",
        },
        "tasks": [bad_role, bad_budget],
    })

    with pytest.raises(PlanValidationError) as exc_info:
        load_plan(plan_path, config)

    error = exc_info.value
    assert len(error.errors) == 2
    assert "role must be one of" in error.errors[0]
    assert "preflight budget" in error.errors[1]
    assert "role must be one of" in str(error)
    assert "preflight budget" in str(error)


def test_plan_validation_single_error_message_is_unchanged(
    tmp_path: Path,
) -> None:
    config = _schema_v3_config(tmp_path)
    bad_role = _schema_v3_task("left", "src/left.py", context_ref="left")
    bad_role["role"] = "unknown-role"
    plan_path = _write_plan(tmp_path, {
        "schemaVersion": 3,
        "integrationVerification": ["unit"],
        "context": {
            "objective": "Update one module.",
            "authoritativeSources": [
                {"label": "left", "content": "src/left.py has LEFT = 1."},
            ],
            "constraints": [],
            "rejectionCriteria": ["The task fails validation."],
            "outputProtocol": "Return only the requested artifact.",
        },
        "tasks": [bad_role],
    })

    with pytest.raises(PlanValidationError) as exc_info:
        load_plan(plan_path, config)

    error = exc_info.value
    assert len(error.errors) == 1
    assert str(error) == error.errors[0]
    assert "Plan validation found" not in str(error)


def test_plan_validation_failed_task_id_still_satisfies_dependents(
    tmp_path: Path,
) -> None:
    config = _schema_v3_config(tmp_path)
    bad_role = _schema_v3_task("left", "src/left.py", context_ref="left")
    bad_role["role"] = "unknown-role"
    dependent = _schema_v3_task("right", "src/right.py", context_ref="right")
    dependent["dependsOn"] = ["left"]
    plan_path = _write_plan(tmp_path, {
        "schemaVersion": 3,
        "integrationVerification": ["unit"],
        "context": {
            "objective": "Update two modules in sequence.",
            "authoritativeSources": [
                {"label": "left", "content": "src/left.py has LEFT = 1."},
                {"label": "right", "content": "src/right.py has RIGHT = 1."},
            ],
            "constraints": [],
            "rejectionCriteria": ["A task fails validation."],
            "outputProtocol": "Return only the requested artifact.",
        },
        "tasks": [bad_role, dependent],
    })

    with pytest.raises(PlanValidationError) as exc_info:
        load_plan(plan_path, config)

    error = exc_info.value
    assert len(error.errors) == 1
    assert "unknown task" not in str(error)


def test_schema_v3_deterministic_edits_at_exact_gate_size_load(
    tmp_path: Path,
) -> None:
    config = _schema_v3_config(tmp_path)
    edits = [{
        "path": "src/value.py",
        "old": "VALUE = 1",
        "new": "VALUE = 2",
    }]
    payload_length = len(serialized_deterministic_edits(edits))
    plan = load_plan(
        _write_plan(
            tmp_path,
            _deterministic_edit_plan_data(edits, payload_length),
        ),
        config,
    )

    assert plan.tasks[0].execution_mode == "deterministic-edit"


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
