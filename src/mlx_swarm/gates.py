"""Gate evaluation — deterministic output validation for untrusted worker responses."""
# @lat: [[Gates]]

from __future__ import annotations

import json
import re
from typing import Any

from .contracts import OutputGate

_SINGLE_CODE_FENCE = re.compile(
    r"\A\s*```[a-zA-Z0-9_-]*[ \t]*\r?\n"
    r"(?P<body>[\s\S]*?)\r?\n```\s*\Z"
)

_SINGLE_JSON_MANIFEST = re.compile(
    r"\A<manifest>[ \t]*\r?\n?"
    r"(?P<body>[\s\S]*?)"
    r"\r?\n?</manifest>\Z"
)

_AGGRESSIVE_PREAMBLE = re.compile(
    r"\A\s*(?:Here(?:'s| is| are)[^.\n]*\.\s*|Sure[^.\n]*\.\s*|Let me[^.\n]*\.\s*|I'll[^.\n]*\.\s*)+",
    re.IGNORECASE,
)

_MODEL_SPECIAL_TOKENS = ("<|im_end|>", "<|endoftext|>", "<|im_start|>")
_JSON_UNSET = object()


def strip_preamble(text: str) -> str:
    """Remove common LLM preambles like 'Here is the code.' before actual output."""
    return _AGGRESSIVE_PREAMBLE.sub("", text, count=1)


def normalize_output(output: str, gate: OutputGate | None) -> tuple[str, list[str]]:
    """Normalize model protocol artifacts before deterministic validation."""
    normalized = output
    normalizations: list[str] = []

    if "</think>" in normalized:
        normalized = normalized.rsplit("</think>", 1)[1]
        normalizations.append("thinking-block")

    token_positions = [
        normalized.find(token)
        for token in _MODEL_SPECIAL_TOKENS
        if token in normalized
    ]
    if token_positions:
        normalized = normalized[:min(token_positions)]
        normalizations.append("model-special-token-suffix")

    without_preamble = strip_preamble(normalized)
    if without_preamble != normalized:
        normalized = without_preamble
        normalizations.append("preamble")

    stripped = normalized.strip()
    if stripped != normalized:
        normalized = stripped
        normalizations.append("outer-whitespace")

    if gate is not None and gate.strip_single_code_fence:
        match = _SINGLE_CODE_FENCE.fullmatch(normalized)
        if match is not None:
            normalized = match.group("body")
            normalizations.append("single-code-fence")

    if gate is not None and gate.output_format == "json":
        match = _SINGLE_JSON_MANIFEST.fullmatch(normalized)
        if match is not None:
            normalized = match.group("body").strip()
            normalizations.append("single-json-manifest")

    return normalized, normalizations


def evaluate_gate(output: str, gate: OutputGate | None) -> dict[str, Any]:
    """Evaluate output against the gate and return a structured result."""
    if gate is None:
        return {"configured": False, "passed": True, "violations": [], "normalizations": []}

    normalized, normalizations = normalize_output(output, gate)

    violations: list[dict[str, str]] = []
    parsed_json: Any = _JSON_UNSET

    if len(normalized) > gate.max_characters:
        violations.append({
            "id": "max-characters",
            "kind": "size",
            "message": f"Output has {len(normalized)} chars; max is {gate.max_characters}.",
        })

    if gate.output_format == "json":
        try:
            parsed_json = json.loads(normalized)
        except json.JSONDecodeError as e:
            violations.append({
                "id": "valid-json",
                "kind": "format",
                "message": f"Not valid JSON: line {e.lineno}, col {e.colno}.",
            })

    if gate.python_syntax:
        if not normalized:
            violations.append({
                "id": "python-syntax",
                "kind": "format",
                "message": "Python output is empty.",
            })
        else:
            try:
                compile(normalized, "<worker-output>", "exec")
            except SyntaxError as exc:
                violations.append({
                    "id": "python-syntax",
                    "kind": "format",
                    "message": (
                        f"Invalid Python syntax: line {exc.lineno}, "
                        f"col {exc.offset}: {exc.msg}."
                    ),
                })

    if parsed_json is not _JSON_UNSET and (
        gate.json_required_keys
        or gate.json_allowed_keys
        or gate.json_field_enums
    ):
        if not isinstance(parsed_json, dict):
            violations.append({
                "id": "json-object",
                "kind": "schema",
                "message": "JSON output must be an object.",
            })
        else:
            missing = set(gate.json_required_keys) - parsed_json.keys()
            if missing:
                violations.append({
                    "id": "json-required-keys",
                    "kind": "schema",
                    "message": f"Missing JSON keys: {', '.join(sorted(missing))}.",
                })
            if gate.json_allowed_keys:
                unknown = parsed_json.keys() - set(gate.json_allowed_keys)
                if unknown:
                    violations.append({
                        "id": "json-allowed-keys",
                        "kind": "schema",
                        "message": f"Unknown JSON keys: {', '.join(sorted(unknown))}.",
                    })
            for field_name, choices in gate.json_field_enums.items():
                if (
                    field_name in parsed_json
                    and parsed_json[field_name] not in choices
                ):
                    violations.append({
                        "id": f"json-enum-{field_name}",
                        "kind": "schema",
                        "message": (
                            f"JSON field {field_name!r} must be one of: "
                            f"{', '.join(map(repr, choices))}."
                        ),
                    })

    for rule in gate.required_patterns:
        if re.search(rule.pattern, normalized, re.MULTILINE) is None:
            violations.append({
                "id": rule.identifier,
                "kind": "required-pattern",
                "message": f"Required pattern not found: {rule.identifier}.",
            })

    for rule in gate.forbidden_patterns:
        if re.search(rule.pattern, normalized, re.MULTILINE) is not None:
            violations.append({
                "id": rule.identifier,
                "kind": "forbidden-pattern",
                "message": f"Forbidden pattern found: {rule.identifier}.",
            })

    return {
        "configured": True,
        "passed": not violations,
        "violations": violations,
        "normalizations": normalizations,
    }


def gate_feedback_for_repair(gate_result: dict[str, Any]) -> str:
    """Generate a feedback string to inject into a repair prompt."""
    violations = gate_result.get("violations", [])
    if not violations:
        return ""
    lines = ["Your previous output was REJECTED for the following reasons:"]
    for v in violations:
        lines.append(f"- [{v['kind']}] {v['message']}")
    lines.append("")
    lines.append("Fix these issues and return ONLY the corrected output.")
    return "\n".join(lines)
