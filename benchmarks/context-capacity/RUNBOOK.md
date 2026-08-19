# Context Capacity Benchmark Runbook

## Purpose
This benchmark measures the **effective exact-edit context** of the configured local MLX model, distinct from its nominal context window or tokens-per-second throughput. It determines the maximum prompt size at which the model reliably produces an exact edit manifest when the authoritative edit anchor is placed at specific positions (start, middle, end) within deterministic distractor text.

## Promotion Preflight
Before running the full promotion matrix, ensure the following conditions are met to guarantee reproducibility:
- **AC Power**: The system must be plugged in; battery fluctuations can cause thermal throttling.
- **Zero Swap**: Ensure no swap space is active to prevent I/O-induced latency spikes.
- **No Competing Inference**: No other processes should be loading or running MLX models.
- **Stable Model/Config Identity**: The model directory and configuration must remain unchanged between runs.
- **Consistent Thermal State**: Allow the device to cool to ambient temperature before starting.

## Execution Commands

### Smoke Test
A quick validation of the benchmark infrastructure and basic model responsiveness.
```bash
python -m mlx_swarm.context_benchmark \
  --config .mlx-swarm/swarm.json \
  --tiers 2048,8192 \
  --positions start,end \
  --trials 1 \
  --output-dir ./results/smoke
```

### Promotion Matrix
The full benchmark suite for verifying context limits across all defined tiers and positions.
```bash
python -m mlx_swarm.context_benchmark \
  --config .mlx-swarm/swarm.json \
  --tiers 2048,4096,8192,16384,32768 \
  --positions start,middle,end \
  --trials 3 \
  --output-dir ./results/promotion
```

## Output Interpretation

### Artifacts
Each run generates two files in the specified output directory:
1. **results.json**: Machine-readable data containing per-task metrics, failure taxonomy, and token counts.
2. **report.md**: A human-readable summary including failure rates, the highest all-pass tier, and total token/time/load statistics.

### Failure Taxonomy
Tasks are classified as:
- `pass`: Exact normalized manifest equality achieved.
- `invalid_json`: Model output was not valid JSON.
- `invalid_schema`: JSON did not match the expected edit manifest schema.
- `wrong_edit`: The edit was semantically correct but not an exact character-for-character match.
- `suspected_token_limit`: A non-passing completion ended at or near its generation ceiling.
- `inference_error`: Backend failure (e.g., OOM, crash).
- `token_fit_out_of_tolerance`: The real rendered prompt could not be fitted within --tolerance-tokens.

### Highest All-Pass Tier
The highest tier in the `--tiers` list where **all** trials for **all** positions resulted in `pass` status. If no tier achieves 100% pass rate, this field will be null.

## Constraints & Guarantees
- **No Frontier Models**: This benchmark never invokes a frontier model API.
- **Deterministic Distractors**: Distractor text is generated deterministically using the configured seed.
- **Single Anchor**: Each prompt contains exactly one authoritative old-to-new edit anchor.
- **Exact Scoring**: Scores are based on exact normalized manifest equality, not semantic similarity.
- **Real Tokenization**: Prompt lengths are measured using the real chat-template tokenizer (`_render_prompt`), not character counts.
- **One Backend Instance**: A single `MLXBatchBackend` is opened for the entire run, submitting one task at a time.

## Versioning & Reruns
- Append a version suffix to the output directory (e.g., `./results/v1`) to distinguish runs.
- Reruns should use the same `--seed` to ensure deterministic distractor generation.
- Do not mutate prior `.swarm` sessions; this benchmark is independent of existing state.
