# Changelog

All notable changes to MLX Swarm are documented in this file.

## [Unreleased]

### Added

- Claude Code installation for the shared `mlx-swarm-commander` Agent Skill
  through `mlx-swarm skill install --host claude`, including
  `CLAUDE_CONFIG_DIR` discovery.
- Provider-neutral structured planning and review handoffs with host-specific
  receipt provenance for Claude Code, Codex, and compatible Agent Skills hosts.

### Changed

- Skill installation now requires `--host claude` or `--host codex`.
- Cockpit and public documentation use frontier-agent language instead of
  presenting Codex as the product boundary.
- Planning and review imports can no longer override the host adapter sealed by
  their claim.
- Skill replacement refuses symlink destinations before resolving or removing
  the leaf path.

## [0.4.0] - 2026-07-30

### Added

- Direct routing for simple one- or two-file cosmetic and mechanical changes,
  avoiding Swarm overhead when decomposition and governance add no value.
- Compact, digest-bound `frontier-review-input.json` packets for final frontier
  review while preserving the full `frontier-result.json` audit record.
- Incremental commander revisions that carry validated completed work from one
  retained, clean isolated worktree and replan only the unfinished subgraph.
- Cockpit and CLI support for selecting a predecessor run with `revisionOf` /
  `--revision-of`.
- Durable one-successor enforcement, fresh approval receipts, predecessor
  evidence, and retry-ancestry checks for revision runs.

### Changed

- The default local execution profile now uses two concurrent agents, a
  49,152-token aggregate prompt ceiling, and a 2,048-token model generation
  ceiling.
- Workspace execution contracts advance to schema version 3 for revision
  authority and inherited-base binding.
- Exact-edit tasks default to zero autonomous repair attempts and are split on
  truncation instead of consuming repeated local generations.
- Final review receives bounded patch and report excerpts instead of the
  complete planning and execution payload.

### Security and reliability

- Final-review claims bind both the compact review packet and its source result
  digest, detecting changes to either artifact.
- Revision creation excludes active or partially reviewed predecessors and
  validates retained branch ancestry, worktree cleanliness, carried artifacts,
  and execution authority.
- Failed commander-evidence attachment is cleaned up and cannot be resumed as a
  valid launched session.

[0.4.0]: https://github.com/fbzz/mlx-swarm/releases/tag/v0.4.0
