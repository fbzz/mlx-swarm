"""Single-case execution for the local context benchmark."""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from typing import Any

from .backend import (
    BatchGenerationError,
    _render_prompt,
    effective_generation_config,
)
from .context_benchmark_fit import fit_prompt
from .context_benchmark_prompt import MODES, build_prompt, source_body
from .context_benchmark_score import score_output
from .contracts import OutputGate, SwarmConfig, TaskDef

__all__ = ["benchmark_task", "run_case"]


def benchmark_task(
    case_id: str,
    max_generation_tokens: int,
    seed: int,
) -> TaskDef:
    return TaskDef(
        id=case_id,
        role="implementation",
        prompt="",
        gate=OutputGate(
            output_format="json",
            strip_single_code_fence=True,
            json_required_keys=("edits",),
            json_allowed_keys=("edits",),
        ),
        generation_override={
            "temperature": 0.0,
            "top_p": 1.0,
            "max_tokens": max_generation_tokens,
            "seed": seed,
        },
        worker_output_protocol="edit-manifest-v1",
    )


def run_case(
    backend: Any,
    config: SwarmConfig,
    tier: int,
    position: str,
    trial: int,
    seed: int,
    tolerance_tokens: int,
    max_generation_tokens: int,
    mode: str = "copy",
    decoys: int = 0,
) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if decoys < 0:
        raise ValueError("decoys must be non-negative.")
    started = time.perf_counter()
    case_id = f"{tier}-{position}-{trial}"
    task = benchmark_task(case_id, max_generation_tokens, seed)
    generation_config = effective_generation_config(task, config)

    def prompt_for_units(unit_count: int) -> str:
        return build_prompt(unit_count, position, trial, seed, mode, decoys)

    def token_count(prompt: str) -> int:
        return len(
            _render_prompt(backend.tokenizer, prompt, generation_config)
        )

    fit = fit_prompt(
        requested_tokens=int(tier),
        tolerance_tokens=tolerance_tokens,
        prompt_for_units=prompt_for_units,
        token_count=token_count,
    )
    record: dict[str, Any] = {
        "caseId": case_id,
        "mode": mode,
        "decoys": decoys,
        "tier": int(tier),
        "position": position,
        "trial": trial,
        "seed": seed,
        "requestedTokens": fit.requested_tokens,
        "renderedTokens": fit.rendered_tokens,
        "unitCount": fit.unit_count,
        "withinTolerance": fit.within_tolerance,
        "outcome": "token_fit_out_of_tolerance",
        "score": None,
        "promptTokens": 0,
        "generationTokens": 0,
        "generationSeconds": 0.0,
        "loadSeconds": 0.0,
        "peakMemoryGigabytes": 0.0,
        # Fit plus generation wall time; fit cost is wallSeconds minus
        # generationSeconds, and a wall/generation gap on one case flags
        # host-side stalls (thermal throttling, paging) for that case.
        "wallSeconds": 0.0,
        "outputSha256": None,
        "outputPreview": None,
    }
    if not fit.within_tolerance:
        return _finish(record, started)

    task = replace(task, prompt=fit.prompt)
    try:
        outputs, stats = backend.generate([task], [fit.prompt])
    except BatchGenerationError as exc:
        return _finish(
            _inference_record(record, stats=exc.statistics, error=str(exc)),
            started,
        )
    except Exception as exc:
        return _finish(
            _inference_record(record, stats={}, error=str(exc)),
            started,
        )

    groups = stats.get("groups") or [{}]
    group = groups[0] if groups else {}
    suspected = bool(group.get("suspectedTokenLimit"))
    raw_output = outputs[0] if outputs else ""
    source = (
        source_body(fit.unit_count, position, trial, seed, decoys)
        if mode == "retrieve"
        else None
    )
    scored = score_output(raw_output, suspected, source=source)
    record.update({
        "outcome": scored.outcome,
        "score": scored.to_dict(),
        "promptTokens": int(stats.get("promptTokens") or 0),
        "generationTokens": int(stats.get("generationTokens") or 0),
        "generationSeconds": float(stats.get("generationSeconds") or 0.0),
        "loadSeconds": float(stats.get("loadSeconds") or 0.0),
        "peakMemoryGigabytes": float(
            stats.get("peakMemoryGigabytes") or 0.0
        ),
        "renderedTokens": int(
            stats.get("renderedPromptTokens") or fit.rendered_tokens
        ),
        "outputSha256": hashlib.sha256(raw_output.encode("utf-8")).hexdigest(),
        "outputPreview": raw_output[:240],
    })
    return _finish(record, started)


def _finish(record: dict[str, Any], started: float) -> dict[str, Any]:
    record["wallSeconds"] = time.perf_counter() - started
    return record


def _inference_record(
    record: dict[str, Any],
    *,
    stats: dict[str, Any],
    error: str,
) -> dict[str, Any]:
    record.update({
        "outcome": "inference_error",
        "promptTokens": int(stats.get("promptTokens") or 0),
        "generationTokens": int(stats.get("generationTokens") or 0),
        "generationSeconds": float(stats.get("generationSeconds") or 0.0),
        "loadSeconds": float(stats.get("loadSeconds") or 0.0),
        "peakMemoryGigabytes": float(
            stats.get("peakMemoryGigabytes") or 0.0
        ),
        "outputPreview": error[:240],
    })
    return record
