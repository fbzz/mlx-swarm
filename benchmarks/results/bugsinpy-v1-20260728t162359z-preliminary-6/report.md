## Preliminary measured economics

**Study status:** `preliminary` — Measured scores, time, and tokens are directional. The 30-pair product claim gate was not evaluated.

**Preliminary 6-pair study.** This is a directional decision gate, not the planned 30-pair claim study. The strong “saves frontier tokens without reducing acceptance” claim is disabled regardless of the observed deltas.

**Decision gate:** `stop_and_improve_workers` — Acceptance is materially behind (0/6 vs 6/6). Improve local worker patch quality before running the 30-pair study.

Pinned protocol: `BugsInPy@11c5f1eea954a42132cfd06bf257766a7963e0fd` · `gpt-5.6-sol` (high) · local `local/qwen35-4b-opus-uncensored-6bit@e017ecf449428c52171387b7dee317e4803708940b8f50ea1c1ef0d25529cd3d` · seed `20260728`.

Recorded `2026-07-28T16:55:42.669391+00:00` on `arm64` / `arm` with 48.0 GiB memory. MLX Swarm commit `2f741c0f4f195272c65feef12939db1096c36717`; Codex `codex-cli 0.145.0`.

One-time case preparation (excluded from task timing): 04:14 across 8 cases.

Scores are binary executable-oracle results. Times are end-to-end wall time and exclude one-time benchmark preparation. Frontier and local tokens are intentionally separate. This pass@1 study is one suite on one machine; it does not establish monetary savings or generalize beyond the pinned protocol.

| Metric | Frontier Alone | MLX Swarm | Delta |
|---|---:|---:|---:|
| Completed | 6/6 (100.0%) | 0/6 (0.0%) | -6 |
| Score | 6/6 | 0/6 | -6 |
| Median end-to-end time | 03:09 | 02:39 | -15.9% |
| Frontier tokens (total / median) | 2,017,393 / 406,294 | 886,183 / 138,726 | 1,131,210 fewer (56.1%) |
| Local tokens (total / median) | — | 70,304 / 8,730 | separate |
| Repairs (total / median) | — | 12 / 2 | — |
| Model loads | — | 6 | — |

| Task | Project | Frontier score | Frontier time | Frontier tokens | Swarm score | Swarm time | Swarm frontier tokens | Local tokens | Repairs | Loads | Review | Token delta | Time delta |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|
| [black-7](benchmarks/results/bugsinpy-v1-20260728t162359z-preliminary-6/cases/black-7.json) | black | 1 | 04:19 | 485,254 | 0 | 08:31 | 264,546 | 25,362 | 2 | 1 | not eligible | 220,708 | -04:11 |
| [fastapi-14](benchmarks/results/bugsinpy-v1-20260728t162359z-preliminary-6/cases/fastapi-14.json) | fastapi | 1 | 03:11 | 429,543 | 0 | 04:19 | 192,032 | 14,645 | 2 | 1 | not eligible | 237,511 | -01:09 |
| [luigi-20](benchmarks/results/bugsinpy-v1-20260728t162359z-preliminary-6/cases/luigi-20.json) | luigi | 1 | 03:07 | 430,410 | 0 | 03:01 | 125,267 | 10,390 | 2 | 1 | not eligible | 305,143 | +00:05 |
| [scrapy-19](benchmarks/results/bugsinpy-v1-20260728t162359z-preliminary-6/cases/scrapy-19.json) | scrapy | 1 | 01:18 | 138,274 | 0 | 01:32 | 55,705 | 6,026 | 2 | 1 | not eligible | 82,569 | -00:14 |
| [sanic-5](benchmarks/results/bugsinpy-v1-20260728t162359z-preliminary-6/cases/sanic-5.json) | sanic | 1 | 01:24 | 150,867 | 0 | 02:02 | 96,447 | 7,070 | 2 | 1 | not eligible | 54,420 | -00:39 |
| [tornado-8](benchmarks/results/bugsinpy-v1-20260728t162359z-preliminary-6/cases/tornado-8.json) | tornado | 1 | 07:26 | 383,045 | 0 | 02:16 | 152,186 | 6,811 | 2 | 1 | not eligible | 230,859 | +05:10 |

Study: `bugsinpy-v1-20260728t162359z-preliminary-6` · paired cases: 6/6 · 95% bootstrap token-saving interval: 117,341.3 to 256,145.8 tokens. Accepted-by-both savings: — tokens across 0 cases.

## Calibration evidence

Calibration validates the harness and does not enter headline scores.

| Task | Arm | Status | Score | Time | Frontier tokens |
|---|---|---|---:|---:|---:|
| black-11 | frontier-alone | completed | 1 | 04:25 | 903,708 |
| black-11 | mlx-swarm | failed | 0 | 08:24 | 217,672 |
| fastapi-6 | frontier-alone | completed | 1 | 02:34 | 412,749 |
| fastapi-6 | mlx-swarm | failed | 0 | 02:19 | 99,120 |
