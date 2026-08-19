"""Context benchmark CLI entry point.

Parses arguments, runs the benchmark matrix, and writes
results.json and report.md to the specified output directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .context_benchmark_aggregate import aggregate_records
from .context_benchmark_case import run_case
from .context_benchmark_runtime import run_benchmark
from .context_benchmark_report import render_report
from .context_benchmark_types import TierPositionTrial
from .context_benchmark_prompt import build_prompt
from .context_benchmark_fit import token_fit_out_of_tolerance
from .backend import MLXBatchBackend, SwarmConfig
from .contracts import SwarmConfig as SwarmConfigType

__all__ = ["build_parser", "main"]


def build_parser(argv: Sequence[str] | None = None) -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        description="Run context capacity benchmark matrix.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the swarm configuration JSON file.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory to write results.json and report.md.",
    )
    parser.add_argument(
        "--tiers",
        type=int,
        nargs="+",
        default=[2048, 4096, 8192, 16384, 32768],
        help="Context capacity tiers to test.",
    )
    parser.add_argument(
        "--positions",
        type=str,
        nargs="+",
        default=["start", "middle", "end"],
        help="Prompt positions to test.",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=3,
        help="Number of trials per position.",
    )
    parser.add_argument(
        "--tolerance-tokens",
        type=int,
        default=32,
        help="Token tolerance for fitting.",
    )
    parser.add_argument(
        "--max-generation-tokens",
        type=int,
        default=512,
        help="Maximum tokens for generation.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducibility.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the benchmark and write results."""
    parser = build_parser(argv)
    args = parser.parse_args(argv)

    config_path: Path = args.config
    output_dir: Path = args.output_dir
    tiers: list[int] = args.tiers
    positions: list[str] = args.positions
    trials: int = args.trials
    tolerance_tokens: int = args.tolerance_tokens
    max_generation_tokens: int = args.max_generation_tokens
    seed: int | None = args.seed

    output_dir.mkdir(parents=True, exist_ok=True)

    payload = run_benchmark(
        config_path=config_path,
        tiers=tiers,
        positions=positions,
        trials=trials,
        tolerance_tokens=tolerance_tokens,
        max_generation_tokens=max_generation_tokens,
        seed=seed,
        backend_factory=MLXBatchBackend,
    )

    results_path = output_dir / "results.json"
    results_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    report_text = render_report(payload)
    report_path = output_dir / "report.md"
    report_path.write_text(report_text, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
