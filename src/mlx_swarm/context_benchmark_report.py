"""Markdown rendering for context-benchmark payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["render_report"]


def render_report(payload: Mapping[str, Any]) -> str:
    metadata = payload.get("metadata") or {}
    aggregate = payload.get("aggregate") or {}
    cases = payload.get("cases") or []
    reproducibility = payload.get("reproducibility") or {}
    identity = ((payload.get("model") or {}).get("identity") or {})
    model_sha = (
        metadata.get("modelSha256")
        or identity.get("sha256")
        or "n/a"
    )
    model_name = (
        metadata.get("modelName")
        or (payload.get("model") or {}).get("repository")
        or "n/a"
    )
    seed = metadata.get("seed", reproducibility.get("seed", "n/a"))
    mode = metadata.get("mode", reproducibility.get("mode", "copy"))
    max_tokens = metadata.get(
        "maxGenerationTokens",
        reproducibility.get("maxGenerationTokens", "n/a"),
    )
    tolerance = metadata.get(
        "toleranceTokens",
        reproducibility.get("toleranceTokens", "n/a"),
    )
    highest = aggregate.get("highestAllPassTier")
    lines = [
        "# Context Capacity Benchmark Report",
        "",
        f"- **Model**: {_cell(model_name)}",
        f"- **Model identity SHA**: {_cell(model_sha)}",
        f"- **Mode**: {_cell(mode)}",
        f"- **Seed**: {_cell(seed)}",
        f"- **Max generation tokens**: {_cell(max_tokens)}",
        f"- **Tolerance tokens**: {_cell(tolerance)}",
        "",
        "## Summary",
        "",
        f"- **Total cases**: {_cell(aggregate.get('total', 0))}",
        f"- **Passed cases**: {_cell(aggregate.get('passes', 0))}",
        f"- **Highest all-pass tier**: {_cell(highest)}",
        "",
        "## Token and time totals",
        "",
        f"- **Rendered prompt tokens**: {_cell(aggregate.get('totalRenderedPromptTokens', 0))}",
        f"- **Prompt tokens**: {_cell(aggregate.get('totalPromptTokens', 0))}",
        f"- **Generation tokens**: {_cell(aggregate.get('totalGenerationTokens', 0))}",
        f"- **Generation seconds**: {_cell(aggregate.get('totalGenerationSeconds', 0.0))}",
        f"- **Load seconds**: {_cell(aggregate.get('totalLoadSeconds', 0.0))}",
        "",
        "## Tier-by-position pass rate",
        "",
        "| Tier | Position | Passed | Total | Rate |",
        "| --- | --- | --- | --- | --- |",
    ]
    rates = aggregate.get("passRatesByTierPosition") or {}
    for tier in aggregate.get("requestedTiers") or sorted(rates):
        cells = rates.get(str(tier)) or {}
        positions = aggregate.get("requestedPositions") or sorted(cells)
        for position in positions:
            cell = cells.get(position) or {}
            lines.append(
                "| "
                + " | ".join((
                    _cell(tier),
                    _cell(position),
                    _cell(cell.get("passed", 0)),
                    _cell(cell.get("total", 0)),
                    _cell(cell.get("rate")),
                ))
                + " |"
            )
    lines.extend(["", "## Failure counts", ""])
    failures = aggregate.get("failureCounts") or {}
    if failures:
        for reason, count in sorted(failures.items()):
            lines.append(f"- {_cell(reason)}: {_cell(count)}")
    else:
        lines.append("- No failures recorded.")
    lines.extend([
        "",
        "## Case details",
        "",
        "| Tier | Position | Trial | Rendered tokens | Outcome | Generation seconds |",
        "| --- | --- | --- | --- | --- | --- |",
    ])
    for case in cases:
        lines.append(
            "| "
            + " | ".join((
                _cell(case.get("tier")),
                _cell(case.get("position")),
                _cell(case.get("trial")),
                _cell(case.get("renderedTokens")),
                _cell(case.get("outcome")),
                _cell(case.get("generationSeconds")),
            ))
            + " |"
        )
    return "\n".join(lines).rstrip("\n") + "\n"


def _cell(value: Any) -> str:
    if value is None:
        return "n/a"
    return str(value).replace("|", "\\|")
