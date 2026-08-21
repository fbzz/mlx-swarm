"""Unit tests for context_benchmark_types shared records and taxonomies."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

from mlx_swarm.context_benchmark_types import OUTCOMES, POSITIONS, PromptFit, ScoreResult


def test_positions_and_outcomes_taxonomy() -> None:
    assert POSITIONS == ("start", "middle", "end")
    assert OUTCOMES == (
        "pass",
        "token_fit_out_of_tolerance",
        "suspected_token_limit",
        "invalid_json",
        "invalid_schema",
        "wrong_edit",
        "inference_error",
    )


def test_prompt_fit_to_dict_uses_camel_case_keys() -> None:
    fit = PromptFit(
        prompt="p",
        requested_tokens=10,
        rendered_tokens=12,
        unit_count=3,
        within_tolerance=True,
    )
    expected = {
        "prompt": "p",
        "requestedTokens": 10,
        "renderedTokens": 12,
        "unitCount": 3,
        "withinTolerance": True,
    }
    assert fit.to_dict() == expected


def test_prompt_fit_is_frozen() -> None:
    fit = PromptFit(
        prompt="p",
        requested_tokens=10,
        rendered_tokens=12,
        unit_count=3,
        within_tolerance=True,
    )
    try:
        fit.unit_count = 4  # type: ignore[assignment]
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("Expected FrozenInstanceError on assignment")


def test_score_result_defaults_and_to_dict() -> None:
    result = ScoreResult(outcome="pass")
    assert result.normalizations == ()
    assert result.detail == ""

    detailed = ScoreResult(
        outcome="wrong_edit",
        normalizations=("outer-whitespace",),
        detail="d",
    )
    expected = {
        "outcome": "wrong_edit",
        "normalizations": ["outer-whitespace"],
        "detail": "d",
    }
    assert detailed.to_dict() == expected


def test_score_result_is_frozen() -> None:
    result = ScoreResult(outcome="pass")
    try:
        result.outcome = "x"  # type: ignore[assignment]
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("Expected FrozenInstanceError on assignment")
