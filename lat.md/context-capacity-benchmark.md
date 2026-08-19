# Context Capacity Benchmark Architecture

## Overview
The `context_benchmark` module is a standalone, observational tool designed to measure the effective context window of a configured local MLX model for exact edit-manifest success. It operates independently of the main MLX Swarm executor, ensuring that normal workspace prompting, session management, and backend generation behaviors remain completely unmodified.

## Core Components

### 2. Pure Modules
- **`context_benchmark_types`**: Defines the data structures for benchmark cases, tiers, positions, and results.
- **`context_benchmark_prompt`**: Constructs the raw prompt string for a given case, integrating context, distractors, and anchors.
- **`context_benchmark_fit`**: Uses the real chat-template tokenizer to calculate `renderedPromptTokens`. It rejects cases where the token count exceeds the requested tier plus `--tolerance-tokens`.
- **`context_benchmark_score`**: Applies `normalize_output` to the model's response and scores it against the expected edit manifest using exact equality checks.
- **`context_benchmark_aggregate`**: Collects results, calculates success rates, and identifies the highest all-pass tier.
- **`context_benchmark_report`**: Generates the human-readable `report.md` summary.

### 3. Runner Modules
- **`context_benchmark_case`**: Handles the lifecycle of a single benchmark case: prompt construction, token fitting, backend invocation, and result recording.
- **`context_benchmark_runtime`**: Orchestrates the promotion matrix (tiers x positions x trials). It opens a single `MLXBatchBackend` instance, iterates through cases sequentially with `temperature=0`, and collects results.
- **`context_benchmark` (CLI)**: The entry point (`python -m mlx_swarm.context_benchmark`) that parses arguments (config, output-dir, tiers, positions, trials, tolerance, seed) and initiates the runtime.
- **Artifacts**: Emits `results.json` (machine-readable data) and `report.md` (human-readable summary) containing failure taxonomy, success rates, highest all-pass tier, token/time/load totals, model identity, and reproducibility parameters.

## Key Design Principles

- **Observational Only**: This benchmark does not modify normal workspace truncation, executor behavior, or backend generation configurations. It is purely for measurement.
- **Real Tokenization**: Uses the actual chat-template tokenizer to determine prompt length, avoiding character-count ambiguities. Cases exceeding the configured tolerance are explicitly classified as `invalid_fit_out_of_tolerance`.
- **One-Case-At-A-Time**: To isolate variables and ensure deterministic results, each benchmark case is submitted individually to the resident backend.
- **Exact Scoring**: Results are scored based on exact normalized manifest equality. Approximate or semantically similar edits are not accepted.
- **Failure Taxonomy**: Results are classified into categories: `pass`, `invalid_json`, `invalid_schema`, `wrong_edit`, `token_fit_out_of_tolerance`, `suspected_token_limit`, or `inference_error`.

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
