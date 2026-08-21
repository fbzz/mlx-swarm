"""Tests for deterministic context-benchmark prompt construction."""

from __future__ import annotations

import pytest

from mlx_swarm.context_benchmark_prompt import (
    SOURCE_BEGIN,
    SOURCE_END,
    TARGET_NEW,
    TARGET_OLD,
    build_prompt,
    distractor_unit,
    manifest_json,
    parse_positions,
    parse_positive_int_csv,
    source_body,
)


def test_parse_positive_int_csv_accepts_ordered_values() -> None:
    assert parse_positive_int_csv("2048, 8192") == (2048, 8192)


@pytest.mark.parametrize(
    "text",
    ["", " ,2", "0,1", "-1", "1,1", "1,x"],
)
def test_parse_positive_int_csv_rejects_invalid(text: str) -> None:
    with pytest.raises(ValueError):
        parse_positive_int_csv(text)


def test_parse_positions_accepts_ordered_values() -> None:
    assert parse_positions("start,end") == ("start", "end")


@pytest.mark.parametrize(
    "text",
    ["", "start, start", "top", "start,middle,start"],
)
def test_parse_positions_rejects_invalid(text: str) -> None:
    with pytest.raises(ValueError):
        parse_positions(text)


def test_distractor_unit_is_deterministic_and_valid_python() -> None:
    first = distractor_unit(2, 9, 1)
    second = distractor_unit(2, 9, 1)
    other = distractor_unit(3, 9, 1)
    assert first == second
    assert first != other
    assert "distractor_2_s9_t1" in first
    compile(first, "<distractor>", "exec")


def test_build_prompt_places_one_old_anchor() -> None:
    for position in ("start", "middle", "end"):
        prompt = build_prompt(4, position, 1, 7)
        source = source_body(4, position, 1, 7)
        assert source.count(TARGET_OLD) == 1
        assert TARGET_NEW not in source
        assert SOURCE_BEGIN + source + SOURCE_END in prompt
        assert prompt.startswith("Return only one JSON object.")
        assert prompt.endswith("Now return only the JSON object. Do not rewrite the file.\n")
        assert manifest_json() in prompt
        first = source.find("distractor_0_s7_t1")
        last = source.find("distractor_3_s7_t1")
        target = source.find(TARGET_OLD)
        assert first != -1 and last != -1 and target != -1
        if position == "start":
            assert target < first
        elif position == "end":
            assert target > last
        else:
            assert first < target < last
        assert build_prompt(4, position, 1, 7) == prompt


def test_source_body_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        source_body(-1, "start", 1, 7)
    with pytest.raises(ValueError):
        source_body(1, "top", 1, 7)
    with pytest.raises(ValueError):
        source_body(1, "start", 0, 7)


def test_retrieve_prompt_names_the_change_without_the_manifest() -> None:
    from mlx_swarm.context_benchmark_prompt import MODES, parse_mode

    assert MODES == ("copy", "retrieve")
    assert parse_mode(" retrieve ") == "retrieve"
    with pytest.raises(ValueError):
        parse_mode("guess")
    with pytest.raises(ValueError):
        build_prompt(4, "start", 1, 7, "guess")
    for position in ("start", "middle", "end"):
        prompt = build_prompt(4, position, 1, 7, "retrieve")
        source = source_body(4, position, 1, 7)
        assert SOURCE_BEGIN + source + SOURCE_END in prompt
        assert manifest_json() not in prompt
        assert TARGET_NEW not in prompt
        assert "function named target" in prompt
        assert "occurs once" in prompt
        assert prompt.startswith("Return only one JSON object.")
        assert prompt.endswith("Now return only the JSON object. Do not rewrite the file.\n")
        assert build_prompt(4, position, 1, 7, "retrieve") == prompt
        assert build_prompt(4, position, 1, 7, "copy") == build_prompt(4, position, 1, 7)


def test_decoys_make_the_bare_literal_ambiguous_but_keep_the_target_unique() -> None:
    from mlx_swarm.context_benchmark_prompt import decoy_unit

    assert decoy_unit(1, 7, 2) == 'def decoy_1_s7_t2() -> str:\n    return "before"\n'
    for position in ("start", "middle", "end"):
        plain = source_body(6, position, 1, 7)
        decoyed = source_body(6, position, 1, 7, decoys=3)
        assert plain.count('"before"') == 1
        assert decoyed.count('"before"') == 4
        assert decoyed.count(TARGET_OLD) == 1
        assert decoyed.count("def decoy_") == 3
        assert decoyed.count("def distractor_") == 6
        assert source_body(6, position, 1, 7, decoys=3) == decoyed
        prompt = build_prompt(6, position, 1, 7, "retrieve", 3)
        assert SOURCE_BEGIN + decoyed + SOURCE_END in prompt
        assert build_prompt(6, position, 1, 7, "copy", 3) != build_prompt(6, position, 1, 7, "copy")
    # Decoys are spread through the body, not bunched at one end.
    body = source_body(10, "start", 1, 7, decoys=3)
    slots = [body.index(f"def decoy_{i}_") for i in range(3)]
    assert slots == sorted(slots)
    assert body.index("def distractor_0_") < slots[0] < body.index("def distractor_9_")
    with pytest.raises(ValueError):
        source_body(6, "start", 1, 7, decoys=-1)
