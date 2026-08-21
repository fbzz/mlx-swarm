"""Exact edit-manifest scoring for the local context benchmark."""

from __future__ import annotations

import json
from typing import Any

from .context_benchmark_prompt import (
    SYNTHETIC_PATH,
    TARGET_NEW,
    TARGET_OLD,
    manifest_json,
)
from .context_benchmark_types import ScoreResult
from .contracts import OutputGate
from .gates import normalize_output

__all__ = ["expected_manifest", "score_output"]


def expected_manifest() -> dict[str, Any]:
    parsed = json.loads(manifest_json())
    if not isinstance(parsed, dict):
        raise RuntimeError("Context-benchmark manifest must be a JSON object.")
    return parsed


def score_output(
    raw_output: str,
    suspected_token_limit: bool = False,
    *,
    source: str | None = None,
) -> ScoreResult:
    """Score one completion.

    Without ``source`` (copy mode) only the normalized expected manifest
    passes. With ``source`` (retrieve mode) a manifest also passes when it
    applies the way the runtime applies edit-manifest-v1: one edit on the
    synthetic path whose non-empty ``old`` occurs exactly once in the file
    and whose replacement reproduces the expected file, so a smaller or
    larger unique anchor is as correct as the canonical one.
    """
    gate = OutputGate(output_format="json", strip_single_code_fence=True)
    normalized, normalizations = normalize_output(raw_output, gate)
    names = tuple(normalizations)
    try:
        parsed = json.loads(normalized)
    except (json.JSONDecodeError, TypeError):
        return ScoreResult(
            outcome="invalid_json",
            normalizations=names,
            detail=_detail("Failed to parse normalized output as JSON."),
        )
    if parsed == expected_manifest():
        return ScoreResult(
            outcome="pass",
            normalizations=names,
            detail=_detail("Output matches expected manifest exactly."),
        )
    applied_detail: str | None = None
    if source is not None and _valid_edit_shape(parsed):
        applied_detail = _applied_edit_detail(parsed["edits"][0], source)
        if applied_detail is None:
            return ScoreResult(
                outcome="pass",
                normalizations=names,
                detail=_detail("Applied edit reproduces the expected file."),
            )
    if suspected_token_limit:
        return ScoreResult(
            outcome="suspected_token_limit",
            normalizations=names,
            detail=_detail("Non-exact completion was flagged as token-limited."),
        )
    if _valid_edit_shape(parsed):
        return ScoreResult(
            outcome="wrong_edit",
            normalizations=names,
            detail=_detail(
                applied_detail
                or "Valid schema but content does not match expected manifest."
            ),
        )
    return ScoreResult(
        outcome="invalid_schema",
        normalizations=names,
        detail=_detail("Output does not match expected schema structure."),
    )


def _applied_edit_detail(edit: dict[str, str], source: str) -> str | None:
    """Return None when the edit applies to the expected file, else why not."""
    if edit["path"] != SYNTHETIC_PATH:
        return f"Edit path is not {SYNTHETIC_PATH}."
    old, new = edit["old"], edit["new"]
    if not old:
        return "Old text is empty."
    if old == new:
        return "Edit is a no-op."
    occurrences = source.count(old)
    if occurrences != 1:
        return f"Old text must match exactly once in the file; found {occurrences}."
    if source.replace(old, new, 1) != source.replace(TARGET_OLD, TARGET_NEW, 1):
        return "Applied edit does not reproduce the expected file."
    return None


def _valid_edit_shape(parsed: Any) -> bool:
    if not isinstance(parsed, dict) or set(parsed) != {"edits"}:
        return False
    edits = parsed["edits"]
    if not isinstance(edits, list) or len(edits) != 1:
        return False
    edit = edits[0]
    return (
        isinstance(edit, dict)
        and set(edit) == {"path", "old", "new"}
        and all(isinstance(value, str) for value in edit.values())
    )


def _detail(text: str) -> str:
    return text[:240]
