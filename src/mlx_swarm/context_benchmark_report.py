"""Context benchmark reporting module.

Provides deterministic Markdown rendering of benchmark payloads
without file I/O or external dependencies.
"""

from typing import Any, Mapping


def render_report(payload: Mapping[str, Any]) -> str:
    """Render a benchmark payload to a concise Markdown report.

    Args:
        payload: Dictionary containing metadata, aggregate stats,
                 and a list of case records.

    Returns:
        A Markdown string with summary, tables, and failure details.
    """
    metadata = payload.get("metadata", {})
    aggregate = payload.get("aggregate", {})
    cases = payload.get("cases", [])

    model_sha = metadata.get("model_sha", "unknown")
    model_name = metadata.get("model_name", "unknown")
    seed = metadata.get("seed", 0)
    max_tokens = metadata.get("max_tokens", 0)
    tolerance = metadata.get("tolerance", 0)
    load_seconds = aggregate.get("loadSeconds", 0.0)
    generation_seconds = aggregate.get("generationSeconds", 0.0)
    total_tokens = aggregate.get("totalTokens", 0)
    prompt_tokens = aggregate.get("promptTokens", 0)
    rendered_tokens = aggregate.get("renderedTokens", 0)
    total_cases = aggregate.get("totalCases", 0)
    passed_cases = aggregate.get("passedCases", 0)
    highest_all_pass = aggregate.get("highestAllPass", "n/a")

    # Escape helper for table cells
    def esc(val: Any) -> str:
        s = str(val)
        return s.replace("|", "\\|")

    lines = []
    lines.append("# Context Capacity Benchmark Report")
    lines.append("")
    lines.append(f"- **Model**: {model_name} ({model_sha})")
    lines.append(f"- **Seed**: {seed}")
    lines.append(f"- **Max Tokens**: {max_tokens}")
    lines.append(f"- **Tolerance**: {tolerance}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **Total Cases**: {total_cases}")
    lines.append(f"- **Passed Cases**: {passed_cases}")
    lines.append(f"- **Highest All-Pass Tier**: {highest_all_pass}")
    lines.append("")
    lines.append("## Token and Time Statistics")
    lines.append("")
    lines.append(f"- **Load Seconds**: {load_seconds:.2f}")
    lines.append(f"- **Generation Seconds**: {generation_seconds:.2f}")
    lines.append(f"- **Total Tokens**: {total_tokens}")
    lines.append(f"- **Prompt Tokens**: {prompt_tokens}")
    lines.append(f"- **Rendered Tokens**: {rendered_tokens}")
    lines.append("")

    # Tier-by-position pass rate
    lines.append("## Tier-by-Position Pass Rate")
    lines.append("")
    lines.append("| Tier | Position | Pass Rate |")
    lines.append("|------|----------|-----------|")
    tier_stats = aggregate.get("tierStats", {})
    for tier, pos_stats in sorted(tier_stats.items()):
        for pos, stats in sorted(pos_stats.items()):
            rate = stats.get("passRate", "n/a")
            lines.append(f"| {esc(tier)} | {esc(pos)} | {esc(rate)} |")
    lines.append("")

    # Failure counts
    lines.append("## Failure Counts")
    lines.append("")
    failure_counts = aggregate.get("failureCounts", {})
    if failure_counts:
        for reason, count in sorted(failure_counts.items()):
            lines.append(f"- {reason}: {count}")
    else:
        lines.append("- No failures recorded.")
    lines.append("")

    # Per-case table
    lines.append("## Case Details")
    lines.append("")
    lines.append(
        "| Tier | Position | Trial | Rendered Tokens | Outcome | Gen Seconds |"
    )
    lines.append(
        "|------|----------|-------|-----------------|---------|-------------|"
    )
    for case in cases:
        tier = case.get("tier", "n/a")
        pos = case.get("position", "n/a")
        trial = case.get("trial", "n/a")
        rendered = case.get("renderedTokens", "n/a")
        outcome = case.get("outcome", "n/a")
        gen_sec = case.get("generationSeconds", "n/a")
        lines.append(
            f"| {esc(tier)} | {esc(pos)} | {esc(trial)} | {esc(rendered)} | {esc(outcome)} | {esc(gen_sec)} |"
        )
    lines.append("")

    return "\n".join(lines)
