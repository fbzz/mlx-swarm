"""Tests for gate evaluation and output normalization."""
# @lat: [[Tests#Gates]]

from __future__ import annotations

from mlx_swarm.contracts import OutputGate, GatePattern
from mlx_swarm.gates import (
    evaluate_gate,
    gate_feedback_for_repair,
    normalize_output,
    strip_preamble,
)


def test_strip_preamble() -> None:
    assert strip_preamble("Here is the code.\ndef foo(): pass") == "def foo(): pass"
    assert strip_preamble("Sure! Here you go.\nreturn 42") == "return 42"
    assert strip_preamble("def bar(): pass") == "def bar(): pass"


def test_normalize_output_strips_code_fence() -> None:
    gate = OutputGate(strip_single_code_fence=True)
    code = "```python\ndef foo(): pass\n```"
    normalized, n = normalize_output(code, gate)
    assert normalized == "def foo(): pass"
    assert "single-code-fence" in n


def test_normalize_output_no_fence() -> None:
    gate = OutputGate(strip_single_code_fence=True)
    code = "def foo(): pass"
    normalized, n = normalize_output(code, gate)
    assert normalized == code
    assert n == []


def test_normalize_output_strips_single_json_manifest_wrapper() -> None:
    gate = OutputGate(output_format="json")
    output = '<manifest>\n{"edits": []}\n</manifest>'
    normalized, normalizations = normalize_output(output, gate)
    assert normalized == '{"edits": []}'
    assert normalizations == ["single-json-manifest"]
    assert evaluate_gate(output, gate)["passed"] is True


def test_normalize_output_preserves_manifest_wrapper_for_text_gate() -> None:
    gate = OutputGate(output_format="text")
    output = '<manifest>\n{"edits": []}\n</manifest>'
    normalized, normalizations = normalize_output(output, gate)
    assert normalized == output
    assert normalizations == []


def test_normalize_output_does_not_strip_manifest_with_surrounding_prose() -> None:
    gate = OutputGate(output_format="json")
    output = 'Result:\n<manifest>\n{"edits": []}\n</manifest>'
    normalized, normalizations = normalize_output(output, gate)
    assert normalized == output
    assert normalizations == []
    assert evaluate_gate(output, gate)["passed"] is False


def test_normalize_output_gate_none() -> None:
    normalized, n = normalize_output("anything", None)
    assert normalized == "anything"
    assert n == []


def test_evaluate_gate_no_gate() -> None:
    result = evaluate_gate("anything", None)
    assert result["passed"] is True
    assert result["configured"] is False
    assert result["violations"] == []


def test_evaluate_gate_passes() -> None:
    gate = OutputGate(
        required_patterns=(GatePattern("has-def", r"def "),),
        forbidden_patterns=(GatePattern("no-import", r"^import "),),
    )
    result = evaluate_gate("def foo(): pass", gate)
    assert result["passed"] is True
    assert result["violations"] == []


def test_evaluate_gate_missing_required() -> None:
    gate = OutputGate(
        required_patterns=(GatePattern("has-def", r"def "),),
    )
    result = evaluate_gate("x = 1", gate)
    assert result["passed"] is False
    assert len(result["violations"]) == 1
    assert result["violations"][0]["id"] == "has-def"


def test_evaluate_gate_forbidden_found() -> None:
    gate = OutputGate(
        forbidden_patterns=(GatePattern("no-import", r"^import "),),
    )
    result = evaluate_gate("import os\ndef foo(): pass", gate)
    assert result["passed"] is False
    assert len(result["violations"]) == 1
    assert result["violations"][0]["id"] == "no-import"


def test_evaluate_gate_max_characters() -> None:
    gate = OutputGate(max_characters=10)
    result = evaluate_gate("x" * 100, gate)
    assert result["passed"] is False
    assert result["violations"][0]["id"] == "max-characters"


def test_evaluate_gate_json_format_valid() -> None:
    gate = OutputGate(output_format="json")
    result = evaluate_gate('{"key": "value"}', gate)
    assert result["passed"] is True


def test_evaluate_gate_json_format_invalid() -> None:
    gate = OutputGate(output_format="json")
    result = evaluate_gate("{not json}", gate)
    assert result["passed"] is False
    assert result["violations"][0]["id"] == "valid-json"


def test_gate_feedback_for_repair() -> None:
    gate_result = {
        "passed": False,
        "violations": [
            {"id": "has-def", "kind": "required-pattern", "message": "Required pattern not found: has-def."},
        ],
    }
    feedback = gate_feedback_for_repair(gate_result)
    assert "REJECTED" in feedback
    assert "has-def" in feedback
    assert "Fix these issues" in feedback


def test_gate_feedback_no_violations() -> None:
    assert gate_feedback_for_repair({"passed": True, "violations": []}) == ""


def test_evaluate_gate_with_preamble_and_fence() -> None:
    gate = OutputGate(
        required_patterns=(GatePattern("has-def", r"def "),),
        strip_single_code_fence=True,
    )
    output = "Here is the code.\n```python\ndef foo(): pass\n```"
    result = evaluate_gate(output, gate)
    assert result["passed"] is True


def test_evaluate_gate_python_syntax() -> None:
    gate = OutputGate(python_syntax=True)
    assert evaluate_gate("def valid():\n    return True", gate)["passed"] is True
    result = evaluate_gate("def broken(:", gate)
    assert result["passed"] is False
    assert result["violations"][0]["id"] == "python-syntax"


def test_evaluate_gate_json_schema() -> None:
    gate = OutputGate(
        output_format="json",
        json_required_keys=("verdict", "issues"),
        json_allowed_keys=("verdict", "issues"),
        json_field_enums={"verdict": ("approve", "reject")},
    )
    assert evaluate_gate(
        '{"verdict": "approve", "issues": []}',
        gate,
    )["passed"] is True
    result = evaluate_gate('{"verdict": "maybe"}', gate)
    assert result["passed"] is False
    assert {violation["id"] for violation in result["violations"]} == {
        "json-required-keys",
        "json-enum-verdict",
    }


def test_normalize_output_removes_thinking_and_special_tokens() -> None:
    gate = OutputGate(python_syntax=True)
    output = (
        "private reasoning</think>\n"
        "```python\ndef valid():\n    return True\n```"
        "<|im_end|><|im_start|>user\nrepeated prompt"
    )
    normalized, normalizations = normalize_output(output, gate)
    assert normalized == "def valid():\n    return True"
    assert "thinking-block" in normalizations
    assert "model-special-token-suffix" in normalizations
