# Backend

MLX batch backend — loads the model once and decodes all workers in a single batch.

The backend module in [[src/swarm_agents/backend.py]] handles model resolution, persistent loading, chat-template rendering, and grouped batched generation through MLX.

## Model Resolution

`_resolve_model_path` resolves the model path in priority order:
1. **localPath**: If set, use the local directory (must contain config.json).
2. **repository**: Download from HuggingFace (local cache only, no network).
3. If neither is set, raise RuntimeError.

See [[src/swarm_agents/backend.py#_resolve_model_path]].

## Batch Generation

`generate_batch` is the main entry point:
1. Resolve model path from config.
2. Load the model and tokenizer once for the full plan.
3. Merge role defaults with each task's validated generation overrides.
4. Render every request through the tokenizer's native chat template.
   When thinking is disabled, templates that forcibly open a thinking block are closed in the assistant prefix so reasoning cannot consume the artifact budget.
5. Group tasks by compatible sampler settings and preserve per-task token limits.
6. Call `mlx_lm.generate.batch_generate` with configured prefill sizing.
7. Collect group and aggregate statistics.
8. Release the model after the full plan finishes.

See [[src/swarm_agents/backend.py#generate_batch]].

## Generation Config

Each role has default generation parameters (temperature, top_p, max_tokens). Validated overrides apply per task; batches never silently inherit the first task's configuration. The seed is set per compatible group for reproducibility. See [[Plans]].

## Statistics

The backend reports:
- **loadSeconds/modelReused**: Initial model load time and whether the resident model was reused.
- **generationSeconds**: Batch generation time.
- **batchSize**: Number of tasks in the batch.
- **peakMemoryGigabytes**: Peak GPU memory usage.
- **promptTokens/generationTokens**: Aggregate token counts.
- **groups**: Per-sampler task IDs, settings, timing, and token counts.

These are recorded in the [[Session]] batch records.
