"""Backend prompt rendering and per-task generation configuration tests."""
# @lat: [[Tests#Backend]]

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from mlx_swarm.backend import (
    BatchGenerationError,
    MLXBatchBackend,
    TOKEN_LIMIT_DETECTION_MARGIN,
    _render_prompt,
    _role_generation_config,
    _split_by_prompt_budget,
    suspected_token_limit,
)
from mlx_swarm.contracts import (
    BatchConfig,
    ModelConfig,
    OutputGate,
    SwarmConfig,
    TaskDef,
    WorkerCapabilityProfile,
    WorkerConfig,
)


class FakeThinkingTokenizer:
    has_chat_template = True

    def __init__(self) -> None:
        self.encoded = ""

    def apply_chat_template(self, messages, **kwargs):
        assert messages[0]["role"] == "user"
        assert kwargs["tokenize"] is False
        return f"<|im_start|>user\n{messages[0]['content']}<|im_end|>\n<|im_start|>assistant\n<think>\n"

    def encode(self, value: str, **kwargs):
        assert kwargs["add_special_tokens"] is False
        self.encoded = value
        return list(range(len(value)))


def test_suspected_token_limit_catches_re_tokenization_drift() -> None:
    assert suspected_token_limit(794, 800)
    assert suspected_token_limit(800, 800)
    assert suspected_token_limit(801, 800)


def test_suspected_token_limit_ignores_comfortable_completions() -> None:
    assert not suspected_token_limit(700, 1024)
    assert not suspected_token_limit(783, 800)


def test_suspected_token_limit_uses_exact_compare_for_tiny_limits() -> None:
    for limit in range(1, TOKEN_LIMIT_DETECTION_MARGIN + 1):
        assert not suspected_token_limit(limit - 1, limit)
        assert suspected_token_limit(limit, limit)


def test_render_prompt_closes_forced_thinking_when_disabled() -> None:
    tokenizer = FakeThinkingTokenizer()
    _render_prompt(tokenizer, "Do the task", {"enable_thinking": False})
    assert tokenizer.encoded.endswith("<think>\n</think>\n\n")


def test_render_prompt_preserves_thinking_when_enabled() -> None:
    tokenizer = FakeThinkingTokenizer()
    _render_prompt(tokenizer, "Do the task", {"enable_thinking": True})
    assert tokenizer.encoded.endswith("<think>\n")
    assert "</think>" not in tokenizer.encoded


def test_generation_override_remains_task_specific() -> None:
    config = SwarmConfig(
        source=Path("swarm.json"),
        model=ModelConfig("unused", ""),
        enable_thinking=False,
        seed=7,
    )
    review = TaskDef(
        id="review",
        role="review",
        prompt="Review",
        generation_override={"max_tokens": 123, "temperature": 0.4},
    )
    result = _role_generation_config(review, config)
    assert result["max_tokens"] == 123
    assert result["temperature"] == 0.4
    assert result["top_p"] == 1.0
    assert result["seed"] == 7


def test_strict_json_tasks_share_deterministic_sampler_by_default() -> None:
    config = SwarmConfig(
        source=Path("swarm.json"),
        model=ModelConfig("unused", ""),
        seed=11,
    )
    task = TaskDef(
        id="edit",
        role="implementation",
        prompt="Return JSON",
        gate=OutputGate(output_format="json"),
        worker_output_protocol="edit-manifest-v1",
    )

    result = _role_generation_config(task, config)

    assert result["temperature"] == 0.0
    assert result["top_p"] == 1.0
    assert result["max_tokens"] == 1024
    assert result["seed"] == 11


def test_strict_json_explicit_token_budget_remains_authoritative() -> None:
    config = SwarmConfig(
        source=Path("swarm.json"),
        model=ModelConfig("unused", ""),
    )
    task = TaskDef(
        id="edit",
        role="implementation",
        prompt="Return JSON",
        gate=OutputGate(output_format="json"),
        worker_output_protocol="edit-manifest-v1",
        generation_override={"max_tokens": 1200},
    )

    result = _role_generation_config(task, config)

    assert result["max_tokens"] == 1200


class _TokenCountTokenizer:
    has_chat_template = False

    def encode(self, _value: str):
        return list(range(10))


def test_backend_enforces_declared_token_context_before_generation() -> None:
    config = SwarmConfig(
        source=Path("swarm.json"),
        model=ModelConfig("unused", ""),
        worker=WorkerConfig(
            capabilities=WorkerCapabilityProfile(
                context_window_tokens=20,
                max_generation_tokens=20,
            )
        ),
    )
    task = TaskDef(
        id="too-large",
        role="general",
        prompt="prompt",
        generation_override={"max_tokens": 11},
    )
    with patch(
        "mlx_swarm.backend._resolve_model_path",
        return_value=Path("."),
    ):
        backend = MLXBatchBackend(config)
    backend.tokenizer = _TokenCountTokenizer()
    backend.model = object()
    backend.open = lambda: None  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="declared 20-token worker context"):
        backend.generate([task], ["prompt"])


class _FakeBatchTokenizer:
    has_chat_template = False

    def encode(self, value: str, **_kwargs):
        return list(value.encode("utf-8"))


class _FakeRandom:
    def __init__(self) -> None:
        self.seeds: list[int] = []

    def seed(self, value: int) -> None:
        self.seeds.append(value)


