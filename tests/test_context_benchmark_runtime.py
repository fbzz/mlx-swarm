"""Injected-backend tests for the context-benchmark runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mlx_swarm.backend import BatchGenerationError
from mlx_swarm.context_benchmark_runtime import run_benchmark
from mlx_swarm.context_benchmark_score import expected_manifest
from mlx_swarm.contracts import TaskDef


class FakeTokenizer:
    has_chat_template = True

    def apply_chat_template(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        assert kwargs["tokenize"] is False
        return f"T{messages[0]['content']}"

    def encode(self, value: str, **kwargs: Any) -> list[int]:
        assert kwargs.get("add_special_tokens") is False
        return list(range(20 + value.count("def distractor_")))


class FakeBackend:
    instances: list["FakeBackend"] = []

    def __init__(self, config: Any, *, model_path: Path, mode: str = "pass") -> None:
        self.config = config
        self.model_path = model_path
        self.tokenizer = FakeTokenizer()
        self.mode = mode
        self.opened = 0
        self.closed = 0
        self.generate_calls: list[tuple[list[TaskDef], list[str]]] = []
        self.instances.append(self)

    def open(self) -> None:
        self.opened += 1

    def close(self) -> None:
        self.closed += 1

    def generate(
        self,
        tasks: list[TaskDef],
        prompts: list[str],
    ) -> tuple[list[str], dict[str, Any]]:
        self.generate_calls.append((tasks, prompts))
        if self.mode == "error":
            raise BatchGenerationError("boom", {"promptTokens": 3})
        assert len(tasks) == 1
        assert len(prompts) == 1
        assert tasks[0].generation_override["temperature"] == 0.0
        text = json.dumps(expected_manifest(), ensure_ascii=False)
        return [text], {
            "loadSeconds": 0.1,
            "generationSeconds": 0.2,
            "promptTokens": 7,
            "renderedPromptTokens": len(prompts[0]) + 1,
            "generationTokens": 4,
            "groups": [{
                "suspectedTokenLimit": False,
                "temperature": 0.0,
            }],
        }


def _write_config(tmp_path: Path) -> Path:
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}", encoding="utf-8")
    path = tmp_path / "swarm.json"
    path.write_text(
        json.dumps({
            "schemaVersion": 1,
            "model": {
                "repository": "local/context-model",
                "localPath": str(model),
            },
            "batch": {"maxWorkers": 1},
            "artifacts": str(tmp_path / "runs"),
            "seed": 11,
        }),
        encoding="utf-8",
    )
    return path


def test_run_benchmark_uses_one_resident_backend(tmp_path: Path) -> None:
    FakeBackend.instances = []
    config = _write_config(tmp_path)
    model_path = tmp_path / "model"

    def factory(loaded: Any) -> FakeBackend:
        return FakeBackend(loaded, model_path=model_path)

    payload = run_benchmark(
        config_path=config,
        tiers=(32,),
        positions=("start", "end"),
        trials=1,
        tolerance_tokens=64,
        max_generation_tokens=32,
        seed=11,
        backend_factory=factory,
    )
    backend = FakeBackend.instances[0]
    assert len(FakeBackend.instances) == 1
    assert backend.opened == 1
    assert backend.closed == 1
    assert len(backend.generate_calls) == 2
    for tasks, prompts in backend.generate_calls:
        assert len(tasks) == 1
        assert len(prompts) == 1
    assert payload["schemaVersion"] == 1
    assert payload["aggregate"]["passes"] == 2
    assert payload["aggregate"]["total"] == 2
    assert payload["metadata"]["modelSha256"]
    assert {case["position"] for case in payload["cases"]} == {"start", "end"}
    assert all(case["renderedTokens"] > 0 for case in payload["cases"])


def test_run_benchmark_skips_generate_when_fit_fails(tmp_path: Path) -> None:
    FakeBackend.instances = []
    config = _write_config(tmp_path)

    def factory(loaded: Any) -> FakeBackend:
        return FakeBackend(loaded, model_path=tmp_path / "model")

    payload = run_benchmark(
        config_path=config,
        tiers=(8,),
        positions=("start",),
        trials=1,
        tolerance_tokens=0,
        max_generation_tokens=32,
        backend_factory=factory,
    )
    backend = FakeBackend.instances[0]
    assert backend.generate_calls == []
    assert payload["cases"][0]["outcome"] == "token_fit_out_of_tolerance"


def test_run_benchmark_records_inference_error(tmp_path: Path) -> None:
    FakeBackend.instances = []
    config = _write_config(tmp_path)

    def factory(loaded: Any) -> FakeBackend:
        return FakeBackend(loaded, model_path=tmp_path / "model", mode="error")

    payload = run_benchmark(
        config_path=config,
        tiers=(64,),
        positions=("start",),
        trials=1,
        tolerance_tokens=64,
        max_generation_tokens=32,
        backend_factory=factory,
    )
    assert payload["cases"][0]["outcome"] == "inference_error"
    assert payload["cases"][0]["promptTokens"] == 3


def test_run_benchmark_records_wall_time_and_peak_memory(tmp_path: Path) -> None:
    FakeBackend.instances = []
    config = _write_config(tmp_path)

    def factory(loaded: Any) -> FakeBackend:
        return FakeBackend(loaded, model_path=tmp_path / "model")

    payload = run_benchmark(
        config_path=config,
        tiers=(32,),
        positions=("start",),
        trials=1,
        tolerance_tokens=64,
        max_generation_tokens=32,
        backend_factory=factory,
    )
    case = payload["cases"][0]
    assert case["outcome"] == "pass"
    assert case["wallSeconds"] >= 0.0
    assert case["peakMemoryGigabytes"] == 0.0
    assert payload["metadata"]["resumedCases"] == 0


def test_run_benchmark_reports_progress_and_resumes_matching_cases(
    tmp_path: Path,
) -> None:
    FakeBackend.instances = []
    config = _write_config(tmp_path)
    seen: list[tuple[str, int, int]] = []

    def factory(loaded: Any) -> FakeBackend:
        return FakeBackend(loaded, model_path=tmp_path / "model")

    def observe(record: dict[str, Any], done: int, total: int) -> None:
        seen.append((record["caseId"], done, total))

    earlier = {
        "caseId": "32-start-1",
        "tier": 32,
        "position": "start",
        "trial": 1,
        "seed": 11,
        "outcome": "wrong_edit",
        "renderedTokens": 33,
    }
    other_seed = dict(earlier, caseId="32-end-1", position="end", seed=12)
    payload = run_benchmark(
        config_path=config,
        tiers=(32,),
        positions=("start", "end"),
        trials=1,
        tolerance_tokens=64,
        max_generation_tokens=32,
        seed=11,
        backend_factory=factory,
        on_case=observe,
        completed_cases=[earlier, other_seed],
    )
    backend = FakeBackend.instances[0]
    # The matching start case is reused; the end case ran because its seed
    # differed from the effective seed.
    assert len(backend.generate_calls) == 1
    assert seen == [("32-end-1", 2, 2)]
    assert payload["metadata"]["resumedCases"] == 1
    outcomes = {case["caseId"]: case["outcome"] for case in payload["cases"]}
    assert outcomes == {"32-start-1": "wrong_edit", "32-end-1": "pass"}
    assert payload["aggregate"]["passes"] == 1
    assert payload["aggregate"]["highestAllPassTier"] is None


def test_run_benchmark_retrieve_mode_scores_applied_edits_and_isolates_resume(
    tmp_path: Path,
) -> None:
    FakeBackend.instances = []
    config = _write_config(tmp_path)

    class AnchorBackend(FakeBackend):
        def generate(
            self,
            tasks: list[TaskDef],
            prompts: list[str],
        ) -> tuple[list[str], dict[str, Any]]:
            self.generate_calls.append((tasks, prompts))
            assert "Reproduce this object exactly" not in prompts[0]
            assert "function named target" in prompts[0]
            text = json.dumps({
                "edits": [{
                    "path": "context_probe.py",
                    "old": 'return "before"',
                    "new": 'return "after"',
                }]
            })
            return [text], {"promptTokens": 7, "generationTokens": 4, "groups": [{}]}

    def factory(loaded: Any) -> AnchorBackend:
        return AnchorBackend(loaded, model_path=tmp_path / "model")

    copy_record = {
        "caseId": "32-start-1",
        "mode": "copy",
        "tier": 32,
        "position": "start",
        "trial": 1,
        "seed": 11,
        "outcome": "pass",
    }
    payload = run_benchmark(
        config_path=config,
        tiers=(32,),
        positions=("start",),
        trials=1,
        tolerance_tokens=64,
        max_generation_tokens=32,
        seed=11,
        backend_factory=factory,
        completed_cases=[copy_record],
        mode="retrieve",
    )
    backend = FakeBackend.instances[0]
    # A copy-mode checkpoint must not satisfy a retrieve-mode cell.
    assert len(backend.generate_calls) == 1
    assert payload["metadata"]["resumedCases"] == 0
    assert payload["metadata"]["mode"] == "retrieve"
    assert payload["reproducibility"]["mode"] == "retrieve"
    case = payload["cases"][0]
    assert case["mode"] == "retrieve"
    assert case["outcome"] == "pass"
    assert "Applied edit" in case["score"]["detail"]
    assert payload["aggregate"]["highestAllPassTier"] == 32


def test_run_benchmark_rejects_unknown_mode(tmp_path: Path) -> None:
    import pytest

    config = _write_config(tmp_path)
    with pytest.raises(ValueError):
        run_benchmark(
            config_path=config,
            tiers=(32,),
            positions=("start",),
            trials=1,
            tolerance_tokens=64,
            max_generation_tokens=32,
            backend_factory=lambda loaded: FakeBackend(loaded, model_path=tmp_path / "model"),
            mode="guess",
        )
