"""Context benchmark type definitions and shared taxonomies."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

__all__ = ["PromptFit", "ScoreResult", "POSITIONS", "OUTCOMES"]

POSITIONS: Tuple[str, ...] = ("start", "middle", "end")

OUTCOMES: Tuple[str, ...] = (
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
    """Immutable record of prompt fitting metrics."""

    prompt: str
    requested_tokens: int
    rendered_tokens: int
    unit_count: int
    within_tolerance: bool

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe camelCase-key dictionary."""
        return {
            "prompt": self.prompt,
            "requestedTokens": self.requested_tokens,
            "renderedTokens": self.rendered_tokens,
            "unitCount": self.unit_count,
            "withinTolerance": self.within_tolerance,
        }


@dataclass(frozen=True)
class ScoreResult:
    """Immutable record of benchmark scoring outcomes."""

    outcome: str
    normalizations: Tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe camelCase-key dictionary."""
        return {
            "outcome": self.outcome,
            "normalizations": list(self.normalizations),
            "detail": self.detail,
        }
