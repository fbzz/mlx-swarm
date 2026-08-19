"""Context benchmark prompt construction and parsing utilities."""

from __future__ import annotations

from typing import Any, Dict, Tuple

from .context_benchmark_types import POSITIONS

__all__ = [
    "SYNTHETIC_PATH",
    "TARGET_OLD",
    "TARGET_NEW",
    "parse_positive_int_csv",
    "parse_positions",
    "distractor_unit",
    "build_prompt",
]

SYNTHETIC_PATH = "context_probe.py"


def _target_old() -> str:
    return "before"


def _target_new() -> str:
    return "after"


TARGET_OLD = f"def target() -> str:\n    return 'before'"
TARGET_NEW = f"def target() -> str:\n    return 'after'"


def parse_positive_int_csv(text: str) -> Tuple[int, ...]:
    """Parse a comma-separated string of positive unique integers.

    Raises ValueError if the string is empty, contains non-integers,
    non-positive values, or duplicates.
    """
    if not text or not text.strip():
        raise ValueError("Input text must not be empty.")

    parts = text.split(",")
    ints: list[int] = []
    seen: set[int] = set()

    for part in parts:
        part = part.strip()
        if not part:
            continue
        try:
            val = int(part)
        except ValueError:
            raise ValueError(f"Invalid integer value: {part}")
        if val <= 0:
            raise ValueError(f"Integer must be positive, got: {val}")
        if val in seen:
            raise ValueError(f"Duplicate integer value: {val}")
        seen.add(val)
        ints.append(val)

    if not ints:
        raise ValueError("No valid positive integers found.")

    return tuple(ints)


def parse_positions(text: str) -> Tuple[str, ...]:
    """Parse a comma-separated string of unique valid positions.

    Raises ValueError if the string is empty, contains invalid positions,
    or duplicates.
    """
    if not text or not text.strip():
        raise ValueError("Input text must not be empty.")

    parts = text.split(",")
    positions: list[str] = []
    seen: set[str] = set()

    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part not in POSITIONS:
            raise ValueError(f"Invalid position: {part}. Must be one of {POSITIONS}")
        if part in seen:
            raise ValueError(f"Duplicate position: {part}")
        seen.add(part)
        positions.append(part)

    if not positions:
        raise ValueError("No valid positions found.")

    return tuple(positions)


def distractor_unit(index: int, seed: int, trial: int) -> str:
    """Generate a deterministic, syntactically valid Python function.

    The function name and body are derived from index, seed, and trial
    to ensure uniqueness without using process-randomized hashing.
    """
    # Deterministic name generation
    func_name = f"distractor_{index}_s{seed}_t{trial}"
    # Simple deterministic body
    body = f'    return {index + seed + trial}'
    return f"def {func_name}() -> int:\n{body}"


def build_prompt(
    unit_count: int,
    position: str,
    trial: int,
    seed: int,
) -> str:
    """Build the benchmark prompt string.

    Args:
        unit_count: Number of distractor units (non-negative).
        position: One of POSITIONS ('start', 'middle', 'end').
        trial: Trial number (positive).
        seed: Seed for deterministic generation (positive).

    Returns:
        The formatted prompt string.

    Raises:
        ValueError: If arguments are invalid.
    """
    if unit_count < 0:
        raise ValueError("unit_count must be non-negative.")
    if position not in POSITIONS:
        raise ValueError(f"position must be one of {POSITIONS}")
    if trial <= 0:
        raise ValueError("trial must be positive.")
    if seed <= 0:
        raise ValueError("seed must be positive.")

    units = [distractor_unit(i, seed, trial) for i in range(unit_count)]
    
    if position == "start":
        target_before = TARGET_OLD
        target_after = ""
    elif position == "middle":
        target_before = TARGET_OLD
        target_after = TARGET_NEW
    else:  # end
        target_before = ""
        target_after = TARGET_NEW

    parts = []
    if target_before:
        parts.append(f"# Target file: {SYNTHETIC_PATH}")
        parts.append(target_before)
    parts.extend(units)
    if target_after:
        parts.append(target_after)
    
    prompt_body = "\n".join(parts)
    return f"""# Target file: {SYNTHETIC_PATH}

{prompt_body}

Please provide exactly one edit to fix the target file.
Return only valid JSON: {{"edits":[{{"path":"{SYNTHETIC_PATH}","old":"{TARGET_OLD}","new":"{TARGET_NEW}"}}]}}""".strip()
