"""Tests for the context-benchmark CLI."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from mlx_swarm.context_benchmark import CASES_FILENAME, build_parser, load_cases, main


def _record(case_id: str, outcome: str = "pass", seed: int = 9) -> dict[str, Any]:
    tier, position, trial = case_id.split("-")
    return {
        "caseId": case_id,
        "tier": int(tier),
        "position": position,
        "trial": int(trial),
        "seed": seed,
        "outcome": outcome,
        "renderedTokens": int(tier) + 3,
        "generationSeconds": 1.5,
        "wallSeconds": 2.25,
        "peakMemoryGigabytes": 20.5,
    }


def test_main_writes_expected_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "schemaVersion": 1,
        "aggregate": {"total": 0, "passes": 0},
        "cases": [],
    }
    report = "# Context Benchmark Report\n"
    captured: dict[str, object] = {}

    def fake_run_benchmark(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return payload

    monkeypatch.setattr(
        "mlx_swarm.context_benchmark.run_benchmark",
        fake_run_benchmark,
    )
    monkeypatch.setattr(
        "mlx_swarm.context_benchmark.render_report",
        lambda _payload: report,
    )
    output_dir = tmp_path / "output"
    exit_code = main([
        "--config",
        str(tmp_path / "swarm.json"),
        "--output-dir",
        str(output_dir),
        "--tiers",
        "2048,8192",
        "--positions",
        "start,end",
        "--trials",
        "1",
        "--tolerance-tokens",
        "16",
        "--max-generation-tokens",
        "256",
        "--seed",
        "9",
    ])
    assert exit_code == 0
    assert captured["tiers"] == (2048, 8192)
    assert captured["positions"] == ("start", "end")
    assert captured["trials"] == 1
    assert captured["tolerance_tokens"] == 16
    assert captured["max_generation_tokens"] == 256
    assert captured["seed"] == 9
    assert captured["mode"] == "copy"
    assert captured["decoys"] == 0
    assert captured["completed_cases"] == []
    assert callable(captured["on_case"])
    results_path = output_dir / "results.json"
    report_path = output_dir / "report.md"
    assert sorted(path.name for path in output_dir.iterdir()) == [
        CASES_FILENAME,
        "report.md",
        "results.json",
    ]
    text = results_path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert json.loads(text) == payload
    assert report_path.read_text(encoding="utf-8") == report
    assert (output_dir / CASES_FILENAME).read_text(encoding="utf-8") == ""


def test_main_checkpoints_each_case_and_reports_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [_record("2048-start-1"), _record("2048-end-1", "wrong_edit")]

    def fake_run_benchmark(**kwargs: Any) -> dict[str, Any]:
        on_case = kwargs["on_case"]
        for index, record in enumerate(records, start=1):
            on_case(record, index, len(records))
        return {"schemaVersion": 1, "aggregate": {}, "cases": records}

    monkeypatch.setattr(
        "mlx_swarm.context_benchmark.run_benchmark",
        fake_run_benchmark,
    )
    monkeypatch.setattr(
        "mlx_swarm.context_benchmark.render_report",
        lambda _payload: "report\n",
    )
    output_dir = tmp_path / "output"
    stderr = io.StringIO()
    assert main(
        ["--config", "c.json", "--output-dir", str(output_dir)],
        stderr=stderr,
    ) == 0
    lines = (output_dir / CASES_FILENAME).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == records
    progress = stderr.getvalue().splitlines()
    assert progress == [
        "[1/2] 2048-start-1 pass rendered=2051 gen=1.5s wall=2.2s peak=20.5GB",
        "[2/2] 2048-end-1 wrong_edit rendered=2051 gen=1.5s wall=2.2s peak=20.5GB",
    ]


def test_main_quiet_suppresses_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_benchmark(**kwargs: Any) -> dict[str, Any]:
        kwargs["on_case"](_record("2048-start-1"), 1, 1)
        return {"schemaVersion": 1, "aggregate": {}, "cases": []}

    monkeypatch.setattr(
        "mlx_swarm.context_benchmark.run_benchmark",
        fake_run_benchmark,
    )
    monkeypatch.setattr(
        "mlx_swarm.context_benchmark.render_report",
        lambda _payload: "report\n",
    )
    stderr = io.StringIO()
    assert main(
        ["--config", "c.json", "--output-dir", str(tmp_path), "--quiet"],
        stderr=stderr,
    ) == 0
    assert stderr.getvalue() == ""
    assert (tmp_path / CASES_FILENAME).read_text(encoding="utf-8").count("\n") == 1


def test_main_resume_passes_checkpointed_cases_and_keeps_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    earlier = [_record("2048-start-1"), _record("2048-middle-1")]
    cases_path = output_dir / CASES_FILENAME
    cases_path.write_text(
        "".join(json.dumps(record) + "\n" for record in earlier)
        + "\n{not json}\n",
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    def fake_run_benchmark(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        kwargs["on_case"](_record("2048-end-1"), 3, 3)
        return {"schemaVersion": 1, "aggregate": {}, "cases": []}

    monkeypatch.setattr(
        "mlx_swarm.context_benchmark.run_benchmark",
        fake_run_benchmark,
    )
    monkeypatch.setattr(
        "mlx_swarm.context_benchmark.render_report",
        lambda _payload: "report\n",
    )
    assert main(
        ["--config", "c.json", "--output-dir", str(output_dir), "--resume"],
        stderr=io.StringIO(),
    ) == 0
    assert captured["completed_cases"] == earlier
    lines = [
        json.loads(line)
        for line in cases_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and line.strip() != "{not json}"
    ]
    assert [line["caseId"] for line in lines] == [
        "2048-start-1",
        "2048-middle-1",
        "2048-end-1",
    ]


def test_main_without_resume_truncates_stale_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases_path = tmp_path / CASES_FILENAME
    cases_path.write_text(json.dumps(_record("2048-start-1")) + "\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_run_benchmark(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"schemaVersion": 1, "aggregate": {}, "cases": []}

    monkeypatch.setattr(
        "mlx_swarm.context_benchmark.run_benchmark",
        fake_run_benchmark,
    )
    monkeypatch.setattr(
        "mlx_swarm.context_benchmark.render_report",
        lambda _payload: "report\n",
    )
    assert main(["--config", "c.json", "--output-dir", str(tmp_path)]) == 0
    assert captured["completed_cases"] == []
    assert cases_path.read_text(encoding="utf-8") == ""


def test_load_cases_handles_missing_and_malformed(tmp_path: Path) -> None:
    assert load_cases(tmp_path / "missing.jsonl") == []
    path = tmp_path / CASES_FILENAME
    path.write_text('{"caseId": "a"}\n\nnot json\n[1, 2]\n', encoding="utf-8")
    assert load_cases(path) == [{"caseId": "a"}]


def test_parser_requires_config_and_output() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "--config",
        "config.json",
        "--output-dir",
        "output",
    ])
    assert args.config == Path("config.json")
    assert args.output_dir == Path("output")
    assert args.tiers == "2048,4096,8192,16384,32768"
    assert args.positions == "start,middle,end"
    assert args.trials == 3
    assert args.tolerance_tokens == 32
    assert args.max_generation_tokens == 512
    assert args.seed is None
    assert args.mode == "copy"
    assert args.decoys == 0
    assert args.resume is False
    assert args.quiet is False


def test_main_rejects_negative_decoys(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--config", "c.json", "--output-dir", str(tmp_path), "--decoys", "-1"])


def test_parser_mode_choices() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "--config", "c.json", "--output-dir", "o", "--mode", "retrieve",
    ])
    assert args.mode == "retrieve"
    with pytest.raises(SystemExit):
        parser.parse_args(["--config", "c.json", "--output-dir", "o", "--mode", "guess"])
