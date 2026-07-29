"""Tests for the paired BugsInPy economics evaluation."""
# @lat: [[Tests#Economics evaluation]]

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mlx_swarm.contracts import (
    ContextSource,
    OutputGate,
    Plan,
    TaskContext,
    TaskDef,
    load_config,
)
from mlx_swarm.evaluation import (
    FAIR_EVALUATION_PROTOCOL_VERSION,
    _executed_lines_from_trace_cover,
    _rank_traced_function_windows,
    _remove_timed_out_docker_container,
    _render_executed_line_map,
    _render_runtime_local_evidence,
    _requested_source_windows,
    CommandResult,
    EvaluationError,
    EvaluationStore,
    aggregate_results,
    apply_protocol_audit,
    bootstrap_mean_interval,
    build_task_packet,
    capability_diagnostic_gate,
    collect_buggy_execution_trace,
    collect_buggy_runtime_locals,
    container_path,
    copy_fixed_test_support,
    deterministic_case_context,
    directory_size,
    docker_connection_environment,
    docker_runtime_argv,
    empty_local_usage,
    ensure_pair_contract,
    evaluation_write_roots,
    evaluation_case,
    exclusive_case_lock,
    fresh_arm_repository,
    frontier_alone_response_prompt,
    frontier_delegation_blueprint_prompt,
    hermes_command,
    inspect_codex_version,
    inspect_frontier_version,
    inspect_container,
    install_frozen_prompt_replay,
    is_dependency_or_project_install,
    load_evaluation_profile,
    local_replay_promotion_gate,
    make_arm_result,
    materialize_frontier_edit_manifest,
    materialize_frontier_delegation_plan,
    mlx_swarm_source_revision,
    normalize_setup_parallelism,
    oracle_infrastructure_failure,
    parse_benchmark_commands,
    parse_codex_usage_jsonl,
    parse_hermes_usage_json,
    parse_frontier_delegation_blueprint,
    patch_metadata,
    preliminary_study_subset,
    preliminary_evaluation_profile,
    profile_payload,
    remove_sensitive_preparation_sources,
    render_readme_economics,
    retained_session_candidate_diff,
    run_command,
    run_swarm_with_synthetic_operator,
    sanitize_suite,
    select_cases,
    split_constraint_text,
    strip_one_json_fence,
    update_readme_economics,
    usage_with_phases,
    validate_arm_result,
    validate_candidate_diff,
    validate_evaluation_plan,
    validate_repository_symlinks,
    validate_resolved_dependencies,
    write_evaluation_config,
)
from mlx_swarm.session import Session


def _write_config(tmp_path: Path) -> Path:
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / "tokenizer.json").write_text("{}", encoding="utf-8")
    (model / "model.safetensors").write_bytes(b"")
    path = tmp_path / "swarm.json"
    path.write_text(
        json.dumps({
            "schemaVersion": 1,
            "model": {
                "repository": "local/evaluation-model",
                "localPath": str(model),
            },
            "batch": {"maxWorkers": 2},
            "artifacts": ".swarm/runs",
        }),
        encoding="utf-8",
    )
    return path


def _profile_payload(
    *,
    pilot_size: int = 3,
    measured_size: int = 6,
    projects: tuple[str, ...] = ("alpha", "beta", "gamma"),
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "profileId": "test-study",
        "benchmark": {
            "repository": "https://example.invalid/bugsinpy.git",
            "revision": "a" * 40,
        },
        "seed": 20260728,
        "selection": {
            "pilotSize": pilot_size,
            "measuredSize": measured_size,
            "minProjects": len(projects),
            "maxPerProject": measured_size // len(projects),
            "maxChangedFiles": 4,
            "maxChangedLines": 200,
            "maxContextCharacters": 120000,
            "minimumPython": "3.8",
            "projects": list(projects),
        },
        "storage": {
            "maxBytes": 20 * 1024**3,
            "minFreeBytes": 15 * 1024**3,
        },
        "container": {
            "image": "benchmark@sha256:" + "b" * 64,
            "digest": "sha256:" + "b" * 64,
            "platform": "linux/amd64",
        },
        "frontier": {
            "command": "codex",
            "codexVersion": "codex-cli 0.145.0",
            "model": "gpt-5.6-sol",
            "reasoningEffort": "high",
            "armTimeoutSeconds": 2700,
            "planningTimeoutSeconds": 600,
            "localTimeoutSeconds": 1500,
            "reviewTimeoutSeconds": 600,
        },
        "pythonBootstrap": [
            "pip==23.3.2",
            "setuptools==57.5.0",
            "wheel==0.41.3",
            "Cython==0.29.36",
        ],
        "dependencyRoots": {
            project: ["pytest"]
            for project in projects
        },
        "dependencyPins": {
            project: []
            for project in projects
        },
        "local": {"maxRepair": 2},
    }


def _write_profile(tmp_path: Path, payload: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(payload or _profile_payload()),
        encoding="utf-8",
    )
    return path


def _hermes_profile_payload() -> dict[str, Any]:
    payload = _profile_payload()
    payload["schemaVersion"] = 3
    payload["profileId"] = "test-study-glm52"
    payload["frontier"] = {
        "adapter": "hermes-completion",
        "command": "hermes",
        "commandVersion": (
            "Hermes Agent v0.19.0 (2026.7.20) · upstream cbc1054e"
        ),
        "provider": "ollama-cloud",
        "model": "glm-5.2",
        "contextWindowTokens": 262144,
        "maxCompletionTokens": 16384,
        "reasoningEffort": "none",
        "toolsets": [],
        "armTimeoutSeconds": 2700,
        "planningTimeoutSeconds": 600,
        "localTimeoutSeconds": 1500,
        "reviewTimeoutSeconds": 600,
    }
    return payload


def _case(case_id: str, project: str, stratum: str) -> dict[str, Any]:
    number = int(case_id.rsplit("-", 1)[-1])
    return {
        "caseId": case_id,
        "project": project,
        "bugId": number,
        "repository": f"https://example.invalid/{project}.git",
        "buggyCommit": f"{number:040x}",
        "fixedCommit": f"{number + 100:040x}",
        "pythonVersion": "3.11",
        "testFiles": ["tests/test_bug.py"],
        "setupArgv": [],
        "verificationArgv": [["pytest", "-q", "tests/test_bug.py"]],
        "requirements": ["pytest==8.3.0"],
        "reference": {
            "paths": ["src/value.py"],
            "changedFiles": 1,
            "changedLines": {"small": 2, "medium": 20, "large": 100}[stratum],
            "sha256": f"{number + 200:064x}",
            "stratum": stratum,
        },
    }


def _reported_usage(total: int) -> dict[str, Any]:
    return usage_with_phases([(
        "phase",
        {
            "usageStatus": "reported",
            "turns": 1,
            "promptTokens": total - 10,
            "cachedInputTokens": 2,
            "completionTokens": 10,
            "reasoningTokens": 3,
            "totalTokens": total,
            "malformedLines": 0,
        },
    )])


def _result(
    case_id: str,
    arm: str,
    *,
    total_tokens: int,
    score: int = 1,
    elapsed: float = 60,
    phase: str = "measured",
) -> dict[str, Any]:
    case = {"caseId": case_id, "phase": phase}
    return make_arm_result(
        case=case,
        arm=arm,
        status="completed",
        completed=True,
        score=score,
        elapsed_seconds=elapsed,
        phase_seconds={
            (
                "frontier"
                if arm == "frontier-alone"
                else "local"
            ): elapsed - 1,
            "oracle": 1,
        },
        frontier_usage=_reported_usage(total_tokens),
        local_usage=(
            {
                "promptTokens": 100,
                "generationTokens": 50,
                "generationCalls": 2,
                "modelLoads": 1,
            }
            if arm == "mlx-swarm"
            else empty_local_usage()
        ),
        repairs=1 if arm == "mlx-swarm" else 0,
        model_loads=1 if arm == "mlx-swarm" else 0,
        review_verdict="approved" if arm == "mlx-swarm" else None,
        patch={"sha256": "f" * 64, "changedFiles": 1},
        oracle={"passed": bool(score), "exitCode": 0 if score else 1, "evidence": "ok"},
    )


def test_profile_is_strict_pinned_and_round_trips(tmp_path: Path) -> None:
    profile = load_evaluation_profile(_write_profile(tmp_path))
    assert profile.seed == 20260728
    assert profile.frontier.codex_version == "codex-cli 0.145.0"
    assert profile.frontier.model == "gpt-5.6-sol"
    assert profile.frontier.reasoning_effort == "high"
    assert profile.storage.max_bytes == 20 * 1024**3
    assert profile_payload(profile) == _profile_payload()

    invalid = _profile_payload()
    invalid["surprise"] = True
    with pytest.raises(EvaluationError, match="unknown fields"):
        load_evaluation_profile(_write_profile(tmp_path, invalid))


def test_hermes_profile_is_strict_pinned_and_round_trips(
    tmp_path: Path,
) -> None:
    payload = _hermes_profile_payload()
    profile = load_evaluation_profile(_write_profile(tmp_path, payload))
    assert profile.schema_version == 3
    assert profile.frontier.adapter == "hermes-completion"
    assert profile.frontier.provider == "ollama-cloud"
    assert profile.frontier.model == "glm-5.2"
    assert profile.frontier.context_window == 262144
    assert profile.frontier.max_completion_tokens == 16384
    assert profile.frontier.reasoning_effort == "none"
    assert profile.frontier.toolsets == ()
    assert profile_payload(profile) == payload

    legacy = json.loads(json.dumps(payload))
    legacy["schemaVersion"] = 2
    legacy["frontier"]["adapter"] = "hermes-oneshot"
    legacy["frontier"]["toolsets"] = ["todo"]
    del legacy["frontier"]["maxCompletionTokens"]
    del legacy["frontier"]["reasoningEffort"]
    legacy_profile = load_evaluation_profile(
        _write_profile(tmp_path, legacy)
    )
    assert legacy_profile.frontier.adapter == "hermes-oneshot"
    assert legacy_profile.frontier.max_completion_tokens == 0
    assert profile_payload(legacy_profile) == legacy

    legacy_with_new_key = _profile_payload()
    legacy_with_new_key["frontier"]["adapter"] = "hermes-completion"
    with pytest.raises(EvaluationError, match="unknown fields"):
        load_evaluation_profile(
            _write_profile(tmp_path, legacy_with_new_key)
        )

    invalid_toolsets = _hermes_profile_payload()
    invalid_toolsets["frontier"]["toolsets"] = ["todo"]
    with pytest.raises(EvaluationError, match="toolsets"):
        load_evaluation_profile(
            _write_profile(tmp_path, invalid_toolsets)
        )

    missing_context = _hermes_profile_payload()
    del missing_context["frontier"]["contextWindowTokens"]
    with pytest.raises(EvaluationError, match="missing fields"):
        load_evaluation_profile(_write_profile(tmp_path, missing_context))

    missing_limit = _hermes_profile_payload()
    del missing_limit["frontier"]["maxCompletionTokens"]
    with pytest.raises(EvaluationError, match="missing fields"):
        load_evaluation_profile(_write_profile(tmp_path, missing_limit))

    invalid_effort = _hermes_profile_payload()
    invalid_effort["frontier"]["reasoningEffort"] = "extreme"
    with pytest.raises(EvaluationError, match="reasoningEffort"):
        load_evaluation_profile(_write_profile(tmp_path, invalid_effort))


