# Backend

MLX batch backend — loads the model once and performs true batched decoding for
compatible workers.

The backend module in [[src/mlx_swarm/backend.py]] handles model resolution, persistent loading, chat-template rendering, and grouped batched generation through MLX.

## Model Resolution

`_resolve_model_path` resolves the model path in priority order:
1. **localPath**: If set, use the local directory (must contain config.json).
2. **repository**: Download from HuggingFace (local cache only, no network).
3. If neither is set, raise RuntimeError.

See [[src/mlx_swarm/backend.py#_resolve_model_path]].

## Batch Generation

`generate_batch` is the main entry point:
1. Resolve model path from config.
2. Load the model and tokenizer once for the full plan.
3. Merge role defaults with each task's validated generation overrides.
4. Render every request through the tokenizer's native chat template.
   When thinking is disabled, templates that forcibly open a thinking block are closed in the assistant prefix so reasoning cannot consume the artifact budget.
5. For strict JSON and edit-manifest tasks, default to deterministic sampling
   and a 1024-token completion budget unless the plan explicitly overrides
   them. This improves small-model structure reliability and avoids needless
   sampler fragmentation.
6. Reject a request when its rendered prompt tokens plus requested generation
   tokens exceed the worker profile's declared context window.
7. Group tasks by compatible sampler settings, then split physical batches so
   their aggregate rendered input stays below `maxBatchPromptTokens`. A single
   prompt above the ceiling is rejected. Completion limits remain per task.
8. Seed once per sampler group so a prompt-budget split does not restart the
   same stochastic stream.
9. Call `mlx_lm.generate.batch_generate` with configured prefill sizing.
10. Collect group and aggregate statistics, including successful calls and
    known tokens when a later physical call fails.
11. Release the model after the full plan finishes.

See [[src/mlx_swarm/backend.py#generate_batch]].

## Generation Config

Each role has default generation parameters (temperature, top_p, max_tokens). Validated overrides apply per task; batches never silently inherit the first task's configuration. The seed is set per compatible group for reproducibility. See [[Plans]].

The measured default is two agents with a 1024-token prefill step and a
49,152-token aggregate prompt ceiling. The contract still accepts one through
thirty-two agents. Raising the configured agent count changes chunk width, not
the number of simultaneously resident model copies.

## Statistics

The backend reports:
- **loadSeconds/modelReused**: Initial model load time and whether the resident model was reused.
- **generationSeconds**: Batch generation time.
- **batchSize**: Number of tasks in the batch.
- **peakMemoryGigabytes**: Peak GPU memory usage.
- **promptTokens/generationTokens**: Aggregate token counts.
- **renderedPromptTokens**: Tokenized chat-template length used by the context
  guard.
- **generationCalls/samplerGroupCount**: Physical local generation calls made
  for one logical chunk.
- **completedGenerationCalls/failedGenerationCalls**: Calls that returned
  evidence versus calls that raised after invocation.
- **plannedPhysicalBatchCount/physicalBatchCount**: Planned prompt-bounded
  chunks versus calls actually attempted.
- **maxTrueBatchWidth/samplerFragmented**: Largest compatible MLX batch and
  whether differing sampler settings split the chunk.
- **batchSplitByPromptBudget/maxBatchPromptTokens**: Whether aggregate rendered
  input forced a split and the active hard prompt ceiling.
- **groups**: Per-call task IDs, settings, timing, token counts, task output
  counts, token-limit indicators, completion state, and any error.

Truncation is reported through two per-task indicators. `hitTokenLimit` is
the exact re-encoded-count comparison against the ceiling.
`suspectedTokenLimit` additionally flags counts within a 16-token margin,
because re-encoding decoded text does not reliably reproduce the generated
token count; the [[Executor]] applies the suspicion only to gate-failing
output, so a complete gate-passing artifact that merely lands near its
ceiling is unaffected. mlx_lm computes a real per-sequence finish reason
internally but its public `batch_generate` discards it; the margin avoids
depending on that unstable pre-1.0 internal API. See
[[src/mlx_swarm/backend.py#suspected_token_limit]].

These are recorded in the [[Session]] batch records.
