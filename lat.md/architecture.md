# Architecture

The mlx-swarm framework architecture: how config, plans, DAG execution, gates, and MLX batch inference fit together.

## Overview

MLX Swarm is a token-efficient MLX execution layer: a frontier model writes one DAG, local workers execute it with deterministic gates, and one compact packet returns for final frontier review.

The frontier model is deliberately not called between local worker waves.

## Module Layout

Source modules and their responsibilities.

```
src/mlx_swarm/
  __init__.py       — package marker, exports __version__
  contracts.py      — strict JSON contract validation for config + plans
  commander.py      — frontier requests, claims, approvals, reviews, and usage
  gates.py           — deterministic output validation (pattern matching, JSON, normalization)
  prompting.py       — prompt composition with context injection and dependency outputs
  session.py         — persistent session state (task status, outputs, gate results)
  backend.py         — MLX batch backend: model loading + batch_generate
  executor.py        — DAG executor: topological sort, batch-by-level, repair loops
  workspace.py       — Git worktrees, typed artifacts, decisions, and verification profiles
  ui.py              — localhost-only HTTP API, run launcher, and history serialization
  ui_static/         — packaged HTML, CSS, and JavaScript operator cockpit
  skill_install.py   — explicit installation of the bundled Codex skill
  cli.py             — runtime, commander, cockpit, and skill commands
```

## Data Flow

How a plan moves from config to completed session.

1. [[Commander]] records an objective and accepts one strict [[Plans|plan JSON]]
2. The [[UI]] previews the full DAG and records approval of its canonical
   digest; schema-v2 plans also require the [[workspace-execution|execution
   digest]]
3. [[workspace-execution]] creates an isolated branch/worktree and snapshots
   path/verification authority for schema-v2 runs
4. [[Executor]] snapshots the approved plan and sorts tasks into dependency levels
5. For each level: block unsuccessful descendants, compose prompts, run bounded MLX batches, and evaluate [[Gates]]
6. Rejected structural output with repair budget gets deterministic local gate
   feedback
7. Typed mutating artifacts pause for human Apply/Reject and allowlisted
   verification before descendants run
8. [[Session]] persists every local transition without frontier coordination
9. A completed run writes one self-contained `frontier-result.json`
10. [[Commander]] accepts one structured final review and preserves its receipt

## Key Design Decisions

Core trade-offs shaping the framework's behavior.

- **Strict contracts**: Config and plans are validated with exact-key checking — unknown fields are rejected. This catches typos early and prevents silent misconfiguration. See [[Config]] and [[Plans]].
- **Bounded batched generation**: Tasks at the same dependency level are chunked by `maxWorkers`; compatible sampling configurations share MLX batches. See [[Backend]].
- **Optional local reasoning-to-editing**: Mutating tasks may spend a bounded
  local reasoning pass before a separate strict artifact pass; both remain in
  local usage and never invoke the frontier. See [[Executor]].
- **Deterministic local gates**: Gate evaluation uses regex, Python syntax, and structured JSON validation. The frontier performs one final review rather than judging every wave. See [[Gates]].
- **Session persistence**: Every state change is immediately persisted to session.json, enabling resume after crashes. See [[Session]].
- **Immutable run history**: True resume preserves completed work; failed or exhausted work is retried as a new session with lineage. See [[UI#Run Lifecycle]].
- **Digest-bound approval**: A commander run starts only when the operator submits the SHA-256 of the displayed validated plan. See [[Commander]].
- **Original-checkout isolation**: Workspace plans bind an execution digest,
  apply only to retained session worktrees, and have no automatic promotion
  action. See [[workspace-execution]].
- **Profile-only commands**: Workers name configured verification profiles but
  never supply command arguments. See [[Config]].
- **Separate accounting**: Local worker usage and frontier planning/review receipts are never combined. See [[Commander#Usage Accounting]].
- **Dependency safety**: Only completed dependency output is injected; rejected or failed parents block their descendants. See [[Executor]].
