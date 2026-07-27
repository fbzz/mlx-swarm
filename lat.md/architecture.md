# Architecture

The swarm-agents framework architecture: how config, plans, DAG execution, gates, and MLX batch inference fit together.

## Overview

Swarm-agents is a token-efficient MLX execution layer: a frontier model writes one DAG, local workers execute it with deterministic gates, and one compact packet returns for final frontier review.

The frontier model is deliberately not called between local worker waves.

## Module Layout

Source modules and their responsibilities.

```
src/swarm_agents/
  __init__.py       — package marker, exports __version__
  contracts.py      — strict JSON contract validation for config + plans
  gates.py           — deterministic output validation (pattern matching, JSON, normalization)
  prompting.py       — prompt composition with context injection and dependency outputs
  session.py         — persistent session state (task status, outputs, gate results)
  backend.py         — MLX batch backend: model loading + batch_generate
  executor.py        — DAG executor: topological sort, batch-by-level, repair loops
  ui.py              — localhost-only HTTP API, run launcher, and history serialization
  ui_static/         — packaged HTML, CSS, and JavaScript operator cockpit
  cli.py             — CLI entrypoint: doctor, run, inspect, resume, list, ui
```

## Data Flow

How a plan moves from config to completed session.

1. Master LLM writes a [[Plans|plan JSON]] with tasks, dependencies, and gates
2. User runs `swarm --config swarm.json run plan.json`
3. `load_config` validates the [[Config|swarm config]], `load_plan` validates the plan
4. [[Executor]] topologically sorts tasks into dependency levels
5. For each level: block descendants of unsuccessful dependencies, compose prompts, run bounded MLX batches, and evaluate [[Gates]]
6. Rejected tasks with repair budget get re-run with [[Gates#Gate Feedback]]
7. [[Session]] persists all state atomically for inspection and resume
8. A single `frontier-result.json` packet is written after local execution for final frontier review
9. The optional [[UI]] reads the same atomic session state and controls isolated CLI subprocesses without adding frontier calls

## Key Design Decisions

Core trade-offs shaping the framework's behavior.

- **Strict contracts**: Config and plans are validated with exact-key checking — unknown fields are rejected. This catches typos early and prevents silent misconfiguration. See [[Config]] and [[Plans]].
- **Bounded batched generation**: Tasks at the same dependency level are chunked by `maxWorkers`; compatible sampling configurations share MLX batches. See [[Backend]].
- **Deterministic local gates**: Gate evaluation uses regex, Python syntax, and structured JSON validation. The frontier performs one final review rather than judging every wave. See [[Gates]].
- **Session persistence**: Every state change is immediately persisted to session.json, enabling resume after crashes. See [[Session]].
- **Immutable run history**: True resume preserves completed work; failed or exhausted work is retried as a new session with lineage. See [[UI#Run Lifecycle]].
- **Dependency safety**: Only completed dependency output is injected; rejected or failed parents block their descendants. See [[Executor]].
