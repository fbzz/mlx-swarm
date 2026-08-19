# Context Capacity Benchmark Architecture

## Overview
The `context_benchmark` module is a standalone, observational tool designed to measure the effective context window of a configured local MLX model for exact edit-manifest success. It operates independently of the main MLX Swarm executor, ensuring that normal workspace prompting, session management, and backend generation behaviors remain completely unmodified.

## Core Components

### 1. Pure Helper Module (`context_benchmark_core`)
This module contains pure logic for:
- **Token Fitting**: Using the real chat-template tokenizer to calculate `renderedPromptTokens` for each benchmark case. It rejects cases where the token count exceeds the configured tolerance relative to the model's nominal context window.
- **Distractor Generation**: Creating deterministic synthetic code distractors based on a fixed seed.
- **Anchor Placement**: Placing exactly one authoritative old-to-new edit anchor at `start`, `middle`, or `end` positions within the generated context.
- **Normalization & Scoring**: Applying `normalize_output` (as defined in the existing output normalization contract) to the model's response and scoring it against the expected edit manifest using exact equality checks.

### 2. Runner Module (`context_benchmark`)
The CLI entry point (`python -m mlx_swarm.context_benchmark`) orchestrates the benchmark run:
- **Configuration**: Accepts arguments for config path, output directory, token tiers (2048, 4096, 8192, 16384, 32768), positions, trials, tolerance, seed, and bounded generation controls.
- **Backend Management**: Opens a single `MLXBatchBackend` instance for the entire run. It submits exactly one benchmark request at a time with `temperature=0` and the configured seed to ensure determinism.
- **Execution**: Iterates through the promotion matrix (tiers x positions x trials), fitting prompts, invoking the backend, and collecting results.
- **Artifacts**: Emits `results.json` (machine-readable data) and `report.md` (human-readable summary) containing failure taxonomy, success rates, highest all-pass tier, token/time/load totals, model identity, and reproducibility parameters.

## Key Design Principles

- **Observational Only**: This benchmark does not modify normal workspace truncation, executor behavior, or backend generation configurations. It is purely for measurement.
- **Real Tokenization**: Uses the actual chat-template tokenizer to determine prompt length, avoiding character-count ambiguities. Cases exceeding the configured tolerance are explicitly classified as `invalid_fit_out_of_tolerance`.
- **One-Case-At-A-Time**: To isolate variables and ensure deterministic results, each benchmark case is submitted individually to the resident backend.
- **Exact Scoring**: Results are scored based on exact normalized manifest equality. Approximate or semantically similar edits are not accepted.
- **Failure Taxonomy**: Results are classified into categories such as `pass`, `invalid_json`, `invalid_schema`, `wrong_edit`, `suspected_token_limit`, or `inference_error`.

## Testing Strategy
Unit tests for this module must:
- Inject token-count and backend seams (mocking `MLXBatchBackend` and tokenizer outputs).
- **Not** import or load MLX model weights.
- **Not** require network access or a frontier adapter.
- Verify that the runner correctly handles token fitting, backend calls, and artifact generation.

## Integration Points
- **Backend**: Uses `MLXBatchBackend` and respects its metrics (e.g., `renderedPromptTokens`, `hitTokenLimit`).
- **Gates**: Uses `OutputGate` for normalization and validation of model outputs.
- **Config**: Uses `load_config` and `TaskDef` from the existing configuration system.

## Promotion Matrix
The benchmark runs a promotion matrix across:
- **Tiers**: 2048, 4096, 8192, 16384, 32768 tokens.
- **Positions**: start, middle, end.
- **Trials**: 3 deterministic trials per tier/position combination.

This structure allows for the identification of the effective context boundary where exact edit reliability degrades, independent of nominal context-window size.
