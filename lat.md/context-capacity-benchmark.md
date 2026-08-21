# Context Capacity Benchmark Architecture

This page describes the local-only exact-edit context benchmark. It measures
whether the configured MLX model can return one exact edit manifest as
rendered prompt size grows.

## Overview

The CLI is a standalone observational runner. It does not change executor,
prompting, or session behavior.

`python -m mlx_swarm.context_benchmark` loads one resident
`MLXBatchBackend`, fits each case with `_render_prompt`, scores with
`normalize_output`, and writes `results.json` plus `report.md`.

## Core Components

Split modules keep prompt construction, fitting, scoring, and running
separate so each concern stays testable without loading weights.

### Pure Modules

These modules have no backend or filesystem side effects.

- **`context_benchmark_types`**: Shared positions, outcomes, `PromptFit`, and `ScoreResult`.
- **`context_benchmark_prompt`**: JSON-only wrapper, one `TARGET_OLD` haystack, deterministic distractors, and two modes: `copy` states the exact manifest to return; `retrieve` names the change and the model must find the anchor and author the manifest.
- **`context_benchmark_fit`**: Binary-search unit counts against real rendered tokens.
- **`context_benchmark_score`**: Exact normalized one-edit manifest equality; in `retrieve` mode a manifest also passes when it applies the way `materialize_edit_manifest` applies edit-manifest-v1 (one non-empty `old` that occurs once in the file and reproduces the expected file).
- **`context_benchmark_aggregate`**: Pass rates and the highest all-pass tier.
- **`context_benchmark_report`**: Markdown summary of a schema-v1 payload.

### Runner Modules

These modules open one backend and emit artifacts.

- **`context_benchmark_case`**: Fit, optional singleton `generate`, and one case record with `wallSeconds` and `peakMemoryGigabytes`.
- **`context_benchmark_runtime`**: Sequential tier × position × trial matrix with an `on_case` observer and `completed_cases` reuse for resumed runs.
- **`context_benchmark`**: CLI for `--config`, `--output-dir`, tiers, positions, trials, tolerance, seed, `--mode`, `--resume`, and `--quiet`; appends one `cases.jsonl` record and one stderr progress line per finished case.

## Key Design Principles

The benchmark is local, exact, and sequential. It never calls a frontier
adapter.

- **Observational only**: no executor or workspace-prompt changes.
- **Real tokenization**: `_render_prompt` counts tokens; out-of-tolerance cases are `token_fit_out_of_tolerance`.
- **One case at a time**: `generate([task], [prompt])` with temperature 0.
- **Exact scoring**: in `copy` mode only the normalized expected manifest passes; in `retrieve` mode the applied file must equal the expected file, so any unique anchor is exact and a near-miss is `wrong_edit`.
- **Failure taxonomy**: `pass`, `invalid_json`, `invalid_schema`, `wrong_edit`, `token_fit_out_of_tolerance`, `suspected_token_limit`, `inference_error`.
- **Checkpointed**: every finished case is appended to `cases.jsonl` before the next one starts, so an interrupted matrix keeps its evidence; `--resume` reuses records whose `caseId` and `seed` match instead of re-running them.
- **Observable**: each case records wall time and peak memory next to generation time, so a slow case can be attributed to host stalls rather than the model.

## Testing Strategy

Unit tests inject tokenizer and backend seams. They must not import `mlx`
or `mlx_lm`, load weights, use the network, or run the real matrix.

## Integration Points

The runner reuses existing load, render, and gate contracts.

- **Backend**: `MLXBatchBackend`, `_render_prompt`, `renderedPromptTokens`, `suspectedTokenLimit`.
- **Gates**: `normalize_output` with a JSON `OutputGate`.
- **Config**: `load_config` and `TaskDef`.

## Promotion Matrix

The published matrix is three trials at start, middle, and end across
2048, 4096, 8192, 16384, and 32768 rendered-token tiers.

The highest all-pass tier is the largest requested tier where every
position and trial passed. See [[src/mlx_swarm/context_benchmark.py]].

## Measured Capacity

Prefill dominates: the model emits the same 44-token manifest at every
tier, so case time grows linearly with rendered tokens.

On an M4 Pro a 32768-token case takes about 90 s (copy) or 50 s
(retrieve) at roughly 22 GB peak, and both modes pass every cell of the
promotion matrix on the calibrated Qwen3.6-35B-A3B-4bit worker.
Each checkpointed record is the evidence; the runbook in
`benchmarks/context-capacity` carries the measured durations and the
full matrix command.
