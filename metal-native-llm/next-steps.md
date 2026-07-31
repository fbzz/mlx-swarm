# Metal-Native LLM Project — Next Steps

## Project Goal

Train a ~20B MoE LLM on NVIDIA GPUs (H100), with architecture co-designed
for MLX/Metal inference on Apple Silicon (M4 Pro 48GB target).

## Skills Built

1. metal-architecture-expert — Metal/Apple Silicon hardware knowledge,
   MSL kernel patterns, inference-first design principles.
2. llm-architecture-research — Paper analysis workflow, MoE comparison
   (Kimi K3, DeepSeek V4, Qwen3.6, GPT-OSS, Mixtral, etc.), attention
   mechanism analysis (KDA, CSA, HCA, GQA, MLA), architecture spec template.

## Papers Analyzed

- Kimi K3 (arXiv:2607.24653) — 2.8T/104B, KDA + Gated MLA hybrid,
  Stable LatentMoE (896 experts/16 active), SiTU-GLU, AttnRes
- DeepSeek V4 (arXiv:2606.19348) — 1.6T Pro / 284B Flash, CSA + HCA
  hybrid attention, mHC residuals, FP4 expert weights, Muon optimizer
- Qwen3.6-35B-A3B — our measured baseline (85 tok/s decode Q4 on M4 Pro)
- GPT-OSS-20b — 20.9B/3.6B, MXFP4-native, closest shipped analog
- DeepSeek V3, Kimi K2, Mixtral 8x7B — lineage/context

## Key Findings

### Best attention for Metal: KDA + CSA hybrid
- KDA (Kimi Delta Attention): fixed-size recurrent state, ZERO KV cache
  for 75% of layers. O(n) compute. Best attention for Metal.
- CSA (Compressed Sparse Attention): 4x KV compression + top-k sparse
  selection. ~2% of GQA8 KV cache. Reduces both memory AND compute.
- Both need custom MLX kernels — not standard ops today.

### Architecture direction for 20B MoE
- 20B total, 2-3B active, 24-28 layers, 4096 dim
- LatentMoE (narrow routed experts, 64-128 experts, top-4 or top-8)
- SiTU-GLU activation (quantization-safe, bounded)
- 32-64K vocab (not 128K+), weight tying
- Q4/MXFP4 quantization, 4-8K native context
- Muon optimizer for training

### Critical risk
KDA and CSA are not implemented in MLX. They need custom Metal kernels.
Must de-risk on shipped weights (Kimi Linear 48B-A3B) before committing
a training run.

## Next Steps (in order)

### STEP 1 — Benchmark GPT-OSS-20B on M4 Pro (1-2 hours, zero risk)

20.9B/3.6B active, MXFP4-native, runs in MLX today. Closest shipped model
to our target. Measure:
- Decode tok/s
- Prefill tok/s
- Peak memory
- KV cache behavior

Gives us a concrete baseline to beat and validates the 20B MoE scale on
Metal.

### STEP 2 — Benchmark Kimi Linear 48B-A3B on M4 Pro/Max (2-4 hours, low risk)

Open weights with actual KDA layers. If it runs in MLX (or we can make it
run), measure whether KDA's fixed-size recurrent state delivers the
zero-KV-cache advantage on Metal. This is the de-risk step — if KDA doesn't
work well in MLX, we need to know NOW, not after a training run.

### STEP 3 — Write the architecture spec (1 session)

Fill the model-config-template with:
- 20B MoE, ~2-3B active, 24-28 layers, 4096 dim
- KDA + CSA hybrid (if step 2 passes) or GQA + sliding window (fallback)
- LatentMoE (narrow routed experts, 64-128 experts, top-4 or top-8)
- SiTU-GLU activation (quantization-safe)
- 32-64K vocab (not 128K+), weight tying
- Q4/MXFP4 quantization, 4-8K native context
- Muon optimizer for training

### STEP 4 — Prototype KDA/CSA kernels in MLX (2-3 sessions, if step 2 passes)

Start with KDA recurrent state update — small kernel (fixed-size state,
matmul + gate). Then CSA compression + sparse selection. Validate
bit-exactness against PyTorch reference. Use mlx-kernel-optimization
skill's boundary profiling workflow.

### STEP 5 — PyTorch model definition + training scaffold (2-3 sessions)

Standard PyTorch model matching the spec, training loop on Modal H100.
The architecture choices from step 3 are what make it Metal-native — the
training code itself is standard.

### STEP 6 — Train small validation run (1-2 H100-hours)

1-3B params, 10-20B tokens, validate the architecture trains stably.
Check loss curves, expert load balance, KDA state behavior.

### STEP 7 — Full 20B training run (150-250 H100-hours)

If step 6 passes, scale up. Convert to MLX, Q4 quantize, benchmark on
M4 Pro. Compare against GPT-OSS-20B baseline from step 1.

## Skill Cross-Reference

metal-architecture-expert says:
- "Two budgets: bytes stored (capacity) and bytes read per token
  (bandwidth → tok/s). Classify every claim into these before adopting."
- "Custom kernels needed for KDA/CSA — not standard MLX ops. De-risk on
  shipped weights before betting a training run."
- "M4 Pro: 273 GB/s, 48GB, ~8 TFLOPS, MoE utilization factor ~0.47 measured."
- "Decode tok/s ≈ BW / (active_bytes + lm_head_bytes + kv_read_bytes) × U"
- "Try mx.compile first, custom kernel second."

llm-architecture-research says:
- "KDA eliminates KV cache for 75% of layers — best attention for Metal."
- "CSA reduces KV to ~2% of baseline AND reduces compute — ideal for
  compute-poor Apple Silicon."
- "Kimi Linear 48B-A3B has open weights with KDA — benchmarkable on M4
  Pro/Max before betting a training run."
- "GPT-OSS-20b (20.9B/3.6B, MXFP4) is the closest shipped analog to a 20B
  Metal target and runs in MLX today."
- "Always confirm against HF config.json — papers round and omit."