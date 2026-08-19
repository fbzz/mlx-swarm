"""Tests for context benchmark CLI entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from src.mlx_swarm.context_benchmark import build_parser, main


def _minimal_payload() -> dict:
    """Return a minimal valid benchmark payload."""
    return {
        "tiers": [2048, 8192],
        "positions": ["start", "end"],
        "trials": 1,
        "results": [],
        "summary": {},
    }


def _minimal_report() -> str:
    """Return a fixed Markdown report string."""
    return "# Context Benchmark Report\n\nDone.\n"


def test_main_writes_expected_artifacts(tmp_path: Path) -> None:
    """Assert main writes results.json and report.md with correct content."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"model": "test"}), encoding="utf-8")
    output_dir = tmp_path / "output"

    with mock.patch(
        "src.mlx_swarm.context_benchmark.run_benchmark",
        return_value=_minimal_payload(),
    ) as mock_run, mock.patch(
        "src.mlx_swarm.context_benchmark.render_report",
        return_value=_minimal_report(),
    ) as mock_render:
        exit_code = main(
            [
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
                "--tiers",
                "2048",
                "8192",
                "--positions",
                "start",
                "end",
                "--trials",
                "1",
                "--tolerance-tokens",
                "16",
                "--max-generation-tokens",
                "256",
                "--seed",
                "9",
            ],
        )

    assert exit_code == 0
    mock_run.assert_called_once()
    mock_render.assert_called_once()

    results_path = output_dir / "results.json"
    report_path = output_dir / "report.md"

    assert results_path.exists()
    assert report_path.exists()

    results_text = results_path.read_text(encoding="utf-8")
    assert results_text.endswith("\n")
    parsed_results = json.loads(results_text)
    assert parsed_results == _minimal_payload()

    report_text = report_path.read_text(encoding="utf-8")
    assert report_text == _minimal_report()


def test_parser_requires_config_and_output() -> None:
    """Assert missing required arguments raise SystemExit."""
    parser = build_parser([])
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parser_accepts_valid_args() -> None:
    """Assert valid arguments parse without error."""
    parser = build_parser([])
    args = parser.parse_args(
        [
            "--config",
            "config.json",
            "--output-dir",
            "output",
        ],
    )
    assert args.config == Path("config.json")
    assert args.output_dir == Path("output")
    assert args.tiers == [2048, 4096, 8192, 16384, 32768]
    assert args.positions == ["start", "middle", "end"]
    assert args.trials == 3
    assert args.tolerance_tokens == 32
    assert args.max_generation_tokens == 512
    assert args.seed is None
