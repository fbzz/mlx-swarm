# Worker Calibration — 2026-08-21

Calibration of the `mlx-community/Qwen3.6-35B-A3B-4bit` local worker on this
machine (Apple M4 Pro, 48 GiB). Written to the machine-local
`.mlx-swarm/swarm.json` (which is gitignored); the evidence lives here.

## Result

`worker.capabilities.calibration` was moved from `unmeasured` to:

```json
{ "status": "passed", "passedCases": 45, "totalCases": 45,
  "evidenceSha256": "657106b30259aa80091a7b9f88da430abedcdee9ce72952ba6f3d37936c6693d" }
```

`delegationLevel` stays `exact-edit` (unchanged).

## Basis — exact-edit context calibration (measured)

The evidence digest is the `results.json` of the copy-mode context-capacity
promotion matrix (`promotion-v1`): the worker returned an exact edit manifest
in 45/45 cases across 2048–32768 rendered tokens at start/middle/end over three
trials. The retrieve-mode matrix (worker locates the anchor itself) also passed
45/45 (`a07c545a…f9433d`). This directly evidences the declared `exact-edit`
delegation level. See `../results/`.

## Diagnosis calibration — attempted, blocked on the oracle

A frozen-plan BugsInPy replay (`mlx-swarm eval replay-local`, zero frontier
calls) was run against the two calibration cases `black-11` and `fastapi-6`.
The worker produced **gate-passing edit manifests for both** (see
`bugsinpy-replay-20260821.json` / `.log`), but the independent oracle reported
`infrastructure_error` and the promotion gate stayed locked, because the pinned
Docker/colima BugsInPy image is absent and cannot be rebuilt on the current
free disk (~10 GiB). Colima was started only to check for the image and stopped
again; no measured-work gate was changed.

To complete the oracle-verified diagnosis (`bounded-implementation`) calibration,
bring up colima with the pinned `mlx-swarm-bugsinpy-amd64` image (see
`benchmarks/bugsinpy-glm52/RUNBOOK.md`) on a machine with sufficient free disk,
then re-run the replay.
