"""Rendered-token fitting for context-benchmark prompts."""

from __future__ import annotations

from collections.abc import Callable

from .context_benchmark_types import PromptFit

__all__ = ["fit_prompt"]


def fit_prompt(
    requested_tokens: int,
    tolerance_tokens: int,
    prompt_for_units: Callable[[int], str],
    token_count: Callable[[str], int],
    max_units: int = 131072,
) -> PromptFit:
    if requested_tokens <= 0:
        raise ValueError("requested_tokens must be > 0")
    if tolerance_tokens < 0:
        raise ValueError("tolerance_tokens must be >= 0")
    if max_units <= 0:
        raise ValueError("max_units must be > 0")

    candidates: dict[int, PromptFit] = {}

    def evaluate(unit_count: int) -> PromptFit:
        if unit_count not in candidates:
            prompt = prompt_for_units(unit_count)
            rendered = token_count(prompt)
            candidates[unit_count] = PromptFit(
                prompt=prompt,
                requested_tokens=requested_tokens,
                rendered_tokens=rendered,
                unit_count=unit_count,
                within_tolerance=abs(rendered - requested_tokens)
                <= tolerance_tokens,
            )
        return candidates[unit_count]

    evaluate(0)
    low = 0
    high = 1
    while high <= max_units:
        fit = evaluate(high)
        if fit.rendered_tokens >= requested_tokens:
            break
        if high == max_units:
            break
        low = high
        high = min(high * 2, max_units)

    while low <= high:
        mid = (low + high) // 2
        fit = evaluate(mid)
        if fit.rendered_tokens == requested_tokens:
            break
        if fit.rendered_tokens < requested_tokens:
            low = mid + 1
        else:
            high = mid - 1

    return min(
        candidates.values(),
        key=lambda item: (
            abs(item.rendered_tokens - item.requested_tokens),
            item.unit_count,
        ),
    )
