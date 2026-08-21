"""Tests for context-benchmark fitting and exact scoring."""

from __future__ import annotations

import json

from mlx_swarm.context_benchmark_fit import fit_prompt
from mlx_swarm.context_benchmark_score import expected_manifest, score_output


def test_fit_prompt_reaches_exact_target_and_tie_breaks() -> None:
    counted: list[str] = []

    def prompt_for_units(unit_count: int) -> str:
        return f"u{unit_count}"

    def token_count(prompt: str) -> int:
        counted.append(prompt)
        return {"u0": 4, "u1": 10, "u2": 20}[prompt]

    exact = fit_prompt(
        requested_tokens=10,
        tolerance_tokens=1,
        prompt_for_units=prompt_for_units,
        token_count=token_count,
        max_units=4,
    )
    assert exact.unit_count == 1
    assert exact.rendered_tokens == 10
    assert exact.within_tolerance is True
    assert counted
    assert all(item.startswith("u") for item in counted)

    def close_count(prompt: str) -> int:
        return {"u0": 8, "u1": 12, "u2": 12}[prompt]

    closest = fit_prompt(
        requested_tokens=10,
        tolerance_tokens=8,
        prompt_for_units=prompt_for_units,
        token_count=close_count,
        max_units=2,
    )
    assert closest.unit_count == 0
    assert closest.rendered_tokens == 8


def test_fit_prompt_marks_impossible_tolerance_and_validates() -> None:
    fit = fit_prompt(
        requested_tokens=100,
        tolerance_tokens=1,
        prompt_for_units=lambda count: "x" * (count + 1),
        token_count=lambda prompt: len(prompt),
        max_units=2,
    )
    assert fit.within_tolerance is False
    try:
        fit_prompt(0, 1, lambda count: "x", lambda prompt: 1)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_score_output_accepts_normalized_exact_manifests() -> None:
    expected = json.dumps(expected_manifest(), ensure_ascii=False)
    assert expected_manifest()["edits"][0]["path"] == "context_probe.py"
    assert len(expected_manifest()["edits"]) == 1
    assert score_output(expected).outcome == "pass"
    assert score_output(f"```json\n{expected}\n```").outcome == "pass"
    assert score_output(f"<manifest>\n{expected}\n</manifest>").outcome == "pass"
    assert score_output(f"<think>plan</think>\n{expected}").outcome == "pass"
    assert score_output(expected, suspected_token_limit=True).outcome == "pass"


def test_score_output_classifies_failures() -> None:
    assert score_output("{not json").outcome == "invalid_json"
    assert score_output("[]").outcome == "invalid_schema"
    assert score_output('{"edits":[]}').outcome == "invalid_schema"
    wrong = {
        "edits": [{
            "path": "context_probe.py",
            "old": "nope",
            "new": "still nope",
        }]
    }
    assert score_output(json.dumps(wrong)).outcome == "wrong_edit"
    assert score_output(
        json.dumps(wrong),
        suspected_token_limit=True,
    ).outcome == "suspected_token_limit"


def test_score_output_retrieve_mode_applies_the_edit() -> None:
    from mlx_swarm.context_benchmark_prompt import TARGET_NEW, TARGET_OLD, source_body

    source = source_body(3, "middle", 1, 7)
    expected = json.dumps(expected_manifest(), ensure_ascii=False)
    assert score_output(expected, source=source).outcome == "pass"

    def manifest(old: str, new: str, path: str = "context_probe.py") -> str:
        return json.dumps({"edits": [{"path": path, "old": old, "new": new}]})

    # Smaller and larger unique anchors reproduce the same file and pass.
    smallest = score_output(manifest('"before"', '"after"'), source=source)
    assert smallest.outcome == "pass"
    assert "Applied edit" in smallest.detail
    no_newline = manifest(TARGET_OLD.rstrip("\n"), TARGET_NEW.rstrip("\n"))
    assert score_output(no_newline, source=source).outcome == "pass"
    larger = manifest(
        source[source.index(TARGET_OLD) - 10 : source.index(TARGET_OLD) + len(TARGET_OLD)],
        source[source.index(TARGET_OLD) - 10 : source.index(TARGET_OLD)] + TARGET_NEW,
    )
    assert score_output(larger, source=source).outcome == "pass"

    # Copy mode keeps exact manifest equality for the same outputs.
    assert score_output(no_newline).outcome == "wrong_edit"

    ambiguous = score_output(manifest("    return", "    return "), source=source)
    assert ambiguous.outcome == "wrong_edit"
    assert "found 4" in ambiguous.detail
    wrong_function = score_output(
        manifest("distractor_1_s7_t1", "distractor_x"), source=source
    )
    assert wrong_function.outcome == "wrong_edit"
    assert "does not reproduce" in wrong_function.detail
    assert score_output(manifest("", "x"), source=source).outcome == "wrong_edit"
    assert score_output(manifest('"before"', '"before"'), source=source).outcome == "wrong_edit"
    wrong_path = score_output(manifest('"before"', '"after"', "other.py"), source=source)
    assert wrong_path.outcome == "wrong_edit"
    assert "not context_probe.py" in wrong_path.detail
    assert score_output("[]", source=source).outcome == "invalid_schema"
    assert score_output(
        manifest("    return", "    return "),
        suspected_token_limit=True,
        source=source,
    ).outcome == "suspected_token_limit"


def test_score_output_retrieve_mode_rejects_ambiguous_anchor_with_decoys() -> None:
    from mlx_swarm.context_benchmark_prompt import TARGET_NEW, TARGET_OLD, source_body

    source = source_body(6, "middle", 1, 7, decoys=2)

    def manifest(old: str, new: str) -> str:
        return json.dumps({"edits": [{"path": "context_probe.py", "old": old, "new": new}]})

    ambiguous = score_output(manifest('"before"', '"after"'), source=source)
    assert ambiguous.outcome == "wrong_edit"
    assert "found 3" in ambiguous.detail
    pinned = manifest(TARGET_OLD.rstrip("\n"), TARGET_NEW.rstrip("\n"))
    assert score_output(pinned, source=source).outcome == "pass"
    decoy_edit = manifest('def decoy_0_s7_t1() -> str:\n    return "before"', 'def decoy_0_s7_t1() -> str:\n    return "after"')
    assert score_output(decoy_edit, source=source).outcome == "wrong_edit"