def test_hermes_command_and_version_pin_are_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = load_evaluation_profile(
        _write_profile(tmp_path, _hermes_profile_payload())
    )
    command_root = tmp_path / "bin"
    command_root.mkdir()
    command = command_root / "hermes"
    command.write_text(f"#!{sys.executable}\n", encoding="utf-8")
    command.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        f"{command_root}{os.pathsep}{os.environ.get('PATH', '')}",
    )
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Return JSON.", encoding="utf-8")
    usage_file = tmp_path / "usage.json"
    argv = hermes_command(
        profile,
        usage_file=usage_file,
        prompt_file=prompt_file,
        request_timeout_seconds=600,
    )
    assert argv == [
        sys.executable,
        str(
            Path(__file__).parents[1]
            / "src"
            / "mlx_swarm"
            / "hermes_completion.py"
        ),
        "--provider",
        "ollama-cloud",
        "--model",
        "glm-5.2",
        "--prompt-file",
        str(prompt_file),
        "--usage-file",
        str(usage_file),
        "--max-completion-tokens",
        "16384",
        "--reasoning-effort",
        "none",
        "--request-timeout-seconds",
        "600",
    ]
    assert all(value not in {"shell", "-c"} for value in argv)

    monkeypatch.setattr(
        "mlx_swarm.evaluation._best_effort_version",
        lambda _argv: profile.frontier.command_version,
    )
    assert inspect_frontier_version(profile) == (
        profile.frontier.command_version
    )
    monkeypatch.setattr(
        "mlx_swarm.evaluation._best_effort_version",
        lambda _argv: "Hermes Agent v0.18.0",
    )
    with pytest.raises(EvaluationError, match="version mismatch"):
        inspect_frontier_version(profile)


def test_hermes_completion_bridge_makes_one_tool_free_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import ModuleType, SimpleNamespace

    from mlx_swarm.hermes_completion import main

    calls: list[dict[str, Any]] = []
    response_content = ['{"edits":[]}']

    class FakeCompletions:
        def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=120,
                    completion_tokens=30,
                    total_tokens=150,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=80),
                    completion_tokens_details=SimpleNamespace(
                        reasoning_tokens=20
                    ),
                ),
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=response_content[0]
                        )
                    )
                ],
            )

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["api_key"] == "secret"
            assert kwargs["base_url"] == "https://example.invalid/v1"
            assert kwargs["max_retries"] == 0
            self.chat = SimpleNamespace(completions=FakeCompletions())

    runtime_module = ModuleType("hermes_cli.runtime_provider")
    runtime_module.resolve_runtime_provider = lambda **_kwargs: {
        "api_mode": "chat_completions",
        "api_key": "secret",
        "base_url": "https://example.invalid/v1",
    }
    hermes_package = ModuleType("hermes_cli")
    hermes_package.__path__ = []  # type: ignore[attr-defined]
    openai_module = ModuleType("openai")
    openai_module.OpenAI = FakeOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_package)
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.runtime_provider",
        runtime_module,
    )
    monkeypatch.setitem(sys.modules, "openai", openai_module)

    prompt = tmp_path / "prompt.txt"
    usage = tmp_path / "usage.json"
    prompt.write_text("Return the manifest.", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes_completion.py",
            "--provider",
            "ollama-cloud",
            "--model",
            "glm-5.2",
            "--prompt-file",
            str(prompt),
            "--usage-file",
            str(usage),
            "--max-completion-tokens",
            "16384",
            "--reasoning-effort",
            "none",
            "--request-timeout-seconds",
            "600",
        ],
    )
    assert main() == 0
    assert capsys.readouterr().out == '{"edits":[]}\n'
    assert len(calls) == 1
    call = calls[0]
    assert call["model"] == "glm-5.2"
    assert call["max_tokens"] == 16384
    assert call["reasoning_effort"] == "none"
    assert call["response_format"] == {"type": "json_object"}
    assert "tools" not in call
    receipt = json.loads(usage.read_text(encoding="utf-8"))
    assert receipt["api_calls"] == 1
    assert receipt["total_tokens"] == 150
    assert receipt["cache_read_tokens"] == 80
    assert receipt["reasoning_tokens"] == 20
    assert receipt["completed"] is True

    response_content[0] = ""
    failed_usage = tmp_path / "failed-usage.json"
    failed_argv = list(sys.argv)
    failed_argv[failed_argv.index("--usage-file") + 1] = str(failed_usage)
    monkeypatch.setattr(
        sys,
        "argv",
        failed_argv,
    )
    assert main() == 1
    assert "no final response content" in capsys.readouterr().err
    failed_receipt = json.loads(
        failed_usage.read_text(encoding="utf-8")
    )
    assert failed_receipt["input_tokens"] == 120
    assert failed_receipt["output_tokens"] == 30
    assert failed_receipt["total_tokens"] == 150
    assert failed_receipt["completed"] is False
    assert failed_receipt["failed"] is True


def test_preliminary_profile_is_fixed_two_plus_six() -> None:
    profile = preliminary_evaluation_profile(
        load_evaluation_profile(Path("benchmarks/bugsinpy-v1/profile.json"))
    )
    assert profile.profile_id.endswith("-preliminary")
    assert profile.selection.pilot_size == 2
    assert profile.selection.measured_size == 6
    assert profile.selection.min_projects == 6
    assert profile.selection.max_per_project == 1


def test_profile_rejects_unpinned_revision_and_timeout_drift(
    tmp_path: Path,
) -> None:
    unpinned = _profile_payload()
    unpinned["benchmark"]["revision"] = "main"
    with pytest.raises(EvaluationError, match="pinned"):
        load_evaluation_profile(_write_profile(tmp_path, unpinned))

    timeout = _profile_payload()
    timeout["frontier"]["reviewTimeoutSeconds"] = 601
    with pytest.raises(EvaluationError, match="must sum"):
        load_evaluation_profile(_write_profile(tmp_path, timeout))

    missing_project_roots = _profile_payload()
    del missing_project_roots["dependencyRoots"]["alpha"]
    with pytest.raises(EvaluationError, match="exactly match"):
        load_evaluation_profile(
            _write_profile(tmp_path, missing_project_roots)
        )

    floating_bootstrap = _profile_payload()
    floating_bootstrap["pythonBootstrap"][0] = "pip>=23"
    with pytest.raises(EvaluationError, match="exact"):
        load_evaluation_profile(_write_profile(tmp_path, floating_bootstrap))


def test_codex_version_pin_and_constraint_chunking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = load_evaluation_profile(_write_profile(tmp_path))
    monkeypatch.setattr(
        "mlx_swarm.evaluation._best_effort_version",
        lambda _argv: "codex-cli 0.145.0",
    )
    assert inspect_codex_version(profile) == "codex-cli 0.145.0"
    monkeypatch.setattr(
        "mlx_swarm.evaluation._best_effort_version",
        lambda _argv: "codex-cli 0.139.0",
    )
    with pytest.raises(EvaluationError, match="version mismatch"):
        inspect_codex_version(profile)

    packet = "failure evidence\n" * 1_000
    chunks = split_constraint_text(packet)
    assert "".join(chunks) == packet
    assert all(0 < len(chunk) <= 4_000 for chunk in chunks)


def test_task_packet_exposes_identical_write_roots_to_both_arms() -> None:
    case = {
        "caseId": "alpha-1",
        "project": "alpha",
        "objective": "Repair alpha",
        "verificationArgv": [["pytest", "-q"]],
    }
    packet = build_task_packet(
        case,
        {"failureEvidence": "failed"},
        ["alpha", "shared.py"],
    )
    assert "APPROVED WRITE ROOTS (identical for both paired arms)" in packet
    assert "- alpha" in packet
    assert "- shared.py" in packet


def test_task_packet_context_is_deterministic_and_uses_only_buggy_tree(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    (base / "src").mkdir(parents=True)
    (base / "tests").mkdir()
    (base / "src" / "target.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    (base / "tests" / "test_target.py").write_text(
        "def test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    (base / "unrelated.py").write_text(
        "SECRET_FUTURE_VALUE = 3\n",
        encoding="utf-8",
    )
    case = {
        "caseId": "alpha-1",
        "project": "alpha",
        "objective": "Repair alpha",
        "verificationArgv": [["pytest", "-q"]],
        "testFiles": ["tests/test_target.py"],
    }
    runtime = {
        "baseSnapshot": str(base),
        "failureEvidence": "src/target.py:1 assertion failed",
    }
    first = build_task_packet(case, runtime, ["src", "unrelated.py"])
    second = build_task_packet(case, runtime, ["src", "unrelated.py"])
    assert first == second
    assert "src/target.py" in first
    assert "tests/test_target.py" in first
    assert "VALUE = 1" in first
    assert "assert VALUE == 2" in first
    # The complete tree is shared, but unrelated production contents are not
    # promoted into the bounded relevant-source section.
    relevant = first.split(
        "FROZEN RELEVANT TEST AND TRACEBACK SOURCE CONTEXT:\n",
        1,
    )[1].split("INITIAL FAILURE EVIDENCE:", 1)[0]
    assert "SECRET_FUTURE_VALUE" not in relevant


def test_pair_contract_freezes_one_exact_packet_for_both_arms(
    tmp_path: Path,
) -> None:
    case_root = tmp_path / "case"
    base = case_root / "base"
    (base / "src").mkdir(parents=True)
    (base / "tests").mkdir()
    (base / "src" / "target.py").write_text("VALUE = 1\n")
    (base / "tests" / "test_target.py").write_text(
        "def test_value():\n    assert VALUE == 2\n"
    )
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "Benchmark"],
        ["git", "config", "user.email", "benchmark@example.invalid"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "base"],
    ):
        assert run_command(argv, cwd=base, timeout=30).returncode == 0
    case = {
        "caseId": "alpha-1",
        "project": "alpha",
        "objective": "Repair alpha",
        "verificationArgv": [["pytest", "-q"]],
        "testFiles": ["tests/test_target.py"],
    }
    runtime = {
        "baseSnapshot": str(base),
        "failureEvidence": "src/target.py:1 assertion failed",
    }
    first = ensure_pair_contract(
        case,
        runtime,
        maximum_characters=120_000,
    )
    second = ensure_pair_contract(
        case,
        runtime,
        maximum_characters=120_000,
    )
    assert first == second
    assert first["approvedWriteRoots"] == ["src"]
    assert (
        first["taskPacketSha256"]
        == hashlib.sha256(
            first["taskPacket"].encode()
        ).hexdigest()
    )
    assert (case_root / "pair-contract.json").is_file()

    (base / "src" / "target.py").write_text("VALUE = 99\n")
    with pytest.raises(EvaluationError, match="differs"):
        ensure_pair_contract(
            case,
            runtime,
            maximum_characters=120_000,
        )


def _evaluation_plan(
    tmp_path: Path,
    *,
    allowed_paths: tuple[str, ...],
    source_content: str,
) -> Plan:
    context = TaskContext(
        objective="Repair value",
        authoritative_sources=(
            ContextSource(
                label="VERBATIM FILE: src/value.py",
                content=source_content,
                sha256="unused",
            ),
        ),
        constraints=("Production only",),
        rejection_criteria=("No tests",),
        output_protocol="Return one diff.",
    )
    task = TaskDef(
        id="repair",
        role="implementation",
        prompt="Repair src/value.py.",
        artifact_type="patch",
        allowed_paths=allowed_paths,
        verification=("bugsinpy-acceptance",),
        worker_output_protocol="edit-manifest-v1",
        generation_override={
            "temperature": 0,
            "top_p": 1,
            "enable_thinking": False,
            "max_tokens": 400,
        },
        gate=OutputGate(
            output_format="json",
            json_required_keys=("edits",),
            json_allowed_keys=("edits",),
        ),
    )
    return Plan(
        source=tmp_path / "plan.json",
        plan_id="fair-plan",
        objective="Repair value",
        context=context,
        tasks=(task,),
        raw={},
        schema_version=2,
    )


