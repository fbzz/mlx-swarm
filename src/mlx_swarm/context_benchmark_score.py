"""Context benchmark scoring and validation utilities."""

from __future__ import annotations

import json
from typing import Any, Dict, Tuple

from .context_benchmark_types import OUTCOMES, ScoreResult
from .context_benchmark_prompt import SYNTHETIC_PATH, TARGET_OLD, TARGET_NEW
from .gates import normalize_output, OutputGate

__all__ = ["expected_manifest", "score_output"]


def expected_manifest() -> Dict[str, Any]:
    """Return the exact JSON manifest expected for a successful benchmark case."""
    return {
        "edits": [
            {
                "path": SYNTHETIC_PATH,
                "old": TARGET_OLD,
                "new": TARGET_NEW,
            }
        ]
    }


def score_output(raw_output: str, suspected_token_limit: bool = False) -> ScoreResult:
    """Score a raw model output against the expected benchmark manifest.

    Args:
        raw_output: The raw string output from the model.
        suspected_token_limit: Flag indicating if the output was truncated.

    Returns:
        A ScoreResult indicating the outcome of the scoring.
    """
    gate = OutputGate(output_format="json", strip_single_code_fence=True)
    normalized, normalizations = normalize_output(raw_output, gate)

    try:
        parsed = json.loads(normalized)
    except (json.JSONDecodeError, TypeError):
        return ScoreResult(
            outcome="invalid_json",
            normalizations=tuple(normalizations),
            detail="Failed to parse normalized output as JSON.",
        )

    if not isinstance(parsed, dict):
        return ScoreResult(
            outcome="invalid_schema",
            normalizations=tuple(normalizations),
            detail="Normalized output is not a JSON object.",
        )

    expected = expected_manifest()

    if parsed == expected:
        return ScoreResult(
            outcome="pass",
            normalizations=tuple(normalizations),
            detail="Output matches expected manifest exactly.",
        )

    if suspected_token_limit:
        return ScoreResult(
            outcome="suspected_token_limit",
            normalizations=tuple(normalizations),
            detail="Output may be truncated due to token limits.",
        )

    # Check for valid schema structure but wrong content
    edits = parsed.get("edits")
    if (
        isinstance(edits, list)
        and len(edits) == 1
        and isinstance(edits[0], dict)
        and set(edits[0].keys()) == {"path", "old", "new"}
        and all(isinstance(v, str) for v in edits[0].values())
    ):
        return ScoreResult(
            outcome="wrong_edit",
            normalizations=tuple(normalizations),
            detail="Valid schema but content does not match expected manifest.",
        )

    return ScoreResult(
        outcome="invalid_schema",
        normalizations=tuple(normalizations),
        detail="Output does not match expected schema structure.",
    )
