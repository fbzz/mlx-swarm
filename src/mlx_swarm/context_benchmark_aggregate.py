"""Context benchmark aggregation logic."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = ["aggregate_records"]


def aggregate_records(
    records: Sequence[Mapping[str, Any]],
    tiers: Sequence[int],
    positions: Sequence[str],
    trials: int,
) -> dict[str, Any]:
    """Aggregate benchmark records into summary statistics.

    Counts total records, passes, and failure counts for every observed
    non-pass outcome. Sums token and time metrics. Computes pass rates
    by tier and position, handling missing cells gracefully.

    Args:
        records: The list of individual benchmark case records.
        tiers: The sequence of requested context tiers.
        positions: The sequence of requested prompt positions.
        trials: The number of trials expected per position.

    Returns:
        A dictionary containing aggregated statistics.
    """
    total = len(records)
    passes = 0
    failure_counts: dict[str, int] = {}
    total_rendered_prompt_tokens = 0
    total_prompt_tokens = 0
    total_generation_tokens = 0
    total_generation_seconds = 0.0
    total_load_seconds = 0.0

    for record in records:
        outcome = record.get("outcome", "")
        if outcome == "pass":
            passes += 1
        else:
            failure_counts[outcome] = failure_counts.get(outcome, 0) + 1

        total_rendered_prompt_tokens += record.get("renderedTokens", 0)
        total_prompt_tokens += record.get("promptTokens", 0)
        total_generation_tokens += record.get("generationTokens", 0)
        total_generation_seconds += record.get("generationSeconds", 0.0)
        total_load_seconds += record.get("loadSeconds", 0.0)

    pass_rates_by_tier_position: dict[str, dict[str, Any]] = {}
    for tier in tiers:
        for position in positions:
            tier_key = f"tier{tier}"
            case_key = f"{tier_key}_{position}"
            case_records = [
                r for r in records
                if r.get("tier") == tier and r.get("position") == position
            ]
            case_total = len(case_records)
            case_passed = sum(
                1 for r in case_records if r.get("outcome") == "pass"
            )
            rate = case_passed / case_total if case_total > 0 else None
            pass_rates_by_tier_position[case_key] = {
                "passed": case_passed,
                "total": case_total,
                "rate": rate,
            }

    highest_all_pass_tier: int | None = None
    for tier in sorted(tiers, reverse=True):
        tier_key = f"tier{tier}"
        all_positions_complete = all(
            pass_rates_by_tier_position.get(f"{tier_key}_{pos}", {}).get("total") == trials
            for pos in positions
        )
        all_positions_passed = all(
            pass_rates_by_tier_position.get(f"{tier_key}_{pos}", {}).get("passed") == trials
            for pos in positions
        )
        if all_positions_complete and all_positions_passed:
            highest_all_pass_tier = tier
            break

    return {
        "total": total,
        "passes": passes,
        "failureCounts": failure_counts,
        "totalRenderedPromptTokens": total_rendered_prompt_tokens,
        "totalPromptTokens": total_prompt_tokens,
        "totalGenerationTokens": total_generation_tokens,
        "totalGenerationSeconds": total_generation_seconds,
        "totalLoadSeconds": total_load_seconds,
        "passRatesByTierPosition": pass_rates_by_tier_position,
        "highestAllPassTier": highest_all_pass_tier,
        "requestedTiers": list(tiers),
        "requestedPositions": list(positions),
        "trials": trials,
    }
