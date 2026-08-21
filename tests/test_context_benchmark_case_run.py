"""Direct unit tests for run_case generating paths."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from mlx_swarm.backend import BatchGenerationError
from mlx_swarm.contracts import load_config
from mlx_swarm.context_benchmark_case import run_case
from mlx_swarm.context_benchmark_score import expected_manifest


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

    def __init__(
        self,
        config: Any,
        *,
        model_path: Path,
        mode: str = "pass",
        text: str | None = None,
        suspected: bool = False,
    ) -> None:
        self.config = config
        self.model_path = model_path
        self.tokenizer = FakeTokenizer()
        self.mode = mode
        self.opened = 0
        self.closed = 0
        self.generate_calls: list[tuple[list[Any], list[str]]] = []
        self.return_text = text
        self.return_suspected = suspected
        self.instances.append(self)

    def open(self) -> None:
        self.opened += 1

    def close(self) -> None:
        self.closed = 1

    def generate(
        self,
        tasks: list[Any],
        prompts: list[str],
    ) -> tuple[list[str], dict[str, Any]]:
        self.generate_calls.append((tasks, prompts))
        if self.mode == "error":
            raise BatchGenerationError("boom", {"promptTokens": 3})
        assert len(tasks) == 1
        assert len(prompts) == 1
        assert tasks[0].generation_override["temperature"] == 0.0
        default = json.dumps(expected_manifest(), ensure_ascii=False)
        text = self.return_text if self.return_text is not None else default
        return [text], {
            "loadSeconds": 0.1,
            "generationSeconds": 0.2,
            "promptTokens": 7,
            "renderedPromptTokens": len(prompts[0]) + 1,
            "generationTokens": 4,
            "groups": [{
                "suspectedTokenLimit": self.return_suspected,
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


def test_run_case_passes_with_exact_manifest(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    backend = FakeBackend(config, model_path=config_path.parent / "model")
    result = run_case(
        backend=backend,
        config=config,
        tier=32,
        position="start",
        trial=1,
        seed=11,
        tolerance_tokens=64,
        max_generation_tokens=32,
    )
    assert result["caseId"] == "32-start-1"
    assert result["mode"] == "copy"
    assert result["decoys"] == 0
    assert result["outcome"] == "pass"
    assert result["score"]["outcome"] == "pass"
    assert result["promptTokens"] == 7
    assert result["generationTokens"] == 4
    assert result["generationSeconds"] == 0.2
    assert result["loadSeconds"] == 0.1
    assert result["peakMemoryGigabytes"] == 0.0
    assert result["wallSeconds"] >= 0.0
    expected_text = json.dumps(expected_manifest(), ensure_ascii=False)
    assert result["outputSha256"] == hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
    assert result["outputPreview"] == expected_text


def test_run_case_retrieve_mode_applies_anchor(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    manifest_text = '{"edits":[{"path":"context_probe.py","old":"return \\"before\\"","new":"return \\"after\\""}]}'
    backend = FakeBackend(config, model_path=config_path.parent / "model", text=manifest_text)
    result = run_case(
        backend=backend,
        config=config,
        tier=32,
        position="start",
        trial=1,
        seed=11,
        tolerance_tokens=64,
        max_generation_tokens=32,
        mode="retrieve",
    )
    assert result["outcome"] == "pass"
    assert result["mode"] == "retrieve"
    assert result["score"]["detail"].startswith("Applied edit")
    
    backend_copy = FakeBackend(config, model_path=config_path.parent / "model", text=manifest_text)
    result_copy = run_case(
        backend=backend_copy,
        config=config,
        tier=32,
        position="start",
        trial=1,
        seed=11,
        tolerance_tokens=64,
        max_generation_tokens=32,
        mode="copy",
    )
    assert result_copy["outcome"] == "wrong_edit"


def test_run_case_marks_suspected_token_limit(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    manifest_text = '{"edits":[{"path":"context_probe.py","old":"x","new":"y"}]}'
    backend = FakeBackend(config, model_path=config_path.parent / "model", text=manifest_text, suspected=True)
    result = run_case(
        backend=backend,
        config=config,
        tier=32,
        position="start",
        trial=1,
        seed=11,
        tolerance_tokens=64,
        max_generation_tokens=32,
    )
    assert result["outcome"] == "suspected_token_limit"


def test_run_case_classifies_invalid_json_and_schema(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    
    backend_invalid_json = FakeBackend(config, model_path=config_path.parent / "model", text="not json")
    result_invalid_json = run_case(
        backend=backend_invalid_json,
        config=config,
        tier=32,
        position="start",
        trial=1,
        seed=11,
        tolerance_tokens=64,
        max_generation_tokens=32,
    )
    assert result_invalid_json["outcome"] == "invalid_json"
    
    backend_invalid_schema = FakeBackend(config, model_path=config_path.parent / "model", text="[]")
    result_invalid_schema = run_case(
        backend=backend_invalid_schema,
        config=config,
        tier=32,
        position="start",
        trial=1,
        seed=11,
        tolerance_tokens=64,
        max_generation_tokens=32,
    )
    assert result_invalid_schema["outcome"] == "invalid_schema"
