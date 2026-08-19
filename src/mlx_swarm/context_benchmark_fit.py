"""Context benchmark prompt fitting logic."""

from __future__ import annotations

from typing import Callable

from .context_benchmark_types import PromptFit

__all__ = ["fit_prompt"]


def fit_prompt(
    requested_tokens: int,
    tolerance_tokens: int,
    prompt_for_units: Callable[[int], str],
    token_count: Callable[[str], int],
    max_units: int = 131072,
) -> PromptFit:
    """Find the unit count whose rendered prompt is closest to requested_tokens.

    Validates inputs, grows an upper bound exponentially, then binary-searches
    between the last bounds.  Tracks every evaluated candidate and returns the
    candidate with the smallest absolute token error, breaking ties by smaller
    unit_count.  Sets within_tolerance from that exact rendered-token error.
    """
    if requested_tokens <= 0:
        raise ValueError("requested_tokens must be > 0")
    if tolerance_tokens < 0:
        raise ValueError("tolerance_tokens must be >= 0")
    if max_units <= 0:
        raise ValueError("max_units must be > 0")

    # Evaluate unit count zero
    zero_prompt = prompt_for_units(0)
    zero_tokens = token_count(zero_prompt)
    candidates: list[PromptFit] = [
        PromptFit(
            prompt=zero_prompt,
            requested_tokens=requested_tokens,
            rendered_tokens=zero_tokens,
            unit_count=0,
            within_tolerance=abs(zero_tokens - requested_tokens) <= tolerance_tokens,
        )
    ]

    # Grow upper bound exponentially
    low = 0
    high = 1
    while high <= max_units:
        high_prompt = prompt_for_units(high)
        high_tokens = token_count(high_prompt)
        candidates.append(
            PromptFit(
                prompt=high_prompt,
                requested_tokens=requested_tokens,
                rendered_tokens=high_tokens,
                unit_count=high,
                within_tolerance=abs(high_tokens - requested_tokens) <= tolerance_tokens,
            )
        )
        if high_tokens >= requested_tokens:
            break
        low = high
        high = min(high * 2, max_units)

    # Binary search between last bounds
    while low < high:
        mid = (low + high) // 2
        mid_prompt = prompt_for_units(mid)
        mid_tokens = token_count(mid_prompt)
        candidates.append(
            PromptFit(
                prompt=mid_prompt,
                requested_tokens=requested_tokens,
                rendered_tokens=mid_tokens,
                unit_count=mid,
                within_tolerance=abs(mid_tokens - requested_tokens) <= tolerance_tokens,
            )
        )
        if mid_tokens == requested_tokens:
            break
        if mid_tokens < requested_tokens:
            low = mid + 1
        else:
            high = mid - 1

    # Return candidate with smallest absolute token error, breaking ties by smaller unit_count
    best = min(
        candidates,
        key=lambda c: (abs(c.rendered_tokens - c.requested_tokens), c.unit_count),
    )
    return best