def test_evaluation_plan_requires_symmetric_roots_and_verbatim_sources(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    (repository / "src").mkdir(parents=True)
    (repository / "src" / "value.py").write_text(
        "VALUE = 1\nOTHER = 2\n",
        encoding="utf-8",
    )
    valid = _evaluation_plan(
        tmp_path,
        allowed_paths=("src",),
        source_content="VALUE = 1\n",
    )
    validate_evaluation_plan(valid, repository, ["src"])

    narrowed = _evaluation_plan(
        tmp_path,
        allowed_paths=("src/value.py",),
        source_content="VALUE = 1\n",
    )
    with pytest.raises(EvaluationError, match="exact paired-arm write roots"):
        validate_evaluation_plan(narrowed, repository, ["src"])

    rewritten = _evaluation_plan(
        tmp_path,
        allowed_paths=("src",),
        source_content="VALUE = 1\n# omitted lines\n",
    )
    with pytest.raises(EvaluationError, match="exact contiguous excerpt"):
        validate_evaluation_plan(rewritten, repository, ["src"])


def test_candidate_diff_cannot_escape_shared_arm_roots(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    assert run_command(
        ["git", "init", "-q"],
        cwd=repository,
        timeout=30,
    ).returncode == 0
    (repository / "src").mkdir()
    (repository / "tests").mkdir()
    (repository / "src" / "value.py").write_text("VALUE = 1\n")
    (repository / "tests" / "helper.py").write_text("VALUE = 1\n")
    patch = (
        "diff --git a/tests/helper.py b/tests/helper.py\n"
        "--- a/tests/helper.py\n"
        "+++ b/tests/helper.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )
    error = validate_candidate_diff(
        patch,
        {"caseId": "alpha-1"},
        repository,
        allowed_paths=["src"],
    )
    assert error is not None
    assert "non-production" in error or "approved write roots" in error


def test_protocol_audit_invalidates_old_results_and_accepts_current() -> None:
    old = {
        "claim": {"status": "preliminary", "text": "directional"},
        "decisionGate": {"status": "stop_and_improve_workers"},
    }
    apply_protocol_audit(old, {})
    assert old["claim"]["status"] == "protocol_invalid"
    assert old["decisionGate"]["status"] == "rerun_fair_protocol"

    current = {
        "claim": {"status": "preliminary", "text": "directional"},
    }
    apply_protocol_audit(
        current,
        {
            "evaluationProtocolVersion": (
                FAIR_EVALUATION_PROTOCOL_VERSION
            )
        },
    )
    assert current["protocolAudit"]["status"] == "valid"
    assert current["claim"]["status"] == "preliminary"


def test_fresh_arm_repository_refreshes_copied_git_index(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "Benchmark"],
        ["git", "config", "user.email", "benchmark@example.invalid"],
    ):
        assert run_command(argv, cwd=base, timeout=30).returncode == 0
    source = base / "module.py"
    source.write_text("value = 1\n", encoding="utf-8")
    assert run_command(
        ["git", "add", "module.py"],
        cwd=base,
        timeout=30,
    ).returncode == 0
    assert run_command(
        ["git", "commit", "-qm", "base"],
        cwd=base,
        timeout=30,
    ).returncode == 0

    arm = fresh_arm_repository(base, tmp_path / "arm")
    (arm / "module.py").write_text("value = 2\n", encoding="utf-8")
    patch = run_command(
        ["git", "diff", "HEAD", "--"],
        cwd=arm,
        timeout=30,
    ).stdout
    oracle = fresh_arm_repository(base, tmp_path / "oracle")
    applied = run_command(
        ["git", "apply", "--index", "-"],
        cwd=oracle,
        timeout=30,
        input_text=patch,
    )
    assert applied.returncode == 0
    assert (oracle / "module.py").read_text(encoding="utf-8") == "value = 2\n"


def test_source_revision_uses_package_checkout_and_reports_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(
        "mlx_swarm.evaluation._best_effort_output",
        lambda argv, cwd: "a" * 40,
    )

    def run(argv: list[str], *, cwd: Path, **kwargs: Any) -> CommandResult:
        calls.append((argv, cwd))
        return CommandResult(
            argv=tuple(argv),
            returncode=0,
            stdout=" M src/mlx_swarm/evaluation.py\n",
            stderr="",
            elapsed_seconds=0.01,
        )

    monkeypatch.setattr("mlx_swarm.evaluation.run_command", run)
    source = mlx_swarm_source_revision()
    assert source["commit"] == "a" * 40
    assert source["dirty"] is True
    assert calls[0][0] == [
        "git",
        "status",
        "--porcelain",
        "--untracked-files=all",
    ]
    assert (calls[0][1] / "pyproject.toml").is_file()


def test_prepare_rejects_dirty_unpinned_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(_write_config(tmp_path))
    profile = load_evaluation_profile(_write_profile(tmp_path))
    store = EvaluationStore(config, root=tmp_path / "evaluations")
    monkeypatch.setattr(
        "mlx_swarm.evaluation.mlx_swarm_source_revision",
        lambda: {"commit": "a" * 40, "dirty": True},
    )
    with pytest.raises(EvaluationError, match="source is dirty"):
        store.prepare(profile)


def test_benchmark_command_parser_has_no_shell_surface(tmp_path: Path) -> None:
    commands = tmp_path / "run_test.sh"
    commands.write_text(
        "pytest -q tests/test_bug.py\npython -m pytest -q\n",
        encoding="utf-8",
    )
    assert parse_benchmark_commands(commands) == [
        ["pytest", "-q", "tests/test_bug.py"],
        ["python", "-m", "pytest", "-q"],
    ]

    commands.write_text("pytest -q && touch escaped\n", encoding="utf-8")
    with pytest.raises(EvaluationError, match="shell syntax"):
        parse_benchmark_commands(commands)


def test_build_ext_zero_parallelism_is_normalized_deterministically() -> None:
    assert normalize_setup_parallelism([
        "/environment/bin/python",
        "setup.py",
        "build_ext",
        "--inplace",
        "-j",
        "0",
    ])[-1] == "4"
    untouched = ["/environment/bin/python", "-m", "pytest"]
    assert normalize_setup_parallelism(untouched) == untouched


def test_bugsinpy_info_allows_portable_assignment_spacing(
    tmp_path: Path,
) -> None:
    from mlx_swarm.evaluation import parse_info_file

    path = tmp_path / "bug.info"
    path.write_text(
        'python_version = "3.11"\n'
        'buggy_commit_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n',
        encoding="utf-8",
    )
    assert parse_info_file(path)["python_version"] == "3.11"


def test_patch_contract_rejects_rename_traversal_and_duplicate_paths() -> None:
    patch = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1 +1 @@\n-a = 1\n+a = 2\n"
    )
    assert patch_metadata(patch)["changedLines"] == 2
    with pytest.raises(EvaluationError, match="rename/copy"):
        patch_metadata("diff --git a/src/a.py b/src/b.py\n")
    with pytest.raises(EvaluationError, match="escapes"):
        patch_metadata("diff --git a/../secret b/../secret\n")
    with pytest.raises(EvaluationError, match="unique"):
        patch_metadata(
            "diff --git a/src/a.py b/src/a.py\n"
            "diff --git a/src/a.py b/src/a.py\n"
        )


def test_selection_is_seeded_disjoint_balanced_and_project_bounded(
    tmp_path: Path,
) -> None:
    profile = load_evaluation_profile(_write_profile(tmp_path))
    candidates = [
        _case(f"{project}-{index}", project, stratum)
        for project in ("alpha", "beta", "gamma")
        for index, stratum in enumerate(
            ("small", "medium", "large", "small", "medium", "large"),
            start=1,
        )
    ]
    pilot, measured = select_cases(candidates, profile)
    pilot_again, measured_again = select_cases(candidates, profile)
    assert [case["caseId"] for case in pilot] == [
        case["caseId"] for case in pilot_again
    ]
    assert [case["caseId"] for case in measured] == [
        case["caseId"] for case in measured_again
    ]
    assert len(pilot) == 3
    assert len(measured) == 6
    assert not ({case["caseId"] for case in pilot} & {
        case["caseId"] for case in measured
    })
    assert {
        stratum: sum(
            case["reference"]["stratum"] == stratum for case in measured
        )
        for stratum in ("small", "medium", "large")
    } == {"small": 2, "medium": 2, "large": 2}
    assert {
        project: sum(case["project"] == project for case in measured)
        for project in ("alpha", "beta", "gamma")
    } == {"alpha": 2, "beta": 2, "gamma": 2}


def test_evaluation_case_has_stable_objective() -> None:
    case = evaluation_case(_case("alpha-1", "alpha", "small"), "pilot")
    assert case["phase"] == "pilot"
    assert "alpha-1" in case["objective"]
    with pytest.raises(EvaluationError, match="phase"):
        evaluation_case(case, "warmup")


def test_prepare_replaces_failed_oracle_candidates_before_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(_write_config(tmp_path))
    profile = load_evaluation_profile(_write_profile(tmp_path))
    store = EvaluationStore(config, root=tmp_path / "evaluations")
    storage_checks = 0

    def check_storage(_profile: Any) -> None:
        nonlocal storage_checks
        storage_checks += 1

    monkeypatch.setattr(store, "_check_storage", check_storage)
    candidates = [
        _case(f"{project}-{index}", project, stratum)
        for project in ("alpha", "beta", "gamma")
        for index, stratum in enumerate(
            ("small", "medium", "large", "small", "medium", "large"),
            start=1,
        )
    ]
    failed: list[str] = []
    prepared: set[str] = set()

    class Runner:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def prepare_case(
            self,
            evaluation_dir: Path,
            case: dict[str, Any],
            *,
            retain_mirror: bool = False,
        ) -> dict[str, Any]:
            case_dir = evaluation_dir / "cases" / case["caseId"]
            case_dir.mkdir(parents=True, exist_ok=True)
            if not failed:
                failed.append(case["caseId"])
                raise EvaluationError("buggy snapshot passed")
            (case_dir / "runtime.json").write_text("{}", encoding="utf-8")
            prepared.add(case["caseId"])
            return {}

    def clone(_profile: Any, target: Path) -> Path:
        target.mkdir(parents=True)
        return target

    monkeypatch.setattr(
        "mlx_swarm.evaluation.mlx_swarm_source_revision",
        lambda: {
            "root": str(tmp_path),
            "commit": "a" * 40,
            "dirty": False,
        },
    )
    monkeypatch.setattr(
        "mlx_swarm.evaluation.inspect_container",
        lambda _profile: {"digest": _profile.container.digest},
    )
    monkeypatch.setattr(
        "mlx_swarm.evaluation.inspect_codex_version",
        lambda _profile: _profile.frontier.codex_version,
    )
    monkeypatch.setattr(
        "mlx_swarm.evaluation.enumerate_bugsinpy_candidates",
        lambda *args: list(candidates),
    )
    monkeypatch.setattr(
        "mlx_swarm.evaluation.resolve_case_commits",
        lambda *args: None,
    )
    monkeypatch.setattr("mlx_swarm.evaluation.EvaluationRunner", Runner)
    monkeypatch.setattr(
        "mlx_swarm.evaluation.environment_fingerprint",
        lambda *args, **kwargs: {
            "profileSha256": "digest",
            "mlxSwarmCommit": "a" * 40,
        },
    )

    detail = store.prepare(profile, clone=clone)
    frozen_ids = {
        case["caseId"] for case in detail["suite"]["cases"]
    }
    assert failed[0] not in frozen_ids
    exclusions = json.loads(
        (
            store._dir(detail["evaluation"]["evaluationId"])
            / "preparation-exclusions.json"
        ).read_text(encoding="utf-8")
    )
    assert exclusions["cases"][0]["caseId"] == failed[0]
    assert len(frozen_ids) == 9
    # One initial check, one after each newly prepared runtime, and one final
    # check. Runtimes reused after reselection are not rescanned.
    assert storage_checks == len(prepared) + 2


def test_prepare_resumes_unsealed_evaluation_and_reuses_case_runtimes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(_write_config(tmp_path))
    profile = load_evaluation_profile(_write_profile(tmp_path))
    store = EvaluationStore(config, root=tmp_path / "evaluations")
    evaluation_id = "interrupted-study"
    evaluation_dir = store.root / evaluation_id
    benchmark = evaluation_dir / "benchmark"
    benchmark.mkdir(parents=True)
    (evaluation_dir / "profile.snapshot.json").write_text(
        json.dumps(profile_payload(profile)),
        encoding="utf-8",
    )

    candidates = [
        _case(f"{project}-{index}", project, stratum)
        for project in ("alpha", "beta", "gamma")
        for index, stratum in enumerate(
            ("small", "medium", "large", "small", "medium", "large"),
            start=1,
        )
    ]
    excluded_id = candidates[0]["caseId"]
    (evaluation_dir / "preparation-exclusions.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "cases": [{
                "caseId": excluded_id,
                "reason": "previous oracle failure",
            }],
            "recordedAt": "earlier",
        }),
        encoding="utf-8",
    )
    eligible = [
        value for value in candidates
        if value["caseId"] != excluded_id
    ]
    pilot, measured = select_cases(eligible, profile)
    reused_id = (pilot + measured)[0]["caseId"]
    reused_runtime = (
        evaluation_dir / "cases" / reused_id / "runtime.json"
    )
    reused_runtime.parent.mkdir(parents=True)
    reused_runtime.write_text("{}", encoding="utf-8")
    observed: list[tuple[str, bool]] = []

    class Runner:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def prepare_case(
            self,
            root: Path,
            case: dict[str, Any],
            *,
            retain_mirror: bool = False,
        ) -> dict[str, Any]:
            runtime = root / "cases" / case["caseId"] / "runtime.json"
            observed.append((case["caseId"], runtime.is_file()))
            runtime.parent.mkdir(parents=True, exist_ok=True)
            runtime.write_text("{}", encoding="utf-8")
            return {}

    monkeypatch.setattr(store, "_check_storage", lambda _profile: None)
    monkeypatch.setattr(
        "mlx_swarm.evaluation.mlx_swarm_source_revision",
        lambda: {
            "root": str(tmp_path),
            "commit": "a" * 40,
            "dirty": False,
        },
    )
    monkeypatch.setattr(
        "mlx_swarm.evaluation.inspect_container",
        lambda _profile: {"digest": _profile.container.digest},
    )
    monkeypatch.setattr(
        "mlx_swarm.evaluation.inspect_codex_version",
        lambda _profile: _profile.frontier.codex_version,
    )
    monkeypatch.setattr(
        "mlx_swarm.evaluation.enumerate_bugsinpy_candidates",
        lambda *args: list(candidates),
    )
    monkeypatch.setattr(
        "mlx_swarm.evaluation.resolve_case_commits",
        lambda *args: None,
    )
    monkeypatch.setattr("mlx_swarm.evaluation.EvaluationRunner", Runner)
    monkeypatch.setattr(
        "mlx_swarm.evaluation.environment_fingerprint",
        lambda *args, **kwargs: {
            "profileSha256": "digest",
            "mlxSwarmCommit": "a" * 40,
        },
    )

    detail = store.prepare(
        profile,
        clone=lambda *_args: pytest.fail(
            "resume must not clone benchmark metadata"
        ),
        resume_evaluation_id=evaluation_id,
    )

    assert detail["evaluation"]["evaluationId"] == evaluation_id
    assert (reused_id, True) in observed
    assert excluded_id not in {
        case["caseId"] for case in detail["suite"]["cases"]
    }
    assert not benchmark.exists()
    with pytest.raises(EvaluationError, match="immutable"):
        store.prepare(
            profile,
            resume_evaluation_id=evaluation_id,
        )


