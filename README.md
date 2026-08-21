<div align="center">

# MLX Swarm

**Keep frontier tokens for planning, not for every file edit.**

A frontier coding agent writes a frozen task graph. One resident MLX model on
your Mac executes the bounded work. The frontier agent comes back once to
review the evidence.

[![Release](https://img.shields.io/github/v/release/fbzz/mlx-swarm?label=release)](https://github.com/fbzz/mlx-swarm/releases/latest)
[![CI](https://github.com/fbzz/mlx-swarm/actions/workflows/ci.yml/badge.svg)](https://github.com/fbzz/mlx-swarm/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Apple silicon](https://img.shields.io/badge/Apple%20silicon-MLX-111111?logo=apple)](https://github.com/ml-explore/mlx)
[![Status: alpha](https://img.shields.io/badge/status-alpha-F59E0B)](#current-scope)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

[Install](#quick-start) · [Desktop app](#operate-it) ·
[How it works](#how-it-works) · [Evidence](#measured-evidence)

</div>

MLX Swarm is a local-first coding-agent runtime for Apple silicon. Claude Code,
Codex, Cursor, or another [Agent Skills](https://code.claude.com/docs/en/slash-commands)
host owns diagnosis and the graph. Local workers on a resident MoE checkpoint
do exact edits, deterministic file writes, tests, and structured reviews. You
approve the contract; they cannot invent shell commands.

During a run the checkpoint, prompts, patches, retries, and session ledger stay
on your machine. The Python runtime does not call a remote model between local
waves. The only bytes that leave are the planning prompt and the compact final
review packet you choose to send.

| Plan once | Execute locally | Review once |
| --- | --- | --- |
| Frontier host returns a digest-bound DAG: paths, interfaces, gates, tests | Default profile runs up to two ready local agents on one loaded MLX model | Frontier host reads one evidence packet and returns approve or reject |

This is not a small model pretending to be an architect. The strong model keeps
design authority. The local model receives one file, one transformation, and a
ceiling.

## What you get

- **Frontier Commander.** Create a request, claim a plan, import a strict
  schema-v3 graph, then bind the plan digest and the Git execution digest in
  one approval.
- **Desktop app.** `mlx-swarm app` is the Mac operator shell: open a folder,
  watch the live graph, apply or reject patches from the task inspector, and
  keep a PTY in the dock. Electron is a repo checkout plus `npm install`; it
  is not on PyPI.
- **Browser cockpit.** `mlx-swarm ui` serves the same API at
  `127.0.0.1:8765` without Node. Use it when you do not want the desktop
  window.
- **Exact-edit workers.** The frontier host freezes diagnosis and the DAG.
  Local agents render the edit-manifest. `deterministic-edit` is only for a
  tiny already-known literal, not whole files.
- **Two bundled skills.** `mlx-swarm skill install` writes
  `mlx-swarm-commander` and `mlx-swarm-skill-map`. New task shows a bounded
  bounded codebase graph, then the skill install cards. Clicking a feature
  stores its implementing files as `focusPaths` without starting a run.
  `mlx-swarm map --format mermaid` prints the same graph for `/mlx-swarm-skill-map`.
- **Isolated Git worktrees.** Supervised mode pauses on every patch. YOLO
  applies inside the frozen scope. Isolated branches are never merged for you.
- **Durable evidence.** Plans, apply receipts, verification logs, commits, and
  the final review packet persist under `.swarm/`.

The marketing site in [`website/`](website/) is the same product in public
copy. This README is the install and operator contract.

## Why this split

Frontier tokens are expensive when they rewrite every file. A 4B–35B local
model is unreliable when you ask it to discover an architecture. MLX Swarm
keeps each job on the model that can actually do it:

- Frontier: diagnosis, decomposition, source anchors, interfaces, acceptance.
- Local: bounded patches, test modules, reports, and mechanical reviews.
- Runtime: one Metal load, two-agent batches, gates before Git apply.
- Operator: visible approvals, a retained branch, and a packet you can audit.

The result is more self-driving than pasting diffs by hand, and much more
bounded than an unconstrained shell agent.

## How it works

```mermaid
flowchart LR
    A["Objective in the desktop app"] --> R{"Simple change?"}
    R -->|"Yes"| D["Frontier host edits directly"]
    R -->|"No"| P["Commander skill writes a frozen DAG"]
    P --> H["You approve plan digest + execution digest"]
    H --> L["Up to two local agents at a time"]
    L --> G["Gates, Git apply, configured tests"]
    G --> W["Isolated worktree + session evidence"]
    W --> C["Compact review packet"]
    C --> F["One frontier review"]
    F -->|"Fix remaining work"| I["One linked successor"]
```

### 1. Decide, then plan

The bundled `mlx-swarm-commander` skill refuses Swarm when the change is one or
two files of copy, layout, or a literal replacement. For governed work it
returns exactly one plan: a wide, shallow DAG, disjoint path ceilings, and
either a mechanical exact-edit prompt the local model must render, or, for a
tiny literal only, `deterministic-edit`. It does not
choose supervised versus YOLO; that stays an operator decision.

### 2. Approve, then execute

One Continue in the app (or `run PLAN --approve-preview`) binds:

- the **plan digest**, the exact task graph;
- the **execution digest**, that graph plus repo root, base commit, write
  roots, verification profiles, approval mode, and worktree versus checkout.

MLX Swarm then schedules ready tasks, validates every artifact, and applies
only after the digest-bound human action in supervised mode. Downstream tasks
see completed output as untrusted candidate material. Failed ancestors block
their descendants.

### 3. Review, then optionally revise

A completed session keeps `frontier-result.json` and a smaller
`frontier-review-input.json`. The frontier host reviews that packet once.
Incremental carry-forward is one successor from a terminal isolated worktree:
carried commits stay, unfinished task IDs are not reused, and you approve the
new graph separately.

## Operate it

On a Mac with a git checkout of this repo, start with the desktop app:

```bash
npm install
mlx-swarm --config examples/swarm.json app
```

Open the project folder. That creates or reuses `<folder>/.mlx-swarm/`. New
task installs skills and can map the tree. Commander holds the planning inbox.
Reviews hold live and finished runs. In supervised mode each waiting patch
shows Apply and Reject on the task; the right panel is the artifact, not a
second copy of the plan.

The app binds its local API on a free port. Do not start a second Electron
window against the same project while one is already open.

The browser cockpit is the same API without a PTY or Node:

```bash
mlx-swarm --config examples/swarm.json ui
```

It binds to `http://127.0.0.1:8765` unless you pass `--port`. Non-local bind
hosts are refused.

A project can keep its own `.mlx-swarm/swarm.json` so the desktop app and CLI
share one artifact directory. Point `artifacts` at the same `.swarm/runs`
tree you already use from the command line.

## Quick start

### Requirements

- Apple silicon Mac
- Python 3.11 or newer
- A local MLX checkpoint (cache-only at runtime; nothing is downloaded mid-run)
- About 19 GB of disk and 32 GB of memory for the reference 35B-A3B 4bit
  profile; a 4B–9B checkpoint with a tighter capability envelope can fit 16 GB
- Node.js, only if you want `mlx-swarm app` (Electron plus `node-pty`)

```bash
git clone https://github.com/fbzz/mlx-swarm.git
cd mlx-swarm

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

hf download mlx-community/Qwen3.6-35B-A3B-4bit

mlx-swarm --config examples/swarm.json doctor
```

Install the commander and skill-map skills into your frontier host. With no
`--skill` flag, both bundled skills are written:

```bash
mlx-swarm skill install --host claude   # ~/.claude/skills/
mlx-swarm skill install --host codex    # ~/.codex/skills/
```

`CLAUDE_CONFIG_DIR` and `CODEX_HOME` are respected. Cursor speaks the same
Agent Skills files; there is no `--host cursor` yet, so point the Claude
layout at Cursor's skills directory:

```bash
mlx-swarm skill install --host claude --skills-dir ~/.cursor/skills
mlx-swarm skill install --host claude --skills-dir .cursor/skills
```

The Cursor adapter on claim and import is `frontier-skill`. To pin skills to
one Claude Code repo instead:

```bash
mlx-swarm skill install --host claude --skills-dir .claude/skills
```

Smoke-test a generation-only plan that does not mutate the tree:

```bash
mlx-swarm --config examples/swarm.json run examples/plan.json --verbose
```

Then open the desktop app and run a real objective through Commander.

The skill uses the frontier host you already have. MLX Swarm does not ask for a
second provider API key.

## Two agents, not two roles

A plan can contain many implementation, test, review, and report tasks. The
shipped profile runs at most two ready agents together when sampling settings
and mutation paths are compatible. Context is not sliced into two 128K shares.

| Limit | v0.5 default | Meaning |
| --- | ---: | --- |
| Checkpoint context | 262,144 tokens | Advertised maximum per model request |
| Prompt characters | 80,000 | Conservative pre-tokenization ceiling per task |
| Physical batch input | 49,152 tokens | Combined rendered input for one local batch |
| Task generation | 2,048 tokens | Hard per-task output ceiling |
| Exact-edit recommendation | ≤2,048 tokens | Preferred ceiling for mechanical edits; keep expected output ≤1,400 |

If an artifact would exceed about 70% of its generation ceiling, split the task
into more local-agent work. Blind retries will not invent a larger model.

## Safe by construction

| Mode | Where changes land | Behavior |
| --- | --- | --- |
| Supervised | Isolated worktree | Pause before every patch or test-suite Apply |
| YOLO, recommended | Isolated worktree | Apply and verify inside the approved scope |
| YOLO, explicit | Current checkout | Allowed only on a completely clean repository |

YOLO is self-driving inside a frozen contract:

- agents never return executable shell commands;
- tests are preconfigured argument arrays, no shell;
- every mutating task has an allowed path ceiling;
- overlapping parallel writes are rejected;
- patches pass path, mode, symlink, and `git apply --check`;
- each accepted mutation is its own commit on the selected target;
- failed verification is not success: supervised and checkout pause; isolated
  YOLO may revert and repair only while budget remains;
- a truncated generation gets one bounded ceiling escalation;
- isolated worktrees are never merged or promoted automatically.

### What stays local

Checkpoint, prompts, candidate diffs, test logs, repairs, and `.swarm/`
evidence stay on the machine. Operator verification commands are local
subprocesses and are not network-sandboxed: if you configured a test that
talks to the network, it still can. When a frontier host does not expose
exact usage, Swarm records `unavailable` instead of inventing a number.

Keep `.swarm/` private. Session files can contain source.

## Measured evidence

Runtime machinery and model quality are separate claims.

| Evidence | Current result |
| --- | --- |
| Release | [`v0.5.1`](https://github.com/fbzz/mlx-swarm/releases/tag/v0.5.1) |
| Regression suite | 400 collected tests |
| CI | Green on Python 3.11, 3.12, and 3.13 |
| Maintainer calibration | Qwen3.6 35B-A3B 4bit, 4/4 first pass, including two single-file diagnoses |
| Throughput on M4 Pro 48 GiB | 78.5 tok/s one worker, 126.5 tok/s width two, 160 tok/s width four, ≤20.2 GB |
| GLM 5.2 planner benchmark | 6/6 schema-v3 plans imported first try; 81.8% first-pass gates; 4/4 integration verifications |
| Runtime model policy | Cache-only resolution; one resident MLX model |

The GLM 5.2 study is one external planner on one Node repository. Protocol,
session audit, and the gate-sizing defect it exposed (since fixed) live in
[`benchmarks/glm52-planner-benchmark-results.md`](benchmarks/glm52-planner-benchmark-results.md).

The shipped example still advertises `exact-edit` with `unmeasured` calibration
for *your* machine. Reproducing calibration locally is the gate for raising
`delegationLevel` to `bounded-implementation`. Multi-file diagnosis, API
discovery, and architecture stay with the frontier at every measured level.

A generated BugsInPy economics table is appended below. Treat it as a
directional worker-quality signal, not a product proof that Swarm saves money.

## Configure a project

Copy [`examples/swarm.json`](examples/swarm.json) or keep
`.mlx-swarm/swarm.json` beside the repo. Change:

- `model.repository` / `model.localPath`
- worker capability numbers to match the checkpoint you actually run
- `workspace.writeRoots`
- `workspace.verificationProfiles` (argument arrays, not shell strings)
- `artifacts` so the desktop app and CLI see the same runs

Frontier Commander authors plans in normal use. A mutating local-agent task
looks like this:

```json
{
  "id": "implement-parser",
  "role": "implementation",
  "prompt": "Apply the specified parser change and return only the edit manifest.",
  "artifactType": "patch",
  "workerOutputProtocol": "edit-manifest-v1",
  "executionMode": "local-agent",
  "contextRefs": ["parser-source"],
  "interfaceContract": "Preserve parse(text: str) -> ParseResult.",
  "expectedOutputTokens": 450,
  "allowedPaths": ["src/parser"],
  "verification": ["pytest-parser"]
}
```

Do not ask the local model to find missing source, invent an API, or choose an
architecture. Those sentences belong in the frontier plan.

## Essential commands

```text
mlx-swarm --config CONFIG doctor
mlx-swarm --config CONFIG app
mlx-swarm --config CONFIG ui
mlx-swarm --config CONFIG map
mlx-swarm --config CONFIG run PLAN
mlx-swarm --config CONFIG run PLAN --approve-preview
mlx-swarm --config CONFIG list
mlx-swarm --config CONFIG inspect SESSION_DIR
mlx-swarm --config CONFIG resume SESSION_DIR
mlx-swarm --config CONFIG commander create --objective TEXT
mlx-swarm skill install --host HOST
mlx-swarm stats
```

`HOST` is `claude` or `codex`. Cursor uses `--host claude --skills-dir …`.
See `mlx-swarm COMMAND --help` for the rest.

## Documentation

| Topic | Guide |
| --- | --- |
| System design | [`lat.md/architecture.md`](lat.md/architecture.md) |
| Configuration | [`lat.md/config.md`](lat.md/config.md) |
| Plan and task contracts | [`lat.md/plans.md`](lat.md/plans.md) |
| Frontier Commander | [`lat.md/commander.md`](lat.md/commander.md) |
| Local execution | [`lat.md/executor.md`](lat.md/executor.md) |
| Worktrees, approvals, and YOLO | [`lat.md/workspace-execution.md`](lat.md/workspace-execution.md) |
| Desktop app and cockpit | [`lat.md/ui.md`](lat.md/ui.md) |
| MLX batching | [`lat.md/backend.md`](lat.md/backend.md) |
| Sessions and review packets | [`lat.md/session.md`](lat.md/session.md) |
| Test specification | [`lat.md/tests.md`](lat.md/tests.md) |
| Public landing page | [`website/`](website/) |
| Release history | [`CHANGELOG.md`](CHANGELOG.md) |

[`lat.md/`](lat.md/) is the technical book. This README is for deciding whether
to install and how to run the first governed session.

## Current scope

MLX Swarm is alpha software for people who want governed local agents, not an
open-ended coding intern.

- Apple silicon and MLX only.
- The shipped worker profile is exact-edit, not autonomous repo diagnosis.
- Gates prove shape and policy, not that the patch is semantically right.
- The desktop app needs a git checkout and `npm install`; `pip install`
  mlx-swarm alone is not enough for Electron.
- Main-checkout YOLO needs a clean tree.
- Worktree branches are never merged automatically.
- Incremental carry-forward is one successor from a terminal isolated worktree.
- Runtime model downloads are disabled on purpose.

If you need an unconstrained shell agent or a sealed economics winner, this is
not that project yet.

## Contributing

Useful work includes MLX profiles, sealed calibration cases, cockpit and
desktop-app fixes, deterministic gates, and clearer operator docs.

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Report vulnerabilities through
[`SECURITY.md`](SECURITY.md).

## License

[MIT](LICENSE)

<!-- BEGIN MLX-SWARM-ECONOMICS -->
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
<!-- END MLX-SWARM-ECONOMICS -->
