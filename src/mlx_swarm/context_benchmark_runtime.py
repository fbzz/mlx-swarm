"""Resident-backend matrix runner for the local context benchmark."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backend import MLXBatchBackend
from .context_benchmark_aggregate import aggregate_records
from .context_benchmark_case import run_case
from .context_benchmark_prompt import MODES
from .contracts import SwarmConfig, load_config
from .model_identity import model_directory_identity

__all__ = ["run_benchmark"]

CaseObserver = Callable[[dict[str, Any], int, int], None]


def run_benchmark(
    config_path: Path,
    tiers: Sequence[int],
    positions: Sequence[str],
    trials: int,
    tolerance_tokens: int,
    max_generation_tokens: int,
    seed: int | None = None,
    backend_factory: Callable[[SwarmConfig], Any] = MLXBatchBackend,
    on_case: CaseObserver | None = None,
    completed_cases: Sequence[Mapping[str, Any]] = (),
    mode: str = "copy",
) -> dict[str, Any]:
    """Run the tier × position × trial matrix on one resident backend.

    ``on_case`` is called after every freshly executed case with the record,
    the number of matrix cells finished so far (resumed cells included), and
    the matrix size. ``completed_cases`` are records from an earlier run of
    the same matrix; a cell whose ``caseId``, ``seed``, and ``mode`` match is
    reused instead of re-run, which lets an interrupted run resume.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if not tiers or not positions:
        raise ValueError("tiers and positions must be nonempty.")
    if trials <= 0:
        raise ValueError("trials must be positive.")
    if max_generation_tokens <= 0:
        raise ValueError("max_generation_tokens must be positive.")
    if tolerance_tokens < 0:
        raise ValueError("tolerance_tokens must be nonnegative.")

    config = load_config(config_path)
    effective_seed = config.seed if seed is None else seed
    effective_max_generation = min(
        max_generation_tokens,
        config.worker.capabilities.max_generation_tokens,
    )
    reusable = {
        str(case.get("caseId")): dict(case)
        for case in completed_cases
        if case.get("seed") == effective_seed
        and case.get("caseId")
        and (case.get("mode") or "copy") == mode
    }
    total = len(tiers) * len(positions) * trials
    backend = backend_factory(config)
    records: list[dict[str, Any]] = []
    identity: dict[str, Any] = {}
    resumed = 0
    try:
        backend.open()
        for tier in tiers:
            for position in positions:
                for trial in range(1, trials + 1):
                    case_id = f"{int(tier)}-{position}-{trial}"
                    if case_id in reusable:
                        records.append(reusable[case_id])
                        resumed += 1
                        continue
                    record = run_case(
                        backend=backend,
                        config=config,
                        tier=int(tier),
                        position=position,
                        trial=trial,
                        seed=effective_seed,
                        tolerance_tokens=tolerance_tokens,
                        max_generation_tokens=effective_max_generation,
                        mode=mode,
                    )
                    records.append(record)
                    if on_case is not None:
                        on_case(record, len(records), total)
        identity = model_directory_identity(backend.model_path)
    finally:
        backend.close()

    aggregate = aggregate_records(records, tiers, positions, trials)
    return {
        "schemaVersion": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "modelName": config.model.repository,
            "modelSha256": identity.get("sha256"),
            "mode": mode,
            "seed": effective_seed,
            "toleranceTokens": tolerance_tokens,
            "maxGenerationTokens": effective_max_generation,
            "resumedCases": resumed,
        },
        "model": {
            "repository": config.model.repository,
            "localPath": str(getattr(backend, "model_path", "")),
            "identity": identity,
        },
        "reproducibility": {
            "mode": mode,
            "seed": effective_seed,
            "toleranceTokens": tolerance_tokens,
            "maxGenerationTokens": effective_max_generation,
        },
        "aggregate": aggregate,
        "cases": records,
    }