def test_prepare_resumes_after_suite_write_before_sensitive_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(_write_config(tmp_path))
    profile = load_evaluation_profile(_write_profile(tmp_path))
    store = EvaluationStore(config, root=tmp_path / "evaluations")
    evaluation_id = "interrupted-finalization"
    evaluation_dir = store.root / evaluation_id
    benchmark = evaluation_dir / "benchmark"
    mirrors = evaluation_dir / "cache" / "repositories"
    benchmark.mkdir(parents=True)
    mirrors.mkdir(parents=True)
    (benchmark / ".DS_Store").write_text("race", encoding="utf-8")
    (mirrors / "future.git").mkdir()
    (evaluation_dir / "profile.snapshot.json").write_text(
        json.dumps(profile_payload(profile)),
        encoding="utf-8",
    )
    candidates = [
        _case(f"{project}-{index}", project, stratum)
        for project in ("alpha", "beta", "gamma")
        for index, stratum in enumerate(
            ("small", "medium", "large", "small", "medium", "large"),
            start=1,
        )
    ]
    pilot, measured = select_cases(candidates, profile)
    cases = [
        evaluation_case(candidate, phase)
        for phase, values in (("pilot", pilot), ("measured", measured))
        for candidate in values
    ]
    suite = {
        "schemaVersion": 1,
        "suiteId": evaluation_id,
        "profileId": profile.profile_id,
        "benchmark": {
            "name": "BugsInPy",
            "repository": profile.benchmark_repository,
            "revision": profile.benchmark_revision,
        },
        "seed": profile.seed,
        "createdAt": "earlier",
        "cases": cases,
    }
    (evaluation_dir / "suite.json").write_text(
        json.dumps(suite),
        encoding="utf-8",
    )
    for case in cases:
        runtime = (
            evaluation_dir
            / "cases"
            / case["caseId"]
            / "runtime.json"
        )
        runtime.parent.mkdir(parents=True)
        runtime.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(store, "_check_storage", lambda _profile: None)
    monkeypatch.setattr(
        "mlx_swarm.evaluation.mlx_swarm_source_revision",
        lambda: {
            "root": str(tmp_path),
            "commit": "a" * 40,
            "dirty": False,
        },
    )
    monkeypatch.setattr(
        "mlx_swarm.evaluation.inspect_container",
        lambda _profile: {"digest": _profile.container.digest},
    )
    monkeypatch.setattr(
        "mlx_swarm.evaluation.inspect_codex_version",
        lambda _profile: _profile.frontier.codex_version,
    )
    monkeypatch.setattr(
        "mlx_swarm.evaluation.enumerate_bugsinpy_candidates",
        lambda *_args: pytest.fail(
            "written suite must finalize without benchmark metadata"
        ),
    )
    monkeypatch.setattr(
        "mlx_swarm.evaluation.environment_fingerprint",
        lambda *args, **kwargs: {
            "profileSha256": "digest",
            "mlxSwarmCommit": "a" * 40,
        },
    )

    detail = store.prepare(
        profile,
        resume_evaluation_id=evaluation_id,
    )
    assert detail["evaluation"]["status"] == "prepared"
    assert not benchmark.exists()
    assert not mirrors.exists()
    assert (evaluation_dir / "environment.json").is_file()


def test_codex_usage_aggregates_every_completed_turn() -> None:
    payload = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "x"}),
        "not-json",
        json.dumps({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 20,
            },
        }),
        json.dumps({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 50,
                "cached_input_tokens": 10,
                "output_tokens": 5,
                "reasoning_output_tokens": 3,
                "total_tokens": 55,
            },
        }),
    ])
    usage = parse_codex_usage_jsonl(payload)
    assert usage["usageStatus"] == "reported"
    assert usage["turns"] == 2
    assert usage["promptTokens"] == 150
    assert usage["cachedInputTokens"] == 90
    assert usage["completionTokens"] == 25
    assert usage["totalTokens"] == 175
    assert usage["malformedLines"] == 1


def test_missing_codex_usage_is_explicitly_unavailable() -> None:
    usage = parse_codex_usage_jsonl(
        json.dumps({"type": "item.completed", "item": {}})
    )
    assert usage["usageStatus"] == "unavailable"
    assert usage["totalTokens"] is None
    combined = usage_with_phases([("planning", usage)])
    assert combined["usageStatus"] == "unavailable"
    assert combined["totalTokens"] is None


def test_hermes_usage_requires_complete_matching_receipt() -> None:
    receipt = {
        "input_tokens": 16978,
        "output_tokens": 6,
        "cache_read_tokens": 12000,
        "cache_write_tokens": 0,
        "reasoning_tokens": 2,
        "total_tokens": 16984,
        "api_calls": 1,
        "model": "glm-5.2",
        "provider": "ollama-cloud",
        "completed": True,
        "failed": False,
    }
    usage = parse_hermes_usage_json(
        json.dumps(receipt),
        expected_provider="ollama-cloud",
        expected_model="glm-5.2",
    )
    assert usage["usageStatus"] == "reported"
    assert usage["promptTokens"] == 16978
    assert usage["cachedInputTokens"] == 12000
    assert usage["completionTokens"] == 6
    assert usage["reasoningTokens"] == 2
    assert usage["totalTokens"] == 16984
    assert usage["turns"] == 1

    invalid_receipts = []
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
        "api_calls",
        "completed",
        "failed",
    ):
        invalid = dict(receipt)
        del invalid[key]
        invalid_receipts.append(invalid)
    invalid_receipts.extend([
        {**receipt, "total_tokens": 1},
        {**receipt, "api_calls": 0},
        {**receipt, "api_calls": 2},
        {**receipt, "completed": False},
        {**receipt, "failed": True},
        {**receipt, "model": "glm-4.7"},
        {**receipt, "provider": "other"},
    ])
    for invalid in invalid_receipts:
        usage = parse_hermes_usage_json(
            json.dumps(invalid),
            expected_provider="ollama-cloud",
            expected_model="glm-5.2",
        )
        assert usage["usageStatus"] == "unavailable"
        assert usage["totalTokens"] is None


def test_response_only_prompt_and_single_outer_fence_contract() -> None:
    prompt = frontier_alone_response_prompt("TASK PACKET")
    assert "no terminal, file, or browser tools" in prompt
    assert "edit-manifest-v1" in prompt
    assert prompt.endswith("TASK PACKET")
    assert strip_one_json_fence("```json\n{\"edits\": []}\n```") == (
        '{"edits": []}'
    )
    assert strip_one_json_fence("```\n{\"edits\": []}\n```") == (
        '{"edits": []}'
    )
    assert strip_one_json_fence("```json\n{\"edits\": []}") == (
        '```json\n{"edits": []}'
    )
    nested = "```json\n```json\n{}\n```\n```"
    assert strip_one_json_fence(nested) == "```json\n{}\n```"


