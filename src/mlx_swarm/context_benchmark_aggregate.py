"""Aggregate per-case context-benchmark records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["aggregate_records"]


def aggregate_records(
    records: Sequence[Mapping[str, Any]],
    tiers: Sequence[int],
    positions: Sequence[str],
    trials: int,
) -> dict[str, Any]:
    passes = 0
    failure_counts: dict[str, int] = {}
    totals = {
        "renderedPromptTokens": 0,
        "promptTokens": 0,
        "generationTokens": 0,
        "generationSeconds": 0.0,
        "loadSeconds": 0.0,
    }
    for record in records:
        outcome = str(record.get("outcome") or "")
        if outcome == "pass":
            passes += 1
        elif outcome:
            failure_counts[outcome] = failure_counts.get(outcome, 0) + 1
        totals["renderedPromptTokens"] += int(record.get("renderedTokens") or 0)
        totals["promptTokens"] += int(record.get("promptTokens") or 0)
        totals["generationTokens"] += int(record.get("generationTokens") or 0)
        totals["generationSeconds"] += float(
            record.get("generationSeconds") or 0.0
        )
        totals["loadSeconds"] += float(record.get("loadSeconds") or 0.0)

    pass_rates: dict[str, dict[str, Any]] = {}
    for tier in tiers:
        cells: dict[str, Any] = {}
        for position in positions:
            matching = [
                record
                for record in records
                if record.get("tier") == tier
                and record.get("position") == position
            ]
            passed = sum(1 for record in matching if record.get("outcome") == "pass")
            total = len(matching)
            cells[position] = {
                "passed": passed,
                "total": total,
                "rate": (passed / total) if total else None,
            }
        pass_rates[str(tier)] = cells

    highest: int | None = None
    for tier in sorted(tiers):
        cells = pass_rates[str(tier)]
        if all(
            cells[position]["total"] == trials and cells[position]["passed"] == trials
            for position in positions
        ):
            highest = tier

    return {
        "total": len(records),
        "passes": passes,
        "failureCounts": failure_counts,
        "totalRenderedPromptTokens": totals["renderedPromptTokens"],
        "totalPromptTokens": totals["promptTokens"],
        "totalGenerationTokens": totals["generationTokens"],
        "totalGenerationSeconds": totals["generationSeconds"],
        "totalLoadSeconds": totals["loadSeconds"],
        "passRatesByTierPosition": pass_rates,
        "highestAllPassTier": highest,
        "requestedTiers": list(tiers),
        "requestedPositions": list(positions),
        "trials": trials,
    }
