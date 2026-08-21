"""Deterministic distractor prompts for the local context benchmark."""

from __future__ import annotations

import json

from .context_benchmark_types import POSITIONS

__all__ = [
    "MODES",
    "SOURCE_BEGIN",
    "SOURCE_END",
    "SYNTHETIC_PATH",
    "TARGET_NEW",
    "TARGET_OLD",
    "build_prompt",
    "distractor_unit",
    "manifest_json",
    "parse_mode",
    "parse_positions",
    "parse_positive_int_csv",
    "source_body",
]

SYNTHETIC_PATH = "context_probe.py"
TARGET_OLD = 'def target() -> str:\n    return "before"\n'
TARGET_NEW = 'def target() -> str:\n    return "after"\n'
SOURCE_BEGIN = "-----BEGIN context_probe.py-----\n"
SOURCE_END = "-----END context_probe.py-----\n"
# copy: the prompt states the exact manifest and the model must return it
# unchanged; retrieve: the prompt names the change and the model must locate
# the anchor in the file and author the manifest itself.
MODES: tuple[str, ...] = ("copy", "retrieve")


def parse_positive_int_csv(text: str) -> tuple[int, ...]:
    parts = [part.strip() for part in text.split(",")]
    if not text.strip() or any(not part for part in parts):
        raise ValueError("CSV must contain one or more positive integers.")
    values: list[int] = []
    seen: set[int] = set()
    for part in parts:
        try:
            value = int(part)
        except ValueError as exc:
            raise ValueError(f"Invalid integer value: {part}") from exc
        if value <= 0:
            raise ValueError(f"Integer must be positive, got: {value}")
        if value in seen:
            raise ValueError(f"Duplicate integer value: {value}")
        seen.add(value)
        values.append(value)
    return tuple(values)


def parse_positions(text: str) -> tuple[str, ...]:
    parts = [part.strip() for part in text.split(",")]
    if not text.strip() or any(not part for part in parts):
        raise ValueError("CSV must contain one or more positions.")
    values: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if part not in POSITIONS:
            raise ValueError(
                f"Invalid position: {part}. Must be one of {POSITIONS}"
            )
        if part in seen:
            raise ValueError(f"Duplicate position: {part}")
        seen.add(part)
        values.append(part)
    return tuple(values)


def parse_mode(text: str) -> str:
    mode = text.strip()
    if mode not in MODES:
        raise ValueError(f"Invalid mode: {text}. Must be one of {MODES}")
    return mode


def distractor_unit(index: int, seed: int, trial: int) -> str:
    name = f"distractor_{index}_s{seed}_t{trial}"
    return (
        f"def {name}() -> int:\n"
        f"    return {index + seed * 1_000 + trial}\n"
    )


def manifest_json() -> str:
    return json.dumps(
        {
            "edits": [{
                "path": SYNTHETIC_PATH,
                "old": TARGET_OLD,
                "new": TARGET_NEW,
            }]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def source_body(
    unit_count: int,
    position: str,
    trial: int,
    seed: int,
) -> str:
    if unit_count < 0:
        raise ValueError("unit_count must be non-negative.")
    if position not in POSITIONS:
        raise ValueError(f"position must be one of {POSITIONS}")
    if trial <= 0:
        raise ValueError("trial must be positive.")

    units = [distractor_unit(index, seed, trial) for index in range(unit_count)]
    if position == "start":
        source_parts = [TARGET_OLD, *units]
    elif position == "end":
        source_parts = [*units, TARGET_OLD]
    else:
        middle = unit_count // 2
        source_parts = [*units[:middle], TARGET_OLD, *units[middle:]]
    return "".join(source_parts)


def build_prompt(
    unit_count: int,
    position: str,
    trial: int,
    seed: int,
    mode: str = "copy",
) -> str:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    source = source_body(unit_count, position, trial, seed)
    if mode == "copy":
        manifest = manifest_json()
        return (
            "Return only one JSON object. Do not emit Python source, Markdown "
            "fences, or prose.\n"
            "Reproduce this object exactly:\n"
            f"{manifest}\n"
            f"The file {SYNTHETIC_PATH} is between the markers. It contains "
            "exactly one occurrence of the old text.\n"
            f"{SOURCE_BEGIN}{source}{SOURCE_END}"
            "Now return only the JSON object. Do not rewrite the file.\n"
        )
    return (
        "Return only one JSON object. Do not emit Python source, Markdown "
        "fences, or prose.\n"
        "Use exactly this shape and no other keys:\n"
        '{"edits":[{"path":"context_probe.py","old":"exact existing text",'
        '"new":"exact replacement text"}]}\n'
        f"The file {SYNTHETIC_PATH} is between the markers.\n"
        f"{SOURCE_BEGIN}{source}{SOURCE_END}"
        "Task: in the function named target, change the returned string "
        'from "before" to "after". Do not change any other function.\n'
        "The old text must be an exact substring of the file that occurs "
        "once; use the smallest sufficient anchor and preserve indentation "
        "and newlines exactly.\n"
        "Now return only the JSON object. Do not rewrite the file.\n"
    )
