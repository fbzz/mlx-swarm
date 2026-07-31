# Design Constraint: Single-User, Persistent-Cache, Parallel-Session LLM

## The Use Case

One user, one machine (M4 Pro 48GB), all resources dedicated to that user.
The model should:

1. Persist computed tokens (KV cache / recurrent state) across sessions
2. Run multiple parallel sessions that share cached prefix tokens
3. Dedicate 100% of compute, memory, and bandwidth to one user
4. Reuse cached state so the user never pays for the same prefix twice

This is not a serving problem. It is a personal compute problem.

## How This Changes the Architecture

### KV cache is no longer disposable — it is a persistent asset

Standard LLM inference treats KV cache as ephemeral: compute it, use it
for one request, discard it. For a single-user persistent system, KV
cache is a long-lived asset that accumulates value over time. The model
architecture should make cache cheap to save, load, and share across
sessions.

### Context length economics invert

With disposable KV cache, long context is expensive: you pay prefill
compute + memory every time. With persistent cache, you pay prefill
ONCE, then reuse. The ongoing cost is the per-token KV READ (bandwidth),
not the prefill compute.

This means:
- GQA at 128K context: 25 GB KV cache, ~91ms read per token → ~11 tok/s.
  Even with persistence, the per-token read kills decode throughput.
- KDA at any context: 12.6 MB recurrent state, constant read regardless
  of context length. ~11 tok/s ceiling from KV becomes irrelevant.
  You save 12.6 MB to disk, reload instantly, continue from any point.
- CSA at 128K context: ~500 MB compressed KV, ~1.8ms read per token.
  Manageable. And it persists to disk as ~500 MB, not 25 GB.

KDA and CSA are not just "Metal-friendly" — they are the ONLY attention
mechanisms that make persistent long-context viable on Apple Silicon.

### Parallel sessions = natural batching

Multiple concurrent sessions sharing a common prefix (system prompt,
loaded codebase, conversation history) create a natural batch:

- Session A: coding task with codebase loaded (prefix = system + codebase)
- Session B: different coding task, same codebase (shares prefix with A)
- Session C: question about previous conversation (shares prefix with A+B)

The shared prefix is computed once. Each session forks from the cached
prefix point. This is RadixAttention / SGLang-style prefix caching, but
for a single user with persistent storage.

On Apple Silicon, a batch of 2-4 parallel sessions:
- Improves prefill throughput (more compute per kernel dispatch)
- Does NOT hurt decode (each session decodes independently, batch-1)
- Shares bandwidth across sessions (unified memory makes this natural)

### Memory budget: all 48 GB for one user

No multi-user overhead. The entire memory is available for:
- Model weights: ~10 GB at Q4 (20B MoE, 2-3B active)
- Persistent KV cache: varies by attention type (see above)
- Session-specific KV: only the divergent part after the shared prefix
- Activations + intermediates: ~1-2 GB
- OS overhead: ~4-6 GB

With KDA: ~30+ GB available for cached states from many sessions.
With CSA: ~25+ GB available for compressed KV entries.
With GQA: ~15 GB for KV cache at 8K context — and 128K is impossible.

### Disk-backed cache (DeepSeek V4 precedent)

DeepSeek V4 section 3.5.2 describes on-disk KV cache storage. The same
pattern applies here:

- Cache states saved to disk (SSD, ~7 GB/s on M4 Pro)
- Loaded on demand when a session resumes
- LRU eviction when memory pressure rises
- Cache keyed by token prefix hash (like SGLang RadixAttention)

For KDA: each session's state is ~12.6 MB. 1000 sessions = 12.6 GB on
disk. Trivial.
For CSA: each session at 128K context is ~500 MB. 50 sessions = 25 GB.
Manageable.
For GQA: each session at 8K context is ~1 GB. 25 sessions = 25 GB. And
128K context sessions are 25 GB EACH — one session fills the disk cache.

## What This Means for Architecture Choices

### Attention: KDA is now even more strongly favored

KDA's fixed-size recurrent state is the ONLY attention mechanism where:
- Context length has ZERO impact on per-token decode cost
- Cache persistence is trivial (save a 12.6 MB matrix)
- Cross-session cache sharing is natural (shared prefix → shared state)
- Disk I/O for cache load/save is negligible

CSA is second-best: compressed KV is small and persistent, but still
grows with context (just 50x slower).

GQA/MLA are worst: KV cache grows linearly, persistence is expensive,
and long-context decode is bandwidth-killing.

### MoE: still favored, but for a different reason

In the multi-user case, MoE helps because it maximizes bandwidth
utilization with minimal compute. In the single-user case, MoE has an
additional benefit: the inactive expert weights are not read, so they
don't compete with KV cache reads for bandwidth. At 2-3B active out of
20B total, only 1-1.5 GB of expert weights are read per token, leaving
most of the 273 GB/s bandwidth budget for KV cache / KDA state reads.

### Context window: can be larger with KDA

Earlier recommendation was 4-8K native context. With persistent KDA
state, larger context becomes affordable because:
- Prefill compute is paid once (amortized over many sessions)
- Per-token decode cost is independent of context length
- State persistence is trivial

Recommendation: 32-128K context with KDA, 8K with CSA, 4K with GQA.
The attention choice determines the viable context length.

### Session management becomes a first-class feature

The model system needs:
- Prefix tree for shared cache (RadixAttention-style)
- Disk-backed state persistence
- Session fork/branch from any cached point
- LRU eviction with memory pressure awareness
- Bandwidth scheduling across parallel decode streams

This is infrastructure, not architecture — but the architecture should
be designed to make it easy. KDA makes it easy (small state, trivial
serialization). GQA makes it hard (large cache, expensive serialization).

## Updated Architecture Direction

| Component | Previous | Updated (persistent single-user) |
|---|---|---|
| Attention | KDA + CSA hybrid | KDA primary (persistent state), CSA for global |
| Context | 4-8K native | 32-128K with KDA (persistence makes it affordable) |
| MoE | Yes (bandwidth) | Yes (bandwidth + doesn't compete with KV reads) |
| Vocab | 32-64K | 32-64K (unchanged) |
| Layers | 24-28 | 24-28 (unchanged) |
| Quantization | Q4 | Q4 (unchanged) |
| KV persistence | Not considered | First-class design constraint |
| Session model | Not considered | Parallel sessions, shared prefix caching |
| Disk cache | Not considered | Required (SSD-backed state/KV) |

## Updated Next Steps

The 7 steps from next-steps.md still apply, but with shifted emphasis:

STEP 1 (GPT-OSS-20B benchmark) — still do this, but ALSO measure:
- KV cache size at various context lengths
- How long it takes to save/load KV cache to disk
- Whether MLX supports KV cache persistence natively

STEP 2 (Kimi Linear 48B benchmark) — now EVEN MORE critical:
- Measure KDA state save/load time (should be near-instant)
- Test resuming a session from saved KDA state
- Test forking two sessions from the same KDA state
- This directly validates the persistent-cache use case

STEP 3 (architecture spec) — add:
- Session management design (prefix tree, disk cache, LRU)
- KDA state serialization format
- Cross-session prefix sharing protocol
- Disk I/O budget (SSD bandwidth for cache load/save)

STEP 4 (kernels) — add:
- KDA state save/load (trivial but needs to be fast)
- CSA compressed KV save/load
- Prefix-cache-aware batched decode (multiple sessions in one batch)

NEW STEP — Build session manager (between steps 4 and 5):
- RadixAttention-style prefix tree
- Disk-backed cache with LRU eviction
- Session fork/branch/resume API
- This is the infrastructure that makes the model "personal"