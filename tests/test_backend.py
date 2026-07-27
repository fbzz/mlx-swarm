"""Backend prompt rendering and per-task generation configuration tests."""
# @lat: [[Tests#Backend]]

from __future__ import annotations

from pathlib import Path

from swarm_agents.backend import _render_prompt, _role_generation_config
from swarm_agents.contracts import ModelConfig, SwarmConfig, TaskDef


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