def test_frontier_edit_manifest_materializes_only_approved_paths(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    (repository / "src").mkdir(parents=True)
    target = repository / "src" / "value.py"
    target.write_text(
        "def value():\n    return 1\n",
        encoding="utf-8",
    )
    run_command(["git", "init", "-q"], cwd=repository, timeout=10)
    manifest = json.dumps({
        "edits": [{
            "path": "src/value.py",
            "old": "    return 1",
            "new": "    return 2",
        }],
    })
    diff = materialize_frontier_edit_manifest(
        manifest,
        repository=repository,
        approved_write_roots=["src"],
    )
    assert "diff --git a/src/value.py b/src/value.py" in diff
    checked = run_command(
        ["git", "apply", "--check", "--recount", "-"],
        cwd=repository,
        timeout=10,
        input_text=diff,
    )
    assert checked.returncode == 0
    assert target.read_text(encoding="utf-8").endswith("return 1\n")

    escaped = json.dumps({
        "edits": [{
            "path": "../outside.py",
            "old": "before",
            "new": "after",
        }],
    })
    with pytest.raises(EvaluationError):
        materialize_frontier_edit_manifest(
            escaped,
            repository=repository,
            approved_write_roots=["src"],
        )


def test_arm_result_contract_rejects_unknown_fields_and_mixed_usage() -> None:
    result = _result("alpha-1", "frontier-alone", total_tokens=500)
    assert validate_arm_result(result)["score"] == 1
    result["unexpected"] = True
    with pytest.raises(EvaluationError, match="unknown fields"):
        validate_arm_result(result)

    result = _result("alpha-1", "frontier-alone", total_tokens=500)
    result["frontierUsage"]["usageStatus"] = "unavailable"
    with pytest.raises(EvaluationError, match="must be null"):
        validate_arm_result(result)


def test_bootstrap_and_claim_gate_are_deterministic() -> None:
    first = bootstrap_mean_interval([100, 120, 80], seed=7, samples=500)
    second = bootstrap_mean_interval([100, 120, 80], seed=7, samples=500)
    assert first == second
    assert first[0] > 0

    suite = {
        "suiteId": "study-1",
        "seed": 20260728,
        "cases": [
            {"caseId": f"alpha-{index}", "project": "alpha", "phase": "measured"}
            for index in range(1, 31)
        ],
    }
    results = [
        result
        for index in range(1, 31)
        for result in (
            _result(f"alpha-{index}", "frontier-alone", total_tokens=1_000),
            _result(f"alpha-{index}", "mlx-swarm", total_tokens=400),
        )
    ]
    summary = aggregate_results(suite, results, bootstrap_samples=500)
    assert summary["claim"]["status"] == "established"
    assert summary["paired"]["frontierTokensSaved"] == 18_000
    assert summary["mlxSwarm"]["localTokens"] == 4_500


def test_preliminary_subset_is_balanced_and_never_enables_claim() -> None:
    pilot_cases = [
        {
            **_case(f"pilot-{index}", f"pilot-{index}", "small"),
            "phase": "pilot",
        }
        for index in range(1, 3)
    ]
    strata = ("small", "small", "medium", "medium", "large", "large")
    measured_cases = [
        {
            **_case(f"case-{index}", f"project-{index}", stratum),
            "phase": "measured",
        }
        for index, stratum in enumerate(strata, 1)
    ]
    suite = {
        "schemaVersion": 1,
        "suiteId": "source-study",
        "profileId": "profile",
        "benchmark": {
            "name": "BugsInPy",
            "repository": "https://example.invalid/benchmark.git",
            "revision": "a" * 40,
        },
        "seed": 20260728,
        "createdAt": "now",
        "cases": [*pilot_cases, *measured_cases],
    }
    results = [
        result
        for case in suite["cases"]
        for result in (
            _result(
                case["caseId"],
                "frontier-alone",
                total_tokens=1_000,
                phase=case["phase"],
            ),
            _result(
                case["caseId"],
                "mlx-swarm",
                total_tokens=400,
                phase=case["phase"],
            ),
        )
    ]
    subset, selected, calibration = preliminary_study_subset(
        suite,
        results,
    )
    assert subset["suiteId"] == "source-study-preliminary-6"
    assert len(calibration) == 4
    assert len(selected) == 16
    measured = [
        case for case in subset["cases"] if case["phase"] == "measured"
    ]
    assert len({case["project"] for case in measured}) == 6
    assert [
        case["reference"]["stratum"] for case in measured
    ].count("small") == 2
    summary = aggregate_results(subset, selected, bootstrap_samples=100)
    assert summary["claim"]["status"] == "tradeoff_measured"


def test_claim_gate_fails_on_score_regression_or_missing_usage() -> None:
    suite = {
        "suiteId": "study-2",
        "seed": 1,
        "cases": [
            {"caseId": "alpha-1", "project": "alpha", "phase": "measured"}
        ],
    }
    frontier = _result("alpha-1", "frontier-alone", total_tokens=1_000)
    swarm = _result(
        "alpha-1",
        "mlx-swarm",
        total_tokens=100,
        score=0,
    )
    summary = aggregate_results(
        suite,
        [frontier, swarm],
        bootstrap_samples=100,
    )
    assert summary["claim"]["status"] == "tradeoff_measured"

    swarm = _result("alpha-1", "mlx-swarm", total_tokens=100)
    swarm["frontierUsage"] = usage_with_phases([(
        "planning",
        {
            "usageStatus": "unavailable",
            "turns": 0,
            "promptTokens": None,
            "cachedInputTokens": None,
            "completionTokens": None,
            "reasoningTokens": None,
            "totalTokens": None,
            "malformedLines": 0,
        },
    )])
    summary = aggregate_results(
        suite,
        [frontier, swarm],
        bootstrap_samples=100,
    )
    assert summary["paired"]["allUsageValid"] is False
    assert summary["claim"]["status"] == "tradeoff_measured"


def test_readme_renderer_is_deterministic_and_checkable(tmp_path: Path) -> None:
    suite = {
        "suiteId": "study-3",
        "seed": 2,
        "cases": [
            {"caseId": "alpha-1", "project": "alpha", "phase": "measured"}
        ],
    }
    summary = aggregate_results(
        suite,
        [
            _result("alpha-1", "frontier-alone", total_tokens=1_000),
            _result(
                "alpha-1",
                "mlx-swarm",
                total_tokens=400,
                elapsed=90,
            ),
        ],
        bootstrap_samples=100,
    )
    rendered = render_readme_economics(summary)
    assert "| [alpha-1](benchmarks/results/study-3/cases/alpha-1.json)" in rendered
    assert "01:00" in rendered
    assert "01:30" in rendered
    readme = tmp_path / "README.md"
    readme.write_text("# Project\n", encoding="utf-8")
    assert update_readme_economics(readme, rendered) is True
    assert update_readme_economics(readme, rendered, check=True) is False
    with pytest.raises(EvaluationError, match="out of date"):
        update_readme_economics(
            readme,
            rendered.replace("01:30", "01:31"),
            check=True,
        )


def test_public_suite_omits_fixed_patch_future_commit_and_commands() -> None:
    case = {
        **_case("alpha-1", "alpha", "small"),
        "phase": "measured",
        "objective": "Fix it",
    }
    suite = {
        "schemaVersion": 1,
        "suiteId": "study-4",
        "profileId": "profile",
        "benchmark": {
            "name": "BugsInPy",
            "repository": "https://example.invalid/benchmark.git",
            "revision": "a" * 40,
        },
        "seed": 1,
        "createdAt": "now",
        "cases": [case],
    }
    sanitized = sanitize_suite(suite)
    public_case = sanitized["cases"][0]
    assert "fixedCommit" not in public_case
    assert "verificationArgv" not in public_case
    assert "requirements" not in public_case
    assert public_case["reference"]["sha256"] == case["reference"]["sha256"]


def test_preparation_sources_are_removed_before_model_execution(
    tmp_path: Path,
) -> None:
    evaluation = tmp_path / "evaluation"
    benchmark = evaluation / "benchmark"
    mirrors = evaluation / "cache" / "repositories"
    runtime = evaluation / "cache" / "environments"
    benchmark.mkdir(parents=True)
    mirrors.mkdir(parents=True)
    runtime.mkdir(parents=True)
    (benchmark / "reference.patch").write_text("secret", encoding="utf-8")
    (mirrors / "project.git").mkdir()
    (runtime / "ready.json").write_text("{}", encoding="utf-8")

    remove_sensitive_preparation_sources(evaluation)
    assert not benchmark.exists()
    assert not mirrors.exists()
    assert runtime.is_dir()


def test_preparation_cleanup_retries_transient_directory_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation = tmp_path / "evaluation"
    benchmark = evaluation / "benchmark"
    mirrors = evaluation / "cache" / "repositories"
    benchmark.mkdir(parents=True)
    mirrors.mkdir(parents=True)
    (benchmark / ".DS_Store").write_text("race", encoding="utf-8")
    calls = 0
    real_rmtree = shutil.rmtree

    def flaky_rmtree(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(66, "Directory not empty", path)
        real_rmtree(path)

    monkeypatch.setattr(
        "mlx_swarm.evaluation.shutil.rmtree",
        flaky_rmtree,
    )
    remove_sensitive_preparation_sources(evaluation)
    assert calls == 3
    assert not benchmark.exists()
    assert not mirrors.exists()


def test_store_exports_immutable_sanitized_evidence_and_check(
    tmp_path: Path,
) -> None:
    config = load_config(_write_config(tmp_path))
    store = EvaluationStore(config, root=tmp_path / "evaluations")
    evaluation_id = "study-5"
    root = store.root / evaluation_id
    root.mkdir()
    case = {
        **_case("alpha-1", "alpha", "small"),
        "phase": "measured",
        "objective": "Fix it",
    }
    suite = {
        "schemaVersion": 1,
        "suiteId": evaluation_id,
        "profileId": "test-study",
        "benchmark": {
            "name": "BugsInPy",
            "repository": "https://example.invalid/benchmark.git",
            "revision": "a" * 40,
        },
        "seed": 7,
        "createdAt": "now",
        "cases": [case],
    }
    summary = aggregate_results(
        suite,
        [
            _result("alpha-1", "frontier-alone", total_tokens=900),
            _result("alpha-1", "mlx-swarm", total_tokens=300),
        ],
        bootstrap_samples=100,
    )
    (root / "evaluation.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "evaluationId": evaluation_id,
            "status": "completed",
            "pilotStatus": "completed",
            "measuredStatus": "completed",
            "createdAt": "now",
            "updatedAt": "now",
            "results": {},
        }),
        encoding="utf-8",
    )
    (root / "suite.json").write_text(json.dumps(suite), encoding="utf-8")
    (root / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (root / "environment.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "localModel": {"path": "/private/model", "fingerprint": "x"},
        }),
        encoding="utf-8",
    )
    export = tmp_path / "public" / evaluation_id
    report = store.report(evaluation_id, export)
    assert report["summary"]["completePairs"] == 1
    assert store.report(evaluation_id, export, check=True)["summary"] == report["summary"]
    assert json.loads(
        (export / "study.json").read_text(encoding="utf-8")
    )["environment"]["localModel"]["path"] is None
    (export / "report.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(EvaluationError, match="out of date"):
        store.report(evaluation_id, export, check=True)


def test_case_lock_rejects_concurrency_and_releases(tmp_path: Path) -> None:
    with exclusive_case_lock(tmp_path, "alpha-1"):
        with pytest.raises(EvaluationError, match="already locked"):
            with exclusive_case_lock(tmp_path, "alpha-1"):
                pass
    with exclusive_case_lock(tmp_path, "alpha-1"):
        assert (tmp_path / "locks" / "alpha-1.lock").is_file()
    assert not (tmp_path / "locks" / "alpha-1.lock").exists()


def test_container_digest_is_verified_not_inferred(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = load_evaluation_profile(_write_profile(tmp_path))
    digest = profile.container.digest
    monkeypatch.setattr(
        "mlx_swarm.evaluation.run_command",
        lambda *args, **kwargs: CommandResult(
            argv=("docker", "image", "inspect"),
            returncode=0,
            stdout=json.dumps([{
                "Id": digest,
                "Architecture": "amd64",
                "Os": "linux",
                "Size": 123,
            }]),
            stderr="",
            elapsed_seconds=0.01,
        ),
    )
    assert inspect_container(profile)["digest"] == digest

    monkeypatch.setattr(
        "mlx_swarm.evaluation.run_command",
        lambda *args, **kwargs: CommandResult(
            argv=("docker", "image", "inspect"),
            returncode=0,
            stdout=json.dumps([{
                "Id": "sha256:" + "c" * 64,
                "Architecture": "amd64",
                "Os": "linux",
                "Size": 123,
            }]),
            stderr="",
            elapsed_seconds=0.01,
        ),
    )
    with pytest.raises(EvaluationError, match="digest mismatch"):
        inspect_container(profile)

    monkeypatch.setattr(
        "mlx_swarm.evaluation.run_command",
        lambda *args, **kwargs: CommandResult(
            argv=("docker", "image", "inspect"),
            returncode=0,
            stdout=json.dumps([{
                "Id": digest,
                "Architecture": "arm64",
                "Os": "linux",
                "Size": 123,
            }]),
            stderr="",
            elapsed_seconds=0.01,
        ),
    )
    with pytest.raises(EvaluationError, match="platform mismatch"):
        inspect_container(profile)


def test_container_runtime_is_confined_and_network_is_explicit(
    tmp_path: Path,
) -> None:
    evaluation_root = tmp_path / "evaluation"
    workspace = evaluation_root / "cases" / "alpha-1"
    workspace.mkdir(parents=True)
    argv = docker_runtime_argv(
        image="benchmark@sha256:" + "b" * 64,
        platform_name="linux/amd64",
        evaluation_root=evaluation_root,
        cwd=workspace,
        argv=["python", "-m", "pytest"],
        network="none",
        extra_env=("CCACHE_DISABLE=true",),
    )
    assert argv[:2] == ["docker", "run"]
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--platform") + 1] == "linux/amd64"
    assert argv[argv.index("--name") + 1].startswith("mlx-swarm-eval-")
    assert argv[argv.index("--label") + 1] == "mlx-swarm.evaluation=true"
    assert "--cap-drop" in argv
    assert "no-new-privileges" in argv
    assert "CC=ccache gcc" in argv
    assert "CXX=ccache g++" in argv
    assert "UV_CACHE_DIR=/tmp/uv-cache" in argv
    assert argv[argv.index("CCACHE_DISABLE=true") - 1] == "--env"
    assert argv[-3:] == ["python", "-m", "pytest"]
    assert container_path(workspace, evaluation_root) == (
        "/evaluation/cases/alpha-1"
    )
    with pytest.raises(EvaluationError, match="escapes"):
        container_path(tmp_path / "outside", evaluation_root)
    with pytest.raises(EvaluationError, match="network"):
        docker_runtime_argv(
            image="benchmark",
            platform_name="linux/amd64",
            evaluation_root=evaluation_root,
            cwd=workspace,
            argv=["python"],
            network="host",
        )
    with pytest.raises(EvaluationError, match="environment"):
        docker_runtime_argv(
            image="benchmark",
            platform_name="linux/amd64",
            evaluation_root=evaluation_root,
            cwd=workspace,
            argv=["python"],
            network="none",
            extra_env=("BAD=value\nINJECTED=true",),
        )


def test_timed_out_container_cleanup_targets_only_generated_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        "mlx_swarm.evaluation.subprocess.run",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )
    generated = "mlx-swarm-eval-" + "a" * 20
    _remove_timed_out_docker_container([
        "docker",
        "run",
        "--name",
        generated,
        "image",
    ])
    assert calls[0][0] == ["docker", "rm", "--force", generated]
    assert calls[0][1]["shell"] is False

    _remove_timed_out_docker_container([
        "docker",
        "run",
        "--name",
        "operator-container",
        "image",
    ])
    assert len(calls) == 1


def test_resolved_dependencies_cannot_escape_frozen_constraints() -> None:
    assert validate_resolved_dependencies(
        "pytest==8.3.0\npip==23.3.2\n",
        ["pytest==8.3.0"],
        ["pip==23.3.2"],
    ) == ["pip==23.3.2", "pytest==8.3.0"]
    with pytest.raises(EvaluationError, match="escaped"):
        validate_resolved_dependencies(
            "pytest==8.4.0\n",
            ["pytest==8.3.0"],
            ["pip==23.3.2"],
        )
    with pytest.raises(EvaluationError, match="exact registry pin"):
        validate_resolved_dependencies(
            "editable @ file:///workspace\n",
            ["pytest==8.3.0"],
            ["pip==23.3.2"],
        )


def test_repository_symlinks_remain_internal(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    target = repository / "docs" / "source.rst"
    target.parent.mkdir(parents=True)
    target.write_text("source", encoding="utf-8")
    (repository / "source.rst").symlink_to("docs/source.rst")
    validate_repository_symlinks(repository)

    (repository / "escape").symlink_to("../../outside")
    with pytest.raises(EvaluationError, match="escapes"):
        validate_repository_symlinks(repository)


def test_fixed_test_support_copies_only_imported_helpers_without_future_source(
    tmp_path: Path,
) -> None:
    fixed = tmp_path / "fixed"
    buggy = tmp_path / "buggy"
    (fixed / "tests" / "helpers").mkdir(parents=True)
    (buggy / "tests").mkdir(parents=True)
    (fixed / "tests" / "test_bug.py").write_text(
        "from .helpers import value\nfrom .existing import current\n",
        encoding="utf-8",
    )
    (fixed / "tests" / "helpers" / "__init__.py").write_text(
        "value = 1\n",
        encoding="utf-8",
    )
    (fixed / "tests" / "existing.py").write_text(
        "current = 'fixed support'\n",
        encoding="utf-8",
    )
    (buggy / "tests" / "existing.py").write_text(
        "current = 'buggy support'\n",
        encoding="utf-8",
    )
    (fixed / "tests" / "unrelated_future.py").write_text(
        "secret = True\n",
        encoding="utf-8",
    )
    (fixed / "tests" / "conftest.py").write_text(
        "FIXED_FIXTURE = True\n",
        encoding="utf-8",
    )
    (buggy / "tests" / "conftest.py").write_text(
        "BUGGY_FIXTURE = True\n",
        encoding="utf-8",
    )
    (fixed / "src").mkdir()
    (fixed / "src" / "future.py").write_text(
        "secret = True\n",
        encoding="utf-8",
    )

    copy_fixed_test_support(fixed, buggy, ["tests/test_bug.py"])
    assert (buggy / "tests" / "test_bug.py").is_file()
    assert (buggy / "tests" / "helpers" / "__init__.py").is_file()
    assert (
        buggy / "tests" / "existing.py"
    ).read_text(encoding="utf-8") == "current = 'fixed support'\n"
    assert (
        buggy / "tests" / "conftest.py"
    ).read_text(encoding="utf-8") == "FIXED_FIXTURE = True\n"
    assert not (buggy / "tests" / "unrelated_future.py").exists()
    assert not (buggy / "src" / "future.py").exists()


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["pip", "install", "pytest"], True),
        (["pip3", "install", "pytest"], True),
        (["python", "setup.py", "install"], True),
        (["python", "-m", "pip", "install", "pytest"], True),
        (["python", "setup.py", "build_ext", "--inplace"], False),
        (["touch", "tests/__init__.py"], False),
    ],
)
def test_dependency_install_commands_are_not_worker_controlled(
    argv: list[str],
    expected: bool,
) -> None:
    assert is_dependency_or_project_install(argv) is expected


