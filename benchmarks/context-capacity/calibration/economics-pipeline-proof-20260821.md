# Economics Pipeline Proof — 2026-08-21

A one-measured-case run proving the full paired BugsInPy economics pipeline
executes end-to-end on this machine (Apple M4 Pro, 48 GiB) with the current
environment. Not a published study — a feasibility + acceptance check.

## Environment

- Evaluation: `bugsinpy-sonnet-preliminary-20260821t132535z` (fresh, gitignored).
- Profile: derived from `benchmarks/bugsinpy-sonnet/profile.json`, frontier
  version repinned to the installed `2.1.238 (Claude Code)`.
- Frontier: `claude-cli` → `claude-sonnet-5` (Claude Code, subscription).
- Local worker: `mlx-community/Qwen3.6-35B-A3B-4bit`, `reasoning-edit`, 768.
- Oracle container: `mlx-swarm-bugsinpy-amd64:11c5f1eea954-py37` (`linux/amd64`).
- MLX Swarm commit: `0bef8ee`.

## Phases and outcome

| Phase | Outcome | Wall time |
| --- | --- | --- |
| prepare (clone BugsInPy + build 7 per-case runtimes) | prepared | ~78 min |
| pilot — black-11, fastapi-6, both arms (real frontier) | 2/2, both arms score 1 | ~7 min |
| local-replay gate (zero frontier, oracle) | passed, measuredEligible | ~4 min |
| measured — fastapi-14, both arms (oracle-verified) | frontier-alone 1, mlx-swarm 1 | ~28 min |

Total ~2 h; dominated by amd64 emulation on a 4-CPU colima VM shared with
other running containers. The remaining 5 measured cases would add ~2.3 h.

## What it shows

- The paired pipeline runs cleanly and the Claude Code frontier bridge
  authenticates.
- On the measured `fastapi-14` case, **mlx-swarm (frontier plan → local Qwen
  worker) matched frontier-alone**: both produced fixes that passed the
  independent BugsInPy pytest oracle.
- The local-replay promotion gate passed 2/2 with zero frontier calls
  (`economics-pipeline-replay-gate-20260821.json`,
  replay output sha256 `7ebd1cf252872719bc43f05a2dbc8bb1658129d7b0911d77cb7985db2aac273a`).

## Limitation — no cost delta via this adapter

The `claude-cli` (Claude Code, subscription) adapter records frontier token
usage as **unavailable** rather than estimating it, so the study's cost
comparison ("saves N% frontier tokens") is not measurable with the Claude
frontier. That number requires a token-reporting adapter (`codex-cli` or
hermes/`glm-5.2`; see `benchmarks/bugsinpy-glm52/RUNBOOK.md`). This proof
therefore establishes acceptance parity and pipeline feasibility only.
