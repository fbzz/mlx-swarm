---
name: mlx-swarm-skill-map
description: Build and display a repository skill map so operators can see the codebase graph and point Swarm at a subgraph. Use when mapping a workspace, opening a new Swarm task, choosing focus paths, or visualizing package structure before planning.
---

# MLX Swarm Skill Map

Use the installed `mlx-swarm` CLI. Do not walk the tree with ad hoc scripts,
and do not approve or launch work for the operator.

## When to use

Invoke this skill when the operator wants a codebase graph, a New task focus
path, or a subgraph for a later commander plan. Then invoke
`mlx-swarm-commander` for planning or review. Simple one-file edits still go
to direct host-agent work, not Swarm.

## Show the graph

Obtain the config path from the cockpit or project `.mlx-swarm/swarm.json`.
Your first reply in this turn must include the mermaid graph. Run:

`mlx-swarm --config CONFIG map --format mermaid`

Paste the command's stdout (the fenced `mermaid` block) into the reply so the
operator sees the graph here. Do not summarize-only. The same command writes
`.mlx-swarm/codebase-map.json`, which is the graph the New task tab displays.
Do not invent nodes or follow paths outside `workspaceRoot`.

If mermaid is truncated, say so and point the operator at New task for the
full SVG.

## Honor cockpit focus

If `.mlx-swarm/ui-state.json` contains `focusPaths`, those are the operator's
highlighted subgraph. Treat them as the inspection ceiling:

- inspect only those paths and their descendants below `workspaceRoot`;
- when later planning with `mlx-swarm-commander`, confine `allowedPaths` to
  that subgraph;
- do not rebind the workspace folder or create a commander request unless the
  operator asks.

If `focusPaths` is missing or empty, still show the mermaid graph, then ask
which path to point at, or wait for a New task click.

## Install

The cockpit New task tab shows host install commands. CLI equivalent:

`mlx-swarm skill install --host claude|codex --skill mlx-swarm-skill-map`

Omitting `--skill` installs this skill together with `mlx-swarm-commander`.
Claude invoke: `/mlx-swarm-skill-map`. Codex invoke: `$mlx-swarm-skill-map`.
