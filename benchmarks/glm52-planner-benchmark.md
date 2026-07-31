# GLM 5.2 Planner Benchmark

A 30-minute, four-objective benchmark measuring whether a non-Anthropic
frontier model (GLM 5.2) can drive MLX Swarm end-to-end — planning and
reviewing — with local execution on the calibrated Qwen3.6-35B-A3B-4bit
worker at `bounded-implementation` delegation.

## What this measures

1. **Foreign-frontier contract compliance** — can GLM 5.2 produce a valid
   schema-v3 plan (diagnosis, changeValidation, edit-manifest tasks) from the
   self-contained plan prompt alone, within the bounded re-import budget of
   three attempts per claim?
2. **Delegation quality at bounded-implementation** — with the capability
   contract advertising bounded delegation, does the planner actually assign
   implementation to local workers instead of embedding frontier-authored
   deterministic edits?
3. **Local worker throughput and acceptance** — first-pass gate acceptance,
   repairs, escalations, verification outcomes under the 0.5.0 executor.

## Fixed configuration

| Component | Value |
| --- | --- |
| Target repository | `mercado-alerta-starter` (worktree-isolated, `node-test` profile) |
| Worker | `mlx-community/Qwen3.6-35B-A3B-4bit`, calibration 4/4, `bounded-implementation` |
| Planner / reviewer | GLM 5.2, adapter `frontier-skill` |
| Execution policy | YOLO, isolated worktree, `--max-repair 1` |
| Measured anchors | model load ~10 s; 66–88 tok/s batched generation |

## Objectives (4 runs, ~6 min local execution each)

Choose four objectives of these shapes, sized to 2–4 tasks in a wide,
shallow DAG. Each must be completable with single-file-per-task bounded
implementations — the calibrated envelope.

1. **Bug fix with evidence** — a reproducible failing behavior plus the
   relevant test output in the objective text.
2. **Small feature plus test** — e.g., add one field to an API response and
   a regression test for it.
3. **Test coverage** — add tests for one currently uncovered service module.
4. **Mechanical sweep** — one repeated bounded transformation across two to
   three files as disjoint parallel tasks (exercises wave batching).

## Protocol per objective

```bash
CONFIG=/Users/fzuin/Documents/mercado-alerta-starter/swarm.json

# 1. Create the request; note REQUEST_ID
mlx-swarm --config $CONFIG commander create --objective "..."

# 2. Claim; give GLM 5.2 the full contents of the returned promptPath.
mlx-swarm --config $CONFIG commander claim-plan REQUEST_ID --adapter frontier-skill

# 3. Save GLM's JSON response and import it; on rejection, hand GLM the
#    complete error list (all errors are reported at once) and re-import on
#    the same claim. Record attempts used (budget: 3).
mlx-swarm --config $CONFIG commander import-plan REQUEST_ID response.json --claim-id CLAIM_ID

# 4. Approve and launch from the cockpit (one action), or headless via the
#    validated plan file with --approve-preview. Record launch time.

# 5. After completion:
mlx-swarm --config $CONFIG commander claim-review SESSION_DIR --adapter frontier-skill
#    Give GLM the review promptPath contents; import its verdict.
mlx-swarm --config $CONFIG commander import-review SESSION_DIR review.json --claim-id CLAIM_ID
```

GLM usage tokens are recorded as unavailable (skill-hosted adapter). Note
GLM-side token counts manually if its host reports them.

## Metrics (all from session.json, receipts, and request records)

| Metric | Source | Target |
| --- | --- | --- |
| Plan import attempts per objective | `planPhase.importAttempts` | ≤ 2 |
| Local-agent share of mutating tasks | plan snapshot `executionMode` | ≥ 60% |
| First-pass gate acceptance | `generationAttempts[0].gatePassed` | ≥ 70% |
| Repairs / escalations used | `repairAttempts`, `escalatedMaxTokens` | informational |
| Blocked-task share | task `status == blocked` | ≤ 15% |
| Integration verification | `integrationVerificationResults` | pass on ≥ 3/4 runs |
| Wall-clock per run | batch records | ≤ 6 min local |
| GLM review verdict | `frontier-review.json` | informational |

## Budget

Local execution: 4 runs × ≤ 6 min ≈ 24 min, plus slack — under the 30-minute
cap. GLM planning/review time is outside the execution budget.

## Success and failure readings

- **Pass**: ≥ 3/4 objectives complete with integration verification green,
  metrics within targets. Conclusion: MLX Swarm is frontier-agnostic in
  practice and bounded-implementation delegation works on real work.
- **Plan-shaped failure** (imports exhaust attempts, or deterministic-edit
  dominates): the plan prompt needs GLM-specific hardening — file findings
  against the prompt, not the worker.
- **Worker-shaped failure** (gates reject at first pass, verification fails):
  the bounded-implementation envelope is too wide for real tasks — collect
  the failed prompts as new calibration cases and consider reverting the
  delegation level.

Every run leaves durable evidence under `.swarm/runs/` for post-hoc scoring;
compare the delegation mix and blocked-task share against the pre-0.5
baseline (63% blocked, deterministic-edit-dominated) from the same project.
