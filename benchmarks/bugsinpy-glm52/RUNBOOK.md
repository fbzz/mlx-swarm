# GLM 5.2 Economics Evaluation — Runbook

Self-contained operating instructions for the preliminary BugsInPy economics
study: GLM 5.2 frontier (hermes-completion adapter) vs MLX Swarm with the
calibrated Qwen3.6-35B-A3B-4bit local worker, under fair-evaluation
protocol 14.

## State (as of 2026-07-31)

- `hermes/glm52-evaluation` branch merged to main (protocol 14); full suite
  350 passed.
- Profile: `benchmarks/bugsinpy-glm52/profile.json` — schema 3, hermes
  `glm-5.2` via ollama-cloud, pin `Hermes Agent v0.19.0 (2026.7.20) ·
  upstream cbc1054e` (matches installed), full sizes 6/30,
  `--preliminary` derives 2 calibration + 6 measured.
- Worker config: `benchmarks/bugsinpy-glm52/swarm-eval.json` (also copied to
  `.swarm/eval-config.json`) — Qwen3.6-35B-A3B-4bit from the HF cache,
  calibration passed 4/4, `bounded-implementation`.
- Container image present: `mlx-swarm-bugsinpy-amd64:11c5f1eea954-py37`
  (digest-pinned); Docker via colima.
- Old `.swarm/evaluations` evidence deleted (published sanitized exports
  remain in git under `benchmarks/results/`); ~23 GiB free vs the 15 GiB
  profile gate.

## Requirements before any phase

- Clean `mlx-swarm` checkout (prepare records the source commit and every
  phase revalidates the frozen environment).
- Docker (colima) running; Hermes CLI resolving to the pinned version;
  Hermes/ollama-cloud credentials valid for `glm-5.2` (needed from the
  pilot phase onward, not for prepare).
- The Qwen3.6-35B-A3B-4bit checkpoint in the HF cache (cache-only
  resolution).

## Sequence

```bash
cd /Users/fzuin/Desktop/swarm-agents
CONFIG=benchmarks/bugsinpy-glm52/swarm-eval.json
PROFILE=benchmarks/bugsinpy-glm52/profile.json

# 1. Freeze the preliminary suite (2 calibration + 6 measured cases).
#    Long: clones BugsInPy metadata, builds per-case runtimes in Docker,
#    proves buggy-fails/fixed-passes for every case. Resumable:
#    add --resume EVALUATION_ID if interrupted before sealing.
mlx-swarm --config $CONFIG eval prepare $PROFILE --preliminary

# 2. Pilot (calibration) phase — needs GLM reachable via hermes.
#    --preliminary is REQUIRED: prepare sealed the derived 2+6 profile, and
#    run must derive it the same way or it fails with "Evaluation profile
#    differs from the prepared snapshot."
mlx-swarm --config $CONFIG eval run EVALUATION_ID --phase pilot --profile $PROFILE --preliminary

# 3. Zero-frontier local replay gate (no frontier calls; unlocks measured).
mlx-swarm --config $CONFIG eval replay-local EVALUATION_ID

# 4. Measured phase (6 paired cases).
mlx-swarm --config $CONFIG eval run EVALUATION_ID --phase measured --profile $PROFILE --preliminary

# 5. Status / report (preliminary reports never emit the savings claim).
mlx-swarm --config $CONFIG eval status EVALUATION_ID
mlx-swarm --config $CONFIG eval report EVALUATION_ID --preliminary
```

`EVALUATION_ID` is printed by prepare and listed under `.swarm/evaluations/`.

## Decision gate

The preliminary report emits a decision gate: expansion to the sealed
30-case study stops if swarm acceptance is lower than frontier-alone.
Preliminary output is explicitly labeled and never supports the
token-savings claim; only the full study with all thirty valid usage pairs
and a positive seeded bootstrap lower bound can emit it.

## Known failure modes

- Version drift (hermes upgrade, package updates) fails phases closed —
  re-pin the profile deliberately, never casually.
- Docker/daemon problems classify arms `invalid` (never score zero);
  candidate test failures score zero (never invalid).
- Storage gate: prepare refuses below 15 GiB free; the evaluations dir
  budget is 20 GiB.
- Evidence files are write-protected; cleanup needs
  `chflags -R nouchg && chmod -R u+w` before deletion.