class _FakeMX:
    def __init__(self) -> None:
        self.random = _FakeRandom()

    @staticmethod
    def get_peak_memory() -> int:
        return 4_000_000_000


def _fake_batch_backend(
    config: SwarmConfig,
) -> tuple[MLXBatchBackend, list[dict]]:
    with patch(
        "mlx_swarm.backend._resolve_model_path",
        return_value=Path("."),
    ):
        backend = MLXBatchBackend(config)
    backend.model = object()
    backend.tokenizer = _FakeBatchTokenizer()
    backend.mx = _FakeMX()
    backend.open = lambda: None  # type: ignore[method-assign]
    backend.make_sampler_fn = lambda **kwargs: kwargs
    calls: list[dict] = []

    def batch_generate(**kwargs):
        calls.append(kwargs)
        ordinal = len(calls)
        return {
            "texts": [
                f"call-{ordinal}-item-{index}"
                for index in range(len(kwargs["prompts"]))
            ],
            "stats": {
                "prompt_tokens": sum(map(len, kwargs["prompts"])),
                "generation_tokens": len(kwargs["prompts"]) * 3,
                "generation_tps": 10,
            },
        }

    backend.batch_generate_fn = batch_generate
    return backend, calls


def test_four_compatible_tasks_use_one_true_batch_call() -> None:
    config = SwarmConfig(
        source=Path("swarm.json"),
        model=ModelConfig("unused", ""),
        batch=BatchConfig(max_workers=4, max_batch_prompt_tokens=100),
        seed=9,
    )
    tasks = [
        TaskDef(
            id=f"task-{index}",
            role="review",
            prompt="review",
            generation_override={"max_tokens": 20},
        )
        for index in range(4)
    ]
    backend, calls = _fake_batch_backend(config)

    outputs, stats = backend.generate(tasks, ["aaaa"] * 4)

    assert len(calls) == 1
    assert calls[0]["max_tokens"] == [20, 20, 20, 20]
    assert calls[0]["completion_batch_size"] == 4
    assert outputs == [
        "call-1-item-0",
        "call-1-item-1",
        "call-1-item-2",
        "call-1-item-3",
    ]
    assert stats["generationCalls"] == 1
    assert stats["maxTrueBatchWidth"] == 4
    assert stats["samplerFragmented"] is False
    assert stats["renderedPromptTokens"] == 16


def test_sampler_groups_and_prompt_budget_split_deterministically() -> None:
    config = SwarmConfig(
        source=Path("swarm.json"),
        model=ModelConfig("unused", ""),
        batch=BatchConfig(max_workers=4, max_batch_prompt_tokens=5),
        seed=3,
    )
    tasks = [
        TaskDef(
            id="first",
            role="review",
            prompt="one",
            generation_override={"temperature": 0.0, "max_tokens": 7},
        ),
        TaskDef(
            id="second",
            role="review",
            prompt="two",
            generation_override={"temperature": 0.0, "max_tokens": 8},
        ),
        TaskDef(
            id="third",
            role="review",
            prompt="three",
            generation_override={"temperature": 0.5, "max_tokens": 9},
        ),
    ]
    backend, calls = _fake_batch_backend(config)

    outputs, stats = backend.generate(tasks, ["aaaa", "bbbb", "cc"])

    assert [call["max_tokens"] for call in calls] == [[7], [8], [9]]
    assert outputs == [
        "call-1-item-0",
        "call-2-item-0",
        "call-3-item-0",
    ]
    assert stats["samplerGroupCount"] == 2
    assert stats["physicalBatchCount"] == 3
    assert stats["generationCalls"] == 3
    assert stats["samplerFragmented"] is True
    assert stats["batchSplitByPromptBudget"] is True
    assert stats["maxTrueBatchWidth"] == 1
    assert backend.mx.random.seeds == [3, 3]


def test_prompt_budget_rejects_oversized_singleton() -> None:
    with pytest.raises(RuntimeError, match="maxBatchPromptTokens 5"):
        _split_by_prompt_budget([0], [list(range(6))], 5)


def test_later_physical_batch_failure_preserves_spent_usage() -> None:
    config = SwarmConfig(
        source=Path("swarm.json"),
        model=ModelConfig("unused", ""),
        batch=BatchConfig(max_workers=2, max_batch_prompt_tokens=4),
        seed=5,
    )
    tasks = [
        TaskDef(
            id=f"task-{index}",
            role="review",
            prompt="review",
            generation_override={"max_tokens": 10},
        )
        for index in range(2)
    ]
    backend, calls = _fake_batch_backend(config)
    successful = backend.batch_generate_fn

    def fail_second_call(**kwargs):
        if len(calls) == 1:
            calls.append(kwargs)
            raise RuntimeError("second chunk failed")
        return successful(**kwargs)

    backend.batch_generate_fn = fail_second_call

    with pytest.raises(BatchGenerationError) as exc_info:
        backend.generate(tasks, ["aaaa", "bbbb"])

    stats = exc_info.value.statistics
    assert len(calls) == 2
    assert stats["generationCalls"] == 2
    assert stats["completedGenerationCalls"] == 1
    assert stats["failedGenerationCalls"] == 1
    assert stats["promptTokens"] == 4
    assert stats["generationTokens"] == 3
    assert stats["batchSize"] == 0
    assert stats["attemptedBatchSize"] == 2
