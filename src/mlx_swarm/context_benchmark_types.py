"""Shared records and taxonomies for the local context benchmark."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["POSITIONS", "OUTCOMES", "PromptFit", "ScoreResult"]

POSITIONS: tuple[str, ...] = ("start", "middle", "end")

OUTCOMES: tuple[str, ...] = (
    "pass",
    "token_fit_out_of_tolerance",
    "suspected_token_limit",
    "invalid_json",
    "invalid_schema",
    "wrong_edit",
    "inference_error",
)


@dataclass(frozen=True)
class PromptFit:
    prompt: str
    requested_tokens: int
    rendered_tokens: int
    unit_count: int
    within_tolerance: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "requestedTokens": self.requested_tokens,
            "renderedTokens": self.rendered_tokens,
            "unitCount": self.unit_count,
            "withinTolerance": self.within_tolerance,
        }


@dataclass(frozen=True)
class ScoreResult:
    outcome: str
    normalizations: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "normalizations": list(self.normalizations),
            "detail": self.detail,
        }