def test_only_verifier_infrastructure_failure_invalidates_measurement() -> None:
    assert oracle_infrastructure_failure({
        "evidence": (
            "Failed to connect to the Docker API at unix:///missing.sock"
        ),
    }) is not None
    assert oracle_infrastructure_failure({
        "evidence": "ModuleNotFoundError: No module named 'candidate_import'",
    }) is None
    assert oracle_infrastructure_failure({
        "evidence": (
            "Error while finding module specification for "
            "'mlx_swarm.evaluation' (ModuleNotFoundError: "
            "No module named 'mlx_swarm')"
        ),
    }) is not None
    assert oracle_infrastructure_failure({
        "evidence": "FAILED tests/test_bug.py::test_value - AssertionError",
    }) is None


def test_docker_context_is_frozen_for_sanitized_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        monkeypatch.delenv(name, raising=False)
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        **_kwargs: Any,
    ) -> CommandResult:
        calls.append(argv)
        return CommandResult(
            argv=tuple(argv),
            returncode=0,
            stdout=json.dumps([{
                "Endpoints": {
                    "docker": {
                        "Host": "unix:///operator/context/docker.sock",
                    },
                },
            }]),
            stderr="",
            elapsed_seconds=0.01,
        )

    monkeypatch.setattr("mlx_swarm.evaluation.run_command", fake_run)
    assert docker_connection_environment(tmp_path) == {
        "DOCKER_HOST": "unix:///operator/context/docker.sock",
    }
    assert calls == [["docker", "context", "inspect"]]


def test_evaluation_verifier_receives_trusted_package_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_config(_write_config(tmp_path))
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "module.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "verifier.json"
    manifest.write_text("{}", encoding="utf-8")
    destination = tmp_path / "evaluation.json"
    monkeypatch.setattr(
        "mlx_swarm.evaluation.docker_connection_environment",
        lambda _cwd: {"DOCKER_HOST": "unix:///trusted/docker.sock"},
    )

    write_evaluation_config(
        source,
        destination,
        tmp_path / "artifacts",
        manifest,
        repository,
    )

    payload = json.loads(destination.read_text(encoding="utf-8"))
    environment = payload["workspace"]["verificationProfiles"][
        "bugsinpy-acceptance"
    ]["environment"]
    assert environment["DOCKER_HOST"] == "unix:///trusted/docker.sock"
    assert environment["PYTHONPATH"] == str(
        Path(__import__("mlx_swarm").__file__).resolve().parents[1]
    )


def test_local_replay_gate_requires_every_calibration_case() -> None:
    required = ["black-11", "fastapi-6"]
    one_pass = [{
        "caseId": "black-11",
        "status": "completed",
        "score": 1,
    }, {
        "caseId": "fastapi-6",
        "status": "failed",
        "score": 0,
    }]
    assert (
        local_replay_promotion_gate(required, one_pass)["measuredEligible"]
        is False
    )
    both_pass = [
        {
            "caseId": case_id,
            "status": "completed",
            "score": 1,
        }
        for case_id in required
    ]
    gate = local_replay_promotion_gate(required, both_pass)
    assert gate["status"] == "passed"
    assert gate["passedCases"] == required


def test_capability_adapted_replay_never_unlocks_measured_work() -> None:
    gate = capability_diagnostic_gate({
        "status": "passed",
        "measuredEligible": True,
        "requiredCases": ["black-11", "fastapi-6"],
        "passedCases": ["black-11", "fastapi-6"],
    })
    assert gate["capabilityResult"] == "passed"
    assert gate["diagnosticOnly"] is True
    assert gate["measuredEligible"] is False


def test_frozen_prompt_replay_copies_exact_digest_bound_prompt(
    tmp_path: Path,
) -> None:
    prompt = "EXACT FRONTIER-AUTHORED WORKER PROMPT"
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    attempt = tmp_path / "source-attempt.json"
    attempt.write_text(json.dumps({
        "taskId": "repair",
        "prompt": prompt,
        "promptSha256": digest,
    }), encoding="utf-8")
    plan = Plan(
        source=tmp_path / "plan.json",
        plan_id="prompt-replay",
        objective="Replay one prompt",
        context=None,
        tasks=(TaskDef(
            id="repair",
            role="implementation",
            prompt="unused",
        ),),
        raw={},
    )
    session = Session(tmp_path / "session", plan)

    install_frozen_prompt_replay(session, [{
        "taskId": "repair",
        "promptSha256": digest,
        "path": str(attempt),
    }])

    assert session.replay_prompt("repair") == prompt
    copied = session.dir / "prompt-replay" / "repair.txt"
    assert copied.read_text(encoding="utf-8") == prompt


