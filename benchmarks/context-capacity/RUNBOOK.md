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

## Modes
- **copy** (default): the prompt states the exact manifest and the model must return it unchanged while the haystack grows. This measures output-format reliability under long context.
- **retrieve**: the prompt only names the change ("in the function named target, change the returned string from before to after") and the model must locate the anchor in the file and author the manifest. Scoring applies the edit the way the runtime applies edit-manifest-v1: one non-empty `old` that occurs exactly once and reproduces the expected file passes, whatever anchor size the model chose; a non-unique anchor, wrong function, wrong path, or no-op is `wrong_edit`. Pass `--mode retrieve`; checkpoints of one mode are never reused by the other.

### Decoys
`--decoys N` inserts N functions that also `return "before"`, spread evenly through the distractors. The target function stays the only occurrence of the full `def target()` text, so copy mode is unchanged, but in retrieve mode the bare literal is no longer a unique anchor: a manifest whose `old` is just `"before"` is `wrong_edit` (`found N+1`) and the worker must pin the edit to the target function. Default 0; checkpoints with a different decoy count are never reused.

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
Add `--resume` to the same command to finish a matrix that was interrupted. Add `--mode retrieve` (and a separate `--output-dir`) for the retrieval variant, and `--decoys 3` to make the anchor genuinely ambiguous.

### Expected Duration
Generation is a fixed 44-token manifest, so case time is prefill time and grows with the tier. Measured 2026-08-21 on an M4 Pro (48 GiB) with `mlx-community/Qwen3.6-35B-A3B-4bit` over the full 45-case promotion matrix (3 trials × 3 positions per tier), all 45 cases passing:

| Tier | Seconds per case (min / median / max) | Peak memory |
| --- | --- | --- |
| 2048 | 5.5 / 5.8 / 9.9 | 20.7 GB |
| 4096 | 8.5 / 9.0 / 9.8 | 20.8 GB |
| 8192 | 16.5 / 20.1 / 22.6 | 21.0 GB |
| 16384 | 38.0 / 39.1 / 42.5 | 21.4 GB |
| 32768 | 51.6 / 91.3 / 100.5 | 22.3 GB |
| 65536 (exploratory, 1 trial; retrieve mode 129.0 / 132.3 / 135.4, also 3/3) | 127.7 / 133.5 / 147.0 | 24.1 GB |

Retrieve mode (`--mode retrieve`, same matrix, 2026-08-21) also passed 45/45 with highest all-pass tier 32768, in 14.4 minutes; per-case medians were 3.2 / 5.8 / 11.0 / 23.2 / 51.3 s for 2048–32768 (shorter prompt than copy mode), and every case returned the same anchor without the trailing newline, which the applied-edit scoring accepts. The 65536 tier is outside the promotion matrix; running it requires `batch.maxBatchPromptTokens` at or above the tier (the backend refuses a single prompt above that budget), so the local config was raised to 131072 for that run. The whole 2048–32768 matrix took 23.5 minutes of wall time plus one model load (~7–14 s); an earlier single-trial 16384 run saw one 163 s host stall, so budget 25–45 minutes. Do not interrupt a 32768 case because it looks stalled: it spends about 90 seconds prefilling before the first output token.

## Output Interpretation

### Artifacts
Each run generates three files in the specified output directory:
1. **cases.jsonl**: One JSON record per finished case, appended the moment the case ends. This is the checkpoint: if the run is interrupted, the finished cases are still here.
2. **results.json**: Machine-readable data containing per-task metrics, failure taxonomy, and token counts, written when the matrix completes.
3. **report.md**: A human-readable summary including failure rates, the highest all-pass tier, and total token/time/load statistics.

Every case record also carries `wallSeconds` (fit plus generation) and `peakMemoryGigabytes`, so a slow case can be told apart from a slow model: a wall/generation gap or a memory jump points at the host (thermal throttling, paging, competing work), not the prompt.

### Progress and Resume
The CLI prints one line per finished case to stderr (`[n/total] caseId outcome rendered=… gen=…s wall=…s peak=…GB`); pass `--quiet` to silence it. To continue an interrupted run, re-issue the same command with `--resume`: cases already present in `cases.jsonl` with the same `caseId` and seed are reused and only the missing cells run. Use the same `--tolerance-tokens`, `--max-generation-tokens`, and `--seed` as the original run; changing them invalidates the reuse.

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

## Recording the Result
The measurement is planner-facing evidence, not the worker calibration: `worker.capabilities.calibration` is reserved for the immutable BugsInPy replay digest and must stay `unmeasured` until that replay runs on this machine. Record the context result as one bounded statement in `worker.capabilities.strengths` of the swarm config — highest all-pass tier per mode, trials, the `results.json` SHA-256, and the per-case prefill cost — so the frontier planner sees how much rendered context the worker can be trusted with. Keep `batch.maxPromptCharacters` and `batch.maxBatchPromptTokens` at or below what the matrix demonstrated.

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
