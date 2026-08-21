"""Tests for context-benchmark aggregation and report rendering."""

from __future__ import annotations

from mlx_swarm.context_benchmark_aggregate import aggregate_records
from mlx_swarm.context_benchmark_report import render_report


def _record(
    tier: int,
    position: str,
    trial: int,
    outcome: str,
    *,
    rendered: int = 10,
    prompt: int = 11,
    generation: int = 3,
    seconds: float = 0.5,
    load: float = 0.25,
) -> dict[str, object]:
    return {
        "tier": tier,
        "position": position,
        "trial": trial,
        "outcome": outcome,
        "renderedTokens": rendered,
        "promptTokens": prompt,
        "generationTokens": generation,
        "generationSeconds": seconds,
        "loadSeconds": load,
    }


def test_aggregate_records_totals_rates_and_highest_tier() -> None:
    records = [
        _record(8, "start", 1, "pass"),
        _record(8, "start", 2, "pass"),
        _record(8, "end", 1, "pass"),
        _record(8, "end", 2, "wrong_edit", rendered=4, prompt=5, generation=1),
        _record(16, "start", 1, "pass"),
        _record(16, "start", 2, "pass"),
        _record(16, "end", 1, "pass"),
        _record(16, "end", 2, "pass"),
    ]
    summary = aggregate_records(records, (8, 16, 32), ("start", "end"), 2)
    assert summary["total"] == 8
    assert summary["passes"] == 7
    assert summary["failureCounts"] == {"wrong_edit": 1}
    assert summary["totalRenderedPromptTokens"] == 74
    assert summary["totalPromptTokens"] == 82
    assert summary["totalGenerationTokens"] == 22
    assert summary["totalGenerationSeconds"] == 4.0
    assert summary["totalLoadSeconds"] == 2.0
    assert summary["passRatesByTierPosition"]["8"]["end"]["rate"] == 0.5
    assert summary["passRatesByTierPosition"]["32"]["start"] == {
        "passed": 0,
        "total": 0,
        "rate": None,
    }
    assert summary["highestAllPassTier"] == 16


def test_render_report_includes_identity_and_escapes_pipes() -> None:
    payload = {
        "metadata": {
            "modelName": "local/model",
            "modelSha256": "abc123",
            "seed": 9,
            "toleranceTokens": 4,
            "maxGenerationTokens": 32,
        },
        "reproducibility": {
            "seed": 9,
            "toleranceTokens": 4,
            "maxGenerationTokens": 32,
        },
        "aggregate": {
            "total": 2,
            "passes": 1,
            "highestAllPassTier": None,
            "totalRenderedPromptTokens": 20,
            "totalPromptTokens": 22,
            "totalGenerationTokens": 6,
            "totalGenerationSeconds": 1.5,
            "totalLoadSeconds": 0.5,
            "failureCounts": {"wrong_edit": 1},
            "requestedTiers": [8],
            "requestedPositions": ["start"],
            "passRatesByTierPosition": {
                "8": {"start": {"passed": 1, "total": 2, "rate": None}},
            },
        },
        "cases": [{
            "tier": 8,
            "position": "start|mid",
            "trial": 1,
            "renderedTokens": 10,
            "outcome": "pass",
            "generationSeconds": 0.5,
        }],
    }
    report = render_report(payload)
    assert report.endswith("\n")
    assert not report.endswith("\n\n")
    assert "abc123" in report
    assert "Seed**: 9" in report
    assert "Mode**: copy" in report
    assert "Decoys**: 0" in report
    assert "n/a" in report
    assert "wrong_edit: 1" in report
    assert "start\\|mid" in report
    assert "pass" in report
