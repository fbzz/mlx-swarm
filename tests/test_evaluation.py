"""Tests for the paired BugsInPy economics evaluation."""
# @lat: [[Tests#Economics evaluation]]

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from mlx_swarm.contracts import (
    ContextSource,
    Plan,
    TaskContext,
    TaskDef,
    load_config,
)
from mlx_swarm.evaluation import (
    FAIR_EVALUATION_PROTOCOL_VERSION,
    _remove_timed_out_docker_container,
    CommandResult,
    EvaluationError,
    EvaluationStore,
    aggregate_results,
    apply_protocol_audit,
    bootstrap_mean_interval,
    build_task_packet,
    container_path,
    copy_fixed_test_support,
    docker_runtime_argv,
    empty_local_usage,
    ensure_pair_contract,
    evaluation_write_roots,
    evaluation_case,
    exclusive_case_lock,
    fresh_arm_repository,
    inspect_codex_version,
    inspect_container,
    is_dependency_or_project_install,
    load_evaluation_profile,
    make_arm_result,
    mlx_swarm_source_revision,
    normalize_setup_parallelism,
    oracle_infrastructure_failure,
    parse_benchmark_commands,
    parse_codex_usage_jsonl,
    patch_metadata,
    preliminary_study_subset,
    preliminary_evaluation_profile,
    profile_payload,
    remove_sensitive_preparation_sources,
    render_readme_economics,
    run_command,
    run_swarm_with_synthetic_operator,
    sanitize_suite,
    select_cases,
    split_constraint_text,
    update_readme_economics,
    usage_with_phases,
    validate_arm_result,
    validate_candidate_diff,
    validate_evaluation_plan,
    validate_repository_symlinks,
    validate_resolved_dependencies,
)


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


def test_oracle_dependency_failure_is_not_a_scored_bug() -> None:
    assert oracle_infrastructure_failure({
        "evidence": (
            "ImportError while importing test module\n"
            "ModuleNotFoundError: No module named 'python_toolbox'"
        ),
    }) is not None
    assert oracle_infrastructure_failure({
        "evidence": "FAILED tests/test_bug.py::test_value - AssertionError",
    }) is None


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
