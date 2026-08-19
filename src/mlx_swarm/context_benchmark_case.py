"""Context benchmark single-case execution logic."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Tuple

from .backend import MLXBatchBackend, SwarmConfig, effective_generation_config
from .contracts import TaskDef, OutputGate
from .context_benchmark_fit import fit_prompt
from .context_benchmark_prompt import build_prompt
from .context_benchmark_score import score_output
from .gates import normalize_output

__all__ = ["benchmark_task", "run_case"]


def benchmark_task(
    case_id: str,
    max_generation_tokens: int,
    seed: int,
) -> TaskDef:
    """Construct a TaskDef for a single benchmark case.

    Args:
        case_id: Unique identifier for the case.
        max_generation_tokens: Maximum tokens for generation.
        seed: Random seed for reproducibility.

    Returns:
        A TaskDef configured for strict JSON scoring.
    """
    return TaskDef(
        id=case_id,
        role="editor",
        prompt="",
        gate=OutputGate(
            max_characters=20_000,
            output_format="json",
            strip_single_code_fence=True,
            python_syntax=False,
            json_required_keys=("edits",),
            json_allowed_keys=("edits",),
        ),
        generation_override={
            "temperature": 0.0,
            "top_p": 1.0,
            "max_tokens": max_generation_tokens,
            "seed": seed,
        },
        output_protocol="edit-manifest-v1",
        artifact_type="report",
        worker_output_protocol="edit-manifest-v1",
    )


def run_case(
    backend: MLXBatchBackend,
    config: SwarmConfig,
    tier: str,
    position: str,
    trial: int,
    seed: int,
    tolerance_tokens: int,
    max_generation_tokens: int,
) -> Dict[str, Any]:
    """Run a single benchmark case.

    Args:
        backend: The MLXBatchBackend instance.
        config: The SwarmConfig instance.
        tier: The context capacity tier.
        position: The position of the target ('start', 'middle', 'end').
        trial: The trial number.
        seed: The seed for deterministic generation.
        tolerance_tokens: The tolerance for token fitting.
        max_generation_tokens: The maximum generation tokens.

    Returns:
        A JSON-safe dictionary with the case results.
    """
    case_id = f"{tier}_{position}_{trial}_{seed}"
    task = benchmark_task(case_id, max_generation_tokens, seed)
    gen_cfg = effective_generation_config(task, config)

    def prompt_for_units(unit_count: int) -> str:
        return build_prompt(unit_count, position, trial, seed)

    def token_count_fn(prompt: str) -> int:
        return len(
            backend._render_prompt(
                backend.tokenizer, prompt, gen_cfg
            )
        )

    fit = fit_prompt(
        requested_tokens=gen_cfg.get("max_tokens", 2048),
        tolerance_tokens=tolerance_tokens,
        prompt_for_units=prompt_for_units,
        token_count=token_count_fn,
    )

    if not fit.within_tolerance:
        return {
            "case_id": case_id,
            "tier": tier,
            "position": position,
            "trial": trial,
            "seed": seed,
            "requested_tokens": fit.requested_tokens,
            "rendered_tokens": fit.rendered_tokens,
            "unit_count": fit.unit_count,
            "within_tolerance": False,
            "outcome": "token_fit_out_of_tolerance",
            "score": None,
            "stats": {},
        }

    task.prompt = fit.prompt
    try:
        outputs, stats = backend.generate([task], [fit.prompt])
        raw_output = outputs[0]
        group_stats = stats.get("groups", [{}])[0]
        suspected_token_limit = group_stats.get("suspectedTokenLimit", False)
        score_result = score_output(raw_output, suspected_token_limit)
        return {
            "case_id": case_id,
            "tier": tier,
            "position": position,
            "trial": trial,
            "seed": seed,
            "requested_tokens": fit.requested_tokens,
            "rendered_tokens": fit.rendered_tokens,
            "unit_count": fit.unit_count,
            "within_tolerance": True,
            "outcome": score_result.outcome,
            "score": score_result.dict(),
            "stats": {
                "loadSeconds": stats.get("loadSeconds", 0.0),
                "generationSeconds": stats.get("generationSeconds", 0.0),
                "promptTokens": stats.get("promptTokens", 0),
                "renderedPromptTokens": stats.get("renderedPromptTokens", 0),
                "generationTokens": stats.get("generationTokens", 0),
                "suspectedTokenLimit": suspected_token_limit,
            },
            "output_hash": hashlib.sha256(raw_output.encode()).hexdigest(),
            "output_preview": raw_output[:240],
        }
    except Exception as e:
        return {
            "case_id": case_id,
            "tier": tier,
            "position": position,
            "trial": trial,
            "seed": seed,
            "requested_tokens": fit.requested_tokens,
            "rendered_tokens": fit.rendered_tokens,
            "unit_count": fit.unit_count,
            "within_tolerance": True,
            "outcome": "inference_error",
            "score": None,
            "stats": {
                "error": str(e)[:240],
                "loadSeconds": 0.0,
                "generationSeconds": 0.0,
            },
            "output_hash": None,
            "output_preview": None,
        }