def test_evaluation_write_roots_exclude_tests_docs_dependencies_and_hidden(
    tmp_path: Path,
) -> None:
    for directory in ("src", "tests", "docs", ".cache"):
        (tmp_path / directory).mkdir()
    for filename in ("module.py", "README.md", "pyproject.toml"):
        (tmp_path / filename).write_text("", encoding="utf-8")
    assert evaluation_write_roots(tmp_path) == ["module.py", "src"]


def test_local_swarm_subprocess_contains_no_frontier_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(_write_config(tmp_path))
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class Process:
        pid = 123
        returncode = 0

        def poll(self) -> int:
            return 0

        def communicate(self, timeout: int | None = None):
            return b"done", b""

    def popen(argv: list[str], **kwargs: Any) -> Process:
        calls.append((argv, kwargs))
        return Process()

    monkeypatch.setattr("mlx_swarm.evaluation.subprocess.Popen", popen)
    result = run_swarm_with_synthetic_operator(
        config,
        tmp_path / "plan.json",
        tmp_path / "session",
        2,
        timeout=30,
    )
    argv, kwargs = calls[0]
    assert argv[2:5] == ["mlx_swarm.cli", "--config", str(config.source)]
    assert "codex" not in argv
    assert kwargs["shell"] is False
    assert result.returncode == 0


def _delegation_blueprint(
    *,
    source_label: str = "module.py:L1-L3",
) -> dict[str, Any]:
    return {
        "schemaVersion": 3,
        "planId": "repair-module",
        "objective": "Repair the frozen failure.",
        "diagnosis": {
            "observedFailure": "The frozen assertion receives one.",
            "causalHypothesis": "value() returns the wrong literal.",
            "validationEvidence": "The cited source returns one directly.",
            "falsificationCondition": "The return is produced elsewhere.",
            "evidenceSources": [source_label],
            "candidateChange": "Return two instead of one.",
            "failingPathPrediction": "The assertion receives two.",
            "preservedControlPrediction": "The function signature is unchanged.",
            "minimalityEvidence": "Only the observed literal changes.",
            "changeEvidenceSources": [source_label],
        },
        "edits": [{
            "path": "module.py",
            "sourceLabel": source_label,
            "startLine": 2,
            "endLine": 2,
            "new": "    return 2",
            "mustAdd": ["return 2"],
            "mustRemove": ["return 1"],
        }],
    }


