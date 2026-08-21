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
| `copy-65536` | copy | 65536 × 3 positions × 1 trial | 3/3 | 65536 | 6.8 min |
| `retrieve-65536` | retrieve | 65536 × 3 positions × 1 trial | 3/3 | 65536 | 6.6 min |

The 65536 runs needed `batch.maxBatchPromptTokens` raised to 131072. Peak
memory was 22.3 GB at 32768 and 24.1 GB at 65536.
