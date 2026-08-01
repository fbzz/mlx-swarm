## Preliminary measured economics

**Study status:** `preliminary` — Measured scores, time, and tokens are directional. The 30-pair product claim gate was not evaluated.

**Protocol audit:** `valid` — Both arms used identical write roots; local file context was verified verbatim; all local generation attempts were retained.

**Preliminary 6-pair study.** This is a directional decision gate, not the planned 30-pair claim study. The strong “saves frontier tokens without reducing acceptance” claim is disabled regardless of the observed deltas.

**Decision gate:** `stop_and_improve_workers` — Acceptance is materially behind (3/6 vs 4/6). Improve local worker patch quality before running the 30-pair study.

Pinned protocol: `BugsInPy@11c5f1eea954a42132cfd06bf257766a7963e0fd` · `claude-sonnet-5` (none) via `claude-cli` / `claude-code` · local `mlx-community/Qwen3.6-35B-A3B-4bit@aff3a46a930400a012bb26f76227c311a590bc8afbc6efd4f2782d3b36063600` · seed `20260728`.

Recorded `2026-08-01T17:20:45.272226+00:00` on `arm64` / `arm` with 48.0 GiB memory. MLX Swarm commit `e02627586f2793d59f688675b2f497b8001e7f85`; frontier command `2.1.220 (Claude Code)`.

One-time case preparation (excluded from task timing): 17:58 across 8 cases.

Scores are binary executable-oracle results. Times are end-to-end wall time and exclude one-time benchmark preparation. Frontier and local tokens are intentionally separate. This pass@1 study is one suite on one machine; it does not establish monetary savings or generalize beyond the pinned protocol.

| Metric | Frontier Alone | MLX Swarm | Delta |
|---|---:|---:|---:|
| Completed | 6/6 (100.0%) | 3/6 (50.0%) | -3 |
| Score | 4/6 | 3/6 | -1 |
| Median end-to-end time | 00:05 | 02:00 | +2415.7% |
| Frontier tokens (total / median) | 1,002,017 / 181,138 | 671,465 / 96,113 | 330,552 fewer (33.0%) |
| Local tokens (total / median) | — | 24,182 / 4,090 | separate |
| Repairs (total / median) | — | 0 / 0 | — |
| Model loads | — | 5 | — |

| Task | Project | Frontier score | Frontier time | Frontier tokens | Swarm score | Swarm time | Swarm frontier tokens | Local tokens | Repairs | Loads | Review | Token delta | Time delta |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|
| [black-7](benchmarks/results/bugsinpy-sonnet-preliminary-20260801t165840z-preliminary-6/cases/black-7.json) | black | 1 | 00:03 | 205,514 | 1 | 03:07 | 144,905 | 7,664 | 0 | 1 | approved | 60,609 | -03:04 |
| [fastapi-14](benchmarks/results/bugsinpy-sonnet-preliminary-20260801t165840z-preliminary-6/cases/fastapi-14.json) | fastapi | 1 | 00:05 | 251,024 | 0 | 06:10 | 90,041 | 3,603 | 0 | 1 | not eligible | 160,983 | -06:05 |
| [luigi-13](benchmarks/results/bugsinpy-sonnet-preliminary-20260801t165840z-preliminary-6/cases/luigi-13.json) | luigi | 1 | 00:04 | 65,420 | 1 | 01:11 | 98,789 | 5,408 | 0 | 1 | approved | -33,369 | -01:07 |
| [scrapy-24](benchmarks/results/bugsinpy-sonnet-preliminary-20260801t165840z-preliminary-6/cases/scrapy-24.json) | scrapy | 0 | 03:26 | 156,761 | 0 | 02:49 | 83,245 | 4,576 | 0 | 1 | not eligible | 73,516 | +00:37 |
| [sanic-5](benchmarks/results/bugsinpy-sonnet-preliminary-20260801t165840z-preliminary-6/cases/sanic-5.json) | sanic | 1 | 00:04 | 62,264 | 1 | 01:10 | 93,437 | 2,931 | 0 | 1 | approved | -31,173 | -01:06 |
| [tornado-2](benchmarks/results/bugsinpy-sonnet-preliminary-20260801t165840z-preliminary-6/cases/tornado-2.json) | tornado | 0 | 00:08 | 261,034 | 0 | 00:00 | 161,048 | 0 | 0 | 0 | not eligible | 99,986 | +00:08 |

Study: `bugsinpy-sonnet-preliminary-20260801t165840z-preliminary-6` · paired cases: 6/6 · 95% bootstrap token-saving interval: 487.0 to 109,343.8 tokens. Accepted-by-both savings: -3,933 tokens across 3 cases.

## Calibration evidence

Calibration validates the harness and does not enter headline scores.

| Task | Arm | Status | Score | Time | Frontier tokens |
|---|---|---|---:|---:|---:|
| black-11 | frontier-alone | completed | 0 | 00:03 | 400,642 |
| black-11 | mlx-swarm | completed | 1 | 02:59 | 330,283 |
| fastapi-6 | frontier-alone | completed | 1 | 00:12 | 134,373 |
| fastapi-6 | mlx-swarm | completed | 1 | 03:07 | 118,098 |
