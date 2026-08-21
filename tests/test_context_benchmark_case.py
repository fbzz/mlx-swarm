"""Unit tests for context_benchmark_case single-case module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mlx_swarm.backend import BatchGenerationError
from mlx_swarm.contracts import load_config
from mlx_swarm.context_benchmark_case import benchmark_task, run_case


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
        self.generate_calls: list[tuple[list, list[str]]] = []
        self.instances.append(self)

    def open(self) -> None:
        self.opened += 1

    def close(self) -> None:
        self.closed += 1

    def generate(
        self,
        tasks: list,  # type: ignore[type-arg]
        prompts: list[str],
    ) -> tuple[list[str], dict[str, Any]]:
        self.generate_calls.append((tasks, prompts))
        if self.mode == "error":
            raise BatchGenerationError("boom", {"promptTokens": 3})
        assert len(tasks) == 1
        assert len(prompts) == 1
        assert tasks[0].generation_override["temperature"] == 0.0
        text = json.dumps({"edits": []}, ensure_ascii=False)
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


def test_benchmark_task_freezes_gate_and_generation(tmp_path: Path) -> None:
    task = benchmark_task("2048-start-1", 256, 9)
    assert task.id == "2048-start-1"
    assert task.role == "implementation"
    assert task.prompt == ""
    assert task.worker_output_protocol == "edit-manifest-v1"
    assert task.gate.output_format == "json"
    assert task.gate.strip_single_code_fence is True
    assert task.gate.json_required_keys == ("edits",)
    assert task.gate.json_allowed_keys == ("edits",)
    assert task.generation_override == {
        "temperature": 0.0,
        "top_p": 1.0,
        "max_tokens": 256,
        "seed": 9,
    }


def test_run_case_fit_out_of_tolerance_skips_generate(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    backend = FakeBackend(config, model_path=config_path.parent / "model")
    result = run_case(
        backend=backend,
        config=config,
        tier=8,
        position="start",
        trial=1,
        seed=11,
        tolerance_tokens=0,
        max_generation_tokens=32,
    )
    assert result["outcome"] == "token_fit_out_of_tolerance"
    assert result["withinTolerance"] is False
    assert result["score"] is None
    assert result["promptTokens"] == 0
    assert backend.generate_calls == []


def test_run_case_records_batch_generation_error(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    backend = FakeBackend(config, model_path=config_path.parent / "model", mode="error")
    result = run_case(
        backend=backend,
        config=config,
        tier=64,
        position="start",
        trial=1,
        seed=11,
        tolerance_tokens=64,
        max_generation_tokens=32,
    )
    assert result["outcome"] == "inference_error"
    assert result["promptTokens"] == 3
    assert result["outputPreview"] == "boom"
    assert result["outputSha256"] is None


def test_run_case_rejects_invalid_mode_and_negative_decoys(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    backend = FakeBackend(config, model_path=config_path.parent / "model")
    with pytest.raises(ValueError, match="mode must be one of"):
        run_case(
            backend=backend,
            config=config,
            tier=1,
            position="start",
            trial=1,
            seed=11,
            tolerance_tokens=0,
            max_generation_tokens=32,
            mode="guess",
        )
    with pytest.raises(ValueError, match="decoys must be non-negative"):
        run_case(
            backend=backend,
            config=config,
            tier=1,
            position="start",
            trial=1,
            seed=11,
            tolerance_tokens=0,
            max_generation_tokens=32,
            decoys=-1,
        )
