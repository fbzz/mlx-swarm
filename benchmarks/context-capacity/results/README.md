# Context Capacity Results

Reports produced by `python -m mlx_swarm.context_benchmark` on an Apple M4 Pro
(48 GiB) with `mlx-community/Qwen3.6-35B-A3B-4bit` (identity SHA
`e1da56d9…c19433`), seed 20260727, on 2026-08-21. Only `report.md` is kept
here; the per-case `cases.jsonl` and `results.json` stay in the local
`.swarm/context-capacity/` run directories.

| Directory | Mode | Matrix | Passed | Highest all-pass tier | Wall time |
| --- | --- | --- | --- | --- | --- |
| `promotion-v1` | copy | 2048–32768 × start/middle/end × 3 trials | 45/45 | 32768 | 23.5 min |
| `promotion-v1-retrieve` | retrieve | same | 45/45 | 32768 | 14.4 min |
| `promotion-ac-repro` | copy (stall-watch) | 2048–32768 × 3 pos × 3 trials | 45/45 | 32768 | 18 min |
| `promotion-v1-retrieve-decoys3` | retrieve, `--decoys 3` | same | 45/45 | 32768 | 13.9 min |
| `copy-65536` | copy | 65536 × 3 positions × 1 trial | 3/3 | 65536 | 6.8 min |
| `retrieve-65536` | retrieve | 65536 × 3 positions × 1 trial | 3/3 | 65536 | 6.6 min |

Evidence digests (`sha256` of each run's `results.json`):

- `promotion-v1`: `657106b30259aa80091a7b9f88da430abedcdee9ce72952ba6f3d37936c6693d`
- `promotion-v1-retrieve`: `a07c545a3d9989cfe57546ebfe850c94bb077a52cf6702a3fdf9c6f07ef9433d`
- `promotion-ac-repro`: `714eed04e9bf1f4ea9c4568e6b63d16649d960af60c242a0a655cb5a69910251`
- `promotion-v1-retrieve-decoys3`: `2619f176406f0d1858da4a584d6e0fe23371a40bc1d6080dcd2c3b83e2f3ba4d`
- `copy-65536`: `6b872e8a4ea98ac711f3e8bdc99111a902f19ea3a4a09201784bb5d48f77fd1f`
- `retrieve-65536`: `3787a73717b489ab162027ee1d75a2630d7d45142f507d9b026671eaad9227c9`

With three decoy functions also returning `"before"`, the worker pinned every anchor to the full `target()` function (45/45 identical manifests). The 65536 runs needed `batch.maxBatchPromptTokens` raised to 131072. Peak
memory was 22.3 GB at 32768 and 24.1 GB at 65536.

### Stall watch

`promotion-ac-repro` is a second full copy-mode matrix run with the per-case `wallSeconds`/`peakMemoryGigabytes` instrumentation. All 45 cases passed with a maximum `wallSeconds − generationSeconds` gap of 0.40 s, i.e. no host-side stall: every case's wall time was its generation time. Generation was ~1.8× faster than `promotion-v1` at each tier (32768 median 51 s vs 91 s), pure throughput variation with thermal state — the machine could not be moved off AC programmatically, so a true battery comparison still needs a physical unplug, but the instrumentation now attributes any future slow case to the host (a wall≫gen gap) versus the model.
