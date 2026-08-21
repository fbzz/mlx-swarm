"""CLI for the local-only effective-context benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from .context_benchmark_prompt import (
    MODES,
    parse_mode,
    parse_positions,
    parse_positive_int_csv,
)
from .context_benchmark_report import render_report
from .context_benchmark_runtime import run_benchmark

__all__ = ["CASES_FILENAME", "build_parser", "load_cases", "main"]

# One JSON record per finished case, appended as soon as the case ends, so an
# interrupted matrix keeps its evidence and can be resumed with --resume.
CASES_FILENAME = "cases.jsonl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure exact edit-manifest success by rendered prompt size.",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--tiers",
        default="2048,4096,8192,16384,32768",
    )
    parser.add_argument(
        "--positions",
        default="start,middle,end",
    )
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--tolerance-tokens", type=int, default=32)
    parser.add_argument("--max-generation-tokens", type=int, default=512)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--mode",
        default="copy",
        choices=MODES,
        help=(
            "copy: the prompt states the exact manifest to return; retrieve: "
            "the prompt names the change and the model must find the anchor "
            "and author the manifest (scored by applying the edit)."
        ),
    )
    parser.add_argument(
        "--decoys",
        type=int,
        default=0,
        help=(
            "Number of decoy functions that also return \"before\" spread "
            "through the distractors, so the bare literal is not a unique "
            "anchor (default 0)."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            f"Reuse cases already recorded in <output-dir>/{CASES_FILENAME} "
            "by an earlier run with the same seed, tolerance, and generation "
            "ceiling instead of re-running them."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print one progress line per finished case to stderr.",
    )
    return parser


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Read checkpointed case records, skipping blank or malformed lines."""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _progress_line(record: dict[str, Any], done: int, total: int) -> str:
    return (
        f"[{done}/{total}] {record.get('caseId')} {record.get('outcome')}"
        f" rendered={record.get('renderedTokens')}"
        f" gen={float(record.get('generationSeconds') or 0.0):.1f}s"
        f" wall={float(record.get('wallSeconds') or 0.0):.1f}s"
        f" peak={float(record.get('peakMemoryGigabytes') or 0.0):.1f}GB"
    )


def main(argv: Sequence[str] | None = None, stderr: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.decoys < 0:
        raise SystemExit("--decoys must be non-negative.")
    err = sys.stderr if stderr is None else stderr
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cases_path = args.output_dir / CASES_FILENAME
    completed = load_cases(cases_path) if args.resume else []
    if not args.resume:
        cases_path.write_text("", encoding="utf-8")

    def on_case(record: dict[str, Any], done: int, total: int) -> None:
        with cases_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False))
            handle.write("\n")
        if not args.quiet:
            print(_progress_line(record, done, total), file=err, flush=True)

    payload = run_benchmark(
        config_path=args.config,
        tiers=parse_positive_int_csv(args.tiers),
        positions=parse_positions(args.positions),
        trials=args.trials,
        tolerance_tokens=args.tolerance_tokens,
        max_generation_tokens=args.max_generation_tokens,
        seed=args.seed,
        on_case=on_case,
        completed_cases=completed,
        mode=parse_mode(args.mode),
        decoys=args.decoys,
    )
    (args.output_dir / "results.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "report.md").write_text(
        render_report(payload),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
