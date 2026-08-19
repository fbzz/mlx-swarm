"""Context benchmark runtime orchestration."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .backend import MLXBatchBackend, SwarmConfig
from .contracts import SwarmConfig as SwarmConfigType
from .context_benchmark_case import run_case
from .context_benchmark_aggregate import aggregate_records
from .model_identity import model_directory_identity

__all__ = ["run_benchmark"]


def run_benchmark(
    config_path: Path,
    tiers: Sequence[int],
    positions: Sequence[str],
    trials: int,
    tolerance_tokens: int,
    max_generation_tokens: int,
    seed: int | None = None,
    backend_factory: Callable[[SwarmConfig], Any] = MLXBatchBackend,
) -> dict[str, Any]:
    """Run the full context capacity benchmark matrix.

    Args:
        config_path: Path to the swarm configuration JSON file.
        tiers: Sequence of context capacity tiers to test.
        positions: Sequence of prompt positions (start, middle, end).
        trials: Number of trials per position.
        tolerance_tokens: Token tolerance for fitting.
        max_generation_tokens: Maximum tokens for generation.
        seed: Random seed for reproducibility.
        backend_factory: Factory to create the backend instance.

    Returns:
        A schema-v1 benchmark payload.
    """
    config: SwarmConfig = json.loads(config_path.read_text())
    if not tiers or not positions or trials <= 0:
        raise ValueError("Tiers, positions, and trials must be valid.")
    if tolerance_tokens < 0 or max_generation_tokens <= 0:
        raise ValueError("Tolerance and max generation must be positive.")

    effective_seed = seed if seed is not None else config.seed
    effective_max_gen = min(
        max_generation_tokens,
        config.worker.capabilities.max_generation_tokens,
    )

    backend = backend_factory(config)
    records: list[dict[str, Any]] = []
    try:
        backend.open()
        for tier in tiers:
            for position in positions:
                for trial in range(1, trials + 1):
                    record = run_case(
                        backend=backend,
                        config=config,
                        tier=tier,
                        position=position,
                        trial=trial,
                        seed=effective_seed,
                        tolerance_tokens=tolerance_tokens,
                        max_generation_tokens=effective_max_gen,
                    )
                    records.append(record)

        identity = model_directory_identity(backend.model_path)
        aggregated = aggregate_records(
            records=records,
            tiers=tiers,
            positions=positions,
            trials=trials,
        )

        return {
            "schemaVersion": 1,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "model": {
                "repository": str(backend.model_path),
                "identity": identity,
            },
            "reproducibility": {
                "seed": effective_seed,
                "toleranceTokens": tolerance_tokens,
                "maxGenerationTokens": effective_max_gen,
            },
            "results": aggregated,
        }
    finally:
        backend.close()
