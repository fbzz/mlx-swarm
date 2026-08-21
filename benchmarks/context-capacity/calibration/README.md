# Worker Calibration — 2026-08-21

Calibration of the `mlx-community/Qwen3.6-35B-A3B-4bit` local worker on this
machine (Apple M4 Pro, 48 GiB). Written to the machine-local
`.mlx-swarm/swarm.json` (gitignored); the evidence lives here.

## Result — oracle-verified diagnosis calibration (passed)

`mlx-swarm eval replay-local bugsinpy-sonnet-preliminary-20260818t181355z`
(worker mode `reasoning-edit`, 768 reasoning tokens, **zero frontier calls**)
replayed the two frozen Sonnet-authored calibration plans against the local
worker and verified each fix with the independent BugsInPy oracle running the
pinned `mlx-swarm-bugsinpy-amd64` container (`linux/amd64`):

| Case | Fix | Oracle | Score |
| --- | --- | --- | --- |
| black-11 | split_line comment placement | pytest exit 0 | 1 |
| fastapi-6 | form list-param handling | pytest exit 0 | 1 |

Promotion gate: **passed** (`measuredEligible: true`). Evidence:
`bugsinpy-replay-20260821-oracle-passed.json` / `.log`
(replay.json sha256 `1246a404f170fd945e7fa1ad8e14b9a61aef56653253a9cc54ace8b87acc9590`).

On the strength of this, the machine-local config was set to:

```json
"delegationLevel": "bounded-implementation",
"calibration": { "status": "passed", "passedCases": 2, "totalCases": 2,
  "evidenceSha256": "1246a404f170fd945e7fa1ad8e14b9a61aef56653253a9cc54ace8b87acc9590" }
```

Because `.mlx-swarm/` is gitignored, the config change takes effect on this
machine and is not in the repo; only this evidence is.

## Supporting — exact-edit context calibration

The context-capacity matrix independently shows the worker returns an exact
edit manifest in 45/45 cases across 2048–32768 rendered tokens in both copy
and retrieve modes (`../results/`, digests `657106b3…` and `a07c545a…`).

## Reproducing

1. `colima start` (needs the pinned `mlx-swarm-bugsinpy-amd64` image in the VM
   and ~15 GiB free host disk).
2. `python -m mlx_swarm.cli --config .swarm/eval-config.json eval replay-local \`
   `  bugsinpy-sonnet-preliminary-20260818t181355z \`
   `  --worker-mode reasoning-edit --reasoning-max-tokens 768`

The first attempt on 2026-08-21 failed with `infrastructure_error` because
colima was stopped; starting it (after clearing stale lima locks) and freeing
host disk to ~44 GiB let the oracle run and pass.