def test_frontier_delegation_blueprint_materializes_strict_worker_plan(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text(
        "def value():\n    return 1\n\n",
        encoding="utf-8",
    )
    task_packet = (
        "SOURCE module.py:L1-L3\n"
        "00001 | def value():\n"
        "00002 |     return 1\n"
        "00003 | \n"
        "END SOURCE module.py:L1-L3\n"
    )
    raw = json.dumps(_delegation_blueprint())
    blueprint = parse_frontier_delegation_blueprint(
        raw,
        objective="Repair the frozen failure.",
        task_packet=task_packet,
        repository=tmp_path,
        approved_write_roots=["module.py"],
        maximum_manifest_characters=3_200,
    )
    plan = materialize_frontier_delegation_plan(
        blueprint,
        task_packet=task_packet,
        repository=tmp_path,
        approved_write_roots=["module.py"],
        max_repair=2,
        max_generation_tokens=800,
    )

    task = plan["tasks"][0]
    assert task["workerOutputProtocol"] == "edit-manifest-v1"
    assert task["allowedPaths"] == ["module.py"]
    assert task["verification"] == ["bugsinpy-acceptance"]
    assert '"path":"module.py"' in task["prompt"]
    assert '"file"' not in task["prompt"]
    assert plan["context"]["authoritativeSources"][0] == {
        "label": "module.py:L1-L3",
        "content": "def value():\n    return 1\n",
    }


def test_frontier_delegation_blueprint_rejects_unknown_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text(
        "def value():\n    return 1\n",
        encoding="utf-8",
    )
    response = json.dumps(_delegation_blueprint(
        source_label="invented.py:L1-L3",
    ))
    with pytest.raises(EvaluationError, match="unknown SOURCE"):
        parse_frontier_delegation_blueprint(
            response,
            objective="Repair the frozen failure.",
            task_packet="SOURCE module.py:L1-L2\n",
            repository=tmp_path,
            approved_write_roots=["module.py"],
            maximum_manifest_characters=3_200,
        )


def test_frontier_delegation_blueprint_normalizes_contained_source_range(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text(
        "def value():\n    return 1\n",
        encoding="utf-8",
    )
    payload = _delegation_blueprint(source_label="module.py:L2-L2")

    parsed = parse_frontier_delegation_blueprint(
        json.dumps(payload),
        objective="Repair the frozen failure.",
        task_packet=(
            "SOURCE module.py:L1-L3\n"
            "00001 | def value():\n"
            "00002 |     return 1\n"
            "END SOURCE module.py:L1-L3\n"
        ),
        repository=tmp_path,
        approved_write_roots=["module.py"],
        maximum_manifest_characters=3_200,
    )

    assert parsed["diagnosis"]["evidenceSources"] == [
        "module.py:L1-L3",
    ]
    assert parsed["diagnosis"]["changeEvidenceSources"] == [
        "module.py:L1-L3",
    ]
    assert parsed["edits"] == [{
        "path": "module.py",
        "old": "    return 1\n",
        "new": "    return 2\n",
    }]


def test_frontier_delegation_blueprint_normalizes_source_display_prefix(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text(
        "def value():\n    return 1\n",
        encoding="utf-8",
    )
    payload = _delegation_blueprint(
        source_label="SOURCE module.py:L1-L3",
    )

    parsed = parse_frontier_delegation_blueprint(
        json.dumps(payload),
        objective="Repair the frozen failure.",
        task_packet=(
            "SOURCE module.py:L1-L3\n"
            "00001 | def value():\n"
            "00002 |     return 1\n"
            "END SOURCE module.py:L1-L3\n"
        ),
        repository=tmp_path,
        approved_write_roots=["module.py"],
        maximum_manifest_characters=3_200,
    )

    assert parsed["diagnosis"]["evidenceSources"] == [
        "module.py:L1-L3",
    ]
    assert parsed["diagnosis"]["changeEvidenceSources"] == [
        "module.py:L1-L3",
    ]
    assert parsed["edits"][0]["old"] == "    return 1\n"


def test_frontier_delegation_blueprint_rejects_range_outside_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text(
        "def value():\n    return 1\n",
        encoding="utf-8",
    )
    payload = _delegation_blueprint()
    payload["edits"][0]["endLine"] = 4

    with pytest.raises(EvaluationError, match="escapes its SOURCE label"):
        parse_frontier_delegation_blueprint(
            json.dumps(payload),
            objective="Repair the frozen failure.",
            task_packet=(
                "SOURCE module.py:L1-L3\n"
                "00001 | def value():\n"
                "00002 |     return 1\n"
                "END SOURCE module.py:L1-L3\n"
            ),
            repository=tmp_path,
            approved_write_roots=["module.py"],
            maximum_manifest_characters=3_200,
        )


def test_frontier_delegation_blueprint_rejects_unproven_change_assertion(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text(
        "def value():\n    return 1\n",
        encoding="utf-8",
    )
    payload = _delegation_blueprint()
    payload["edits"][0]["mustAdd"] = ["return 3"]

    with pytest.raises(EvaluationError, match="mustAdd assertion"):
        parse_frontier_delegation_blueprint(
            json.dumps(payload),
            objective="Repair the frozen failure.",
            task_packet=(
                "SOURCE module.py:L1-L3\n"
                "00001 | def value():\n"
                "00002 |     return 1\n"
                "END SOURCE module.py:L1-L3\n"
            ),
            repository=tmp_path,
            approved_write_roots=["module.py"],
            maximum_manifest_characters=3_200,
        )


def test_frontier_edit_manifest_rejects_new_python_syntax_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text(
        "def value():\n    return 1\n",
        encoding="utf-8",
    )

    with pytest.raises(EvaluationError, match="invalid Python syntax"):
        materialize_frontier_edit_manifest(
            json.dumps({
                "edits": [{
                    "path": "module.py",
                    "old": "    return 1\n",
                    "new": "return 2\n",
                }],
            }),
            repository=tmp_path,
            approved_write_roots=["module.py"],
        )


def test_frontier_edit_manifest_rejects_unresolved_bare_callable(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text(
        "def value(item):\n    return item\n",
        encoding="utf-8",
    )

    with pytest.raises(EvaluationError, match="unresolved bare callable"):
        materialize_frontier_edit_manifest(
            json.dumps({
                "edits": [{
                    "path": "module.py",
                    "old": "    return item\n",
                    "new": "    return invented_helper(item)\n",
                }],
            }),
            repository=tmp_path,
            approved_write_roots=["module.py"],
        )


def test_frontier_edit_manifest_allows_new_call_to_existing_function(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text(
        "def normalize(item):\n"
        "    return str(item)\n\n"
        "def value(item):\n"
        "    return item\n",
        encoding="utf-8",
    )

    diff = materialize_frontier_edit_manifest(
        json.dumps({
            "edits": [{
                "path": "module.py",
                "old": "    return item\n",
                "new": "    return normalize(item)\n",
            }],
        }),
        repository=tmp_path,
        approved_write_roots=["module.py"],
    )

    assert "+    return normalize(item)" in diff


def test_context_ranking_uses_buggy_execution_trace(
    tmp_path: Path,
) -> None:
    source = tmp_path / "module.py"
    source.write_text(
        "\n".join(
            f"value_{index} = {index}"
            for index in range(1, 301)
        )
        + "\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_module.py").write_text(
        "def test_failure():\n    assert False\n",
        encoding="utf-8",
    )
    runtime = {
        "baseSnapshot": str(tmp_path),
        "failureEvidence": "test_failure assertion failed",
        "executedSourceLines": {"module.py": [245, 246]},
    }
    case = {
        "testFiles": ["tests/test_module.py"],
        "verificationArgv": [["pytest", "tests/test_module.py::test_failure"]],
    }

    _tree, context = deterministic_case_context(case, runtime)

    assert "SOURCE module.py:L" in context
    assert "00245 | value_245 = 245" in context


def test_requested_source_windows_prioritize_exact_test_identifier() -> None:
    lines = [
        f"def generic_case_{index}():\n    assert value == {index}"
        for index in range(1, 301)
    ]
    lines.insert(
        180,
        "def test_comment_contents_are_preserved():\n"
        "    assert comments == expected",
    )

    windows = _requested_source_windows(
        "\n".join(lines).splitlines(),
        (
            "pytest tests/test_black.py::"
            "test_comment_contents_are_preserved"
        ),
        maximum_characters=4_000,
    )

    selected = "\n".join(content for _start, _end, content in windows)
    assert "def test_comment_contents_are_preserved" in selected


def test_traced_function_context_includes_adjacent_causal_helper(
    tmp_path: Path,
) -> None:
    source = tmp_path / "module.py"
    source.write_text(
        "def parse_comments():\n"
        "    return '# type: int'\n"
        "\n"
        "def split_line():\n"
        "    return 'causal split decision'\n",
        encoding="utf-8",
    )

    windows = _rank_traced_function_windows(
        [source],
        ["module.py"],
        "comments type annotation failure",
        executed_lines={"module.py": [1, 2, 4, 5]},
    )

    assert windows
    assert "def split_line" in windows[0][3]


def test_buggy_execution_trace_uses_first_approved_argv_without_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_root = tmp_path / "evaluation"
    workspace = evaluation_root / "cases" / "case" / "base"
    environment = evaluation_root / "cache" / "environment"
    workspace.mkdir(parents=True)
    environment.mkdir(parents=True)
    (workspace / "module.py").write_text(
        "def repair():\n    return 1\n",
        encoding="utf-8",
    )
    profile = load_evaluation_profile(_write_profile(tmp_path))
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> CommandResult:
        calls.append(argv)
        return CommandResult(
            argv=tuple(argv),
            returncode=1,
            stdout=(
                "functions called:\n"
                "filename: /evaluation/cases/case/base/module.py, "
                "modulename: module, funcname: repair\n"
            ),
            stderr="",
            elapsed_seconds=0.1,
            timed_out=False,
        )

    monkeypatch.setattr("mlx_swarm.evaluation.run_command", fake_run)
    traced = collect_buggy_execution_trace(
        {
            "verificationArgv": [
                ["pytest", "tests/test_module.py::test_failure"],
                ["python", "-c", "must-not-run"],
            ],
        },
        evaluation_root=evaluation_root,
        workspace=workspace,
        environment=environment,
        profile=profile,
    )

    assert traced == {"module.py": [1, 2]}
    assert len(calls) == 2
    assert "--network" in calls[0]
    assert "none" in calls[0]
    assert "--module" in calls[0]
    assert "--listfuncs" in calls[0]
    assert "--count" in calls[1]
    assert "pytest" in calls[0]
    assert all("must-not-run" not in call for call in calls)
    assert all(";" not in value for call in calls for value in call)


def test_trace_cover_maps_only_executed_source_lines(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "package" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("first = 1\nsecond = 2\nthird = 3\n")
    cover_root = tmp_path / "cover"
    cover_root.mkdir()
    (cover_root / "package.module.cover").write_text(
        "    2: first = 1\n"
        "       second = 2\n"
        "    1: third = 3\n",
        encoding="utf-8",
    )

    assert _executed_lines_from_trace_cover(
        workspace,
        cover_root,
    ) == {"package/module.py": [1, 3]}


def test_executed_line_map_is_compact_and_production_only() -> None:
    rendered = _render_executed_line_map({
        "module.py": [1, 2, 3, 7, 9, 10],
        "tests/test_module.py": [1, 2],
    }, source_context=(
        "SOURCE module.py:L2-L9\n"
        "00002 | value = 2\n"
        "END SOURCE module.py:L2-L9\n"
    ))

    assert rendered == "- module.py: 2-3,7,9"


def test_runtime_local_evidence_is_ranked_bounded_and_production_only() -> None:
    rendered = _render_runtime_local_evidence(
        [
            {
                "path": "module.py",
                "line": 8,
                "function": "split_line",
                "sample": 1,
                "locals": {
                    "line_str": {
                        "type": "str",
                        "length": 18,
                        "value": "def f(a): # type",
                    },
                    "inside": False,
                },
            },
            {
                "path": "tests/test_module.py",
                "line": 2,
                "function": "test_failure",
                "sample": 1,
                "locals": {"secret": "excluded"},
            },
            {
                "path": "other.py",
                "line": 3,
                "function": "secondary_path",
                "sample": 1,
                "locals": {"state": False},
            },
        ],
        source_context=(
            "SOURCE module.py:L1-L10\n"
            "00008 | value = split_line()\n"
            "END SOURCE module.py:L1-L10\n"
            "SOURCE tests/test_module.py:L1-L3\n"
            "00002 | assert False\n"
            "END SOURCE tests/test_module.py:L1-L3\n"
            "SOURCE other.py:L1-L5\n"
            "00003 | state = False\n"
            "END SOURCE other.py:L1-L5\n"
        ),
        failure_evidence="split_line loses # type comments",
    )

    assert "module.py:L8 split_line" in rendered
    assert "def f(a): # type" in rendered
    assert "tests/test_module.py" not in rendered
    assert "other.py:L3 secondary_path" in rendered


def test_runtime_local_evidence_prioritizes_traceback_function() -> None:
    noisy = [
        {
            "path": "module.py",
            "line": index,
            "function": "parse_comment",
            "sample": 1,
            "locals": {
                "comment": {
                    "type": "str",
                    "length": 11,
                    "value": "# type: int",
                },
            },
        }
        for index in range(1, 80)
    ]
    decisive = {
        "path": "module.py",
        "line": 90,
        "function": "split_line",
        "sample": 1,
        "locals": {
            "line_str": {
                "type": "str",
                "length": 23,
                "value": "def f(a,):  # type: int",
            },
            "inside_brackets": False,
        },
    }

    rendered = _render_runtime_local_evidence(
        [*noisy, decisive],
        source_context=(
            "SOURCE module.py:L1-L100\n"
            "00090 | split_line(value)\n"
            "END SOURCE module.py:L1-L100\n"
        ),
        failure_evidence="Traceback in split_line while preserving # type: int",
    )

    assert rendered.splitlines()[0].startswith(
        "- module.py:L90 split_line"
    )


def test_runtime_local_evidence_surfaces_same_location_call_contrast() -> None:
    rendered = _render_runtime_local_evidence(
        [
            {
                "path": "module.py",
                "line": 12,
                "function": "split_line",
                "sample": 1,
                "locals": {
                    "line_str": {
                        "type": "str",
                        "value": "from typing import Any",
                    },
                    "line": {
                        "type": "Line",
                        "fields": {
                            "comments": {"type": "dict", "length": 0},
                            "should_explode": False,
                        },
                    },
                },
            },
            {
                "path": "module.py",
                "line": 12,
                "function": "split_line",
                "sample": 2,
                "locals": {
                    "line_str": {
                        "type": "str",
                        "value": "def f(a,):  # type: int",
                    },
                    "line": {
                        "type": "Line",
                        "fields": {
                            "comments": {"type": "dict", "length": 1},
                            "should_explode": False,
                        },
                    },
                },
            },
        ],
        source_context=(
            "SOURCE module.py:L1-L20\n"
            "00012 | if is_line_short_enough(line):\n"
            "END SOURCE module.py:L1-L20\n"
        ),
        failure_evidence="def f(a,):  # type: int is not exploded",
    )

    assert rendered.splitlines()[0].startswith(
        "CAUSAL CONTRAST CANDIDATES"
    )
    assert "sample=1 vs sample=2" in rendered
    assert "line.fields.comments.length: 0 -> 1" in rendered
    assert '"from typing import Any" -> "def f(a,):  # type: int"' in rendered
    assert "RAW LOCAL SAMPLES:" in rendered


def test_runtime_local_evidence_preserves_function_diversity_by_source_block(
) -> None:
    records: list[dict[str, Any]] = []
    blocks: list[str] = []
    for block in range(8):
        start = block * 20 + 1
        end = start + 19
        blocks.append(
            f"SOURCE module.py:L{start}-L{end}\n"
            f"{start:05d} | value = {block}\n"
            f"END SOURCE module.py:L{start}-L{end}\n"
        )
        for sample in range(1, 7):
            records.append({
                "path": "module.py",
                "line": start + 1,
                "function": f"noisy_{block}",
                "sample": sample,
                "locals": {
                    "text": {
                        "type": "str",
                        "length": 800,
                        "value": "comment " * 100,
                    },
                },
            })
        records.append({
            "path": "module.py",
            "line": start + 2,
            "function": (
                "split_line" if block == 6 else f"secondary_{block}"
            ),
            "sample": 1,
            "locals": {"branch": False},
        })

    rendered = _render_runtime_local_evidence(
        records,
        source_context="".join(blocks),
        failure_evidence="formatting assertion differs",
    )

    assert "split_line" in rendered
    assert "secondary_7" in rendered


def test_runtime_local_trace_uses_only_first_approved_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_root = tmp_path / "evaluation"
    workspace = evaluation_root / "cases" / "case" / "base"
    environment = evaluation_root / "cache" / "environment"
    workspace.mkdir(parents=True)
    environment.mkdir(parents=True)
    (workspace / "module.py").write_text(
        "def repair():\n    return 1\n",
        encoding="utf-8",
    )
    profile = load_evaluation_profile(_write_profile(tmp_path))
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> CommandResult:
        calls.append(argv)
        container_output = next(
            value for value in argv if value.endswith(".locals.json")
        )
        output = Path(
            str(evaluation_root)
            + container_output.removeprefix("/evaluation")
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({
            "schemaVersion": 1,
            "records": [{
                "path": "module.py",
                "line": 2,
                "function": "repair",
                "sample": 1,
                "locals": {"value": 1},
            }],
        }), encoding="utf-8")
        return CommandResult(
            argv=tuple(argv),
            returncode=1,
            stdout="frozen failure",
            stderr="",
            elapsed_seconds=0.1,
            timed_out=False,
        )

    monkeypatch.setattr("mlx_swarm.evaluation.run_command", fake_run)
    records = collect_buggy_runtime_locals(
        {
            "caseId": "case",
            "verificationArgv": [
                ["pytest", "tests/test_module.py::test_failure"],
                ["python", "-c", "must-not-run"],
            ],
        },
        evaluation_root=evaluation_root,
        workspace=workspace,
        environment=environment,
        profile=profile,
        source_context=(
            "SOURCE module.py:L1-L2\n"
            "00001 | def repair():\n"
            "00002 |     return 1\n"
            "END SOURCE module.py:L1-L2\n"
        ),
    )

    assert records == [{
        "path": "module.py",
        "line": 2,
        "function": "repair",
        "sample": 1,
        "locals": {"value": 1},
    }]
    assert len(calls) == 1
    assert "--network" in calls[0]
    assert "none" in calls[0]
    assert "-m" in calls[0]
    assert "pytest" in calls[0]
    assert "must-not-run" not in calls[0]
    assert all(";" not in value for value in calls[0])


def test_retained_candidate_survives_operator_revert() -> None:
    diff = (
        "diff --git a/module.py b/module.py\n"
        "--- a/module.py\n"
        "+++ b/module.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )
    session = SimpleNamespace(state={"tasks": {
        "repair": {
            "artifactType": "patch",
            "status": "rejected_by_operator",
            "normalizedOutput": diff,
            "artifact": {"sha256": "a" * 64},
        },
    }})

    assert retained_session_candidate_diff(session) == diff


def test_frontier_delegation_prompt_exposes_small_worker_limits() -> None:
    prompt = frontier_delegation_blueprint_prompt(
        "SOURCE module.py:L1-L2\n",
        worker_capabilities={
            "parameterScale": "4B",
            "maxGenerationTokens": 800,
            "delegationLevel": "exact-edit",
        },
    )
    assert '"parameterScale": "4B"' in prompt
    assert '"maxGenerationTokens": 800' in prompt
    assert "must not discover APIs" in prompt
    assert "compact strict JSON" in prompt
    assert '"schemaVersion": 3' in prompt
    assert '"mustAdd"' in prompt
    assert "invalid Python syntax" not in prompt
    assert "Mentally splice new into the complete file" in prompt


def test_directory_size_counts_files_without_following_symlinks(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (tmp_path / "one.bin").write_bytes(b"123")
    (nested / "two.bin").write_bytes(b"4567")
    (nested / "loop").symlink_to(tmp_path, target_is_directory=True)

    assert directory_size(tmp_path) == 7
