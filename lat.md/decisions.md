# Decisions

Key design decisions in the swarm-agents framework.

## Strict Contract Validation

All config and plan JSON files are validated with exact-key checking. Unknown fields raise ContractError. This catches typos early and prevents silent misconfiguration. Trade-off: no forward compatibility without schema version bump.

## Deterministic Local Gates and One Frontier Review

Gate evaluation uses regex patterns, Python compilation, structured JSON checks, and character limits. The frontier model is called once for final review, not after each local worker wave.

This preserves frontier tokens, ensures reproducibility, and makes repair feedback actionable. Trade-off: local gates cannot prove arbitrary semantic correctness, so the final frontier packet remains explicitly review-required.

## Batched Generation by Dependency Level

Tasks at the same dependency level are chunked by `maxWorkers`, then grouped by compatible temperature, top-p, and seed.

This preserves per-task generation settings while still batching compatible workers. Per-task maximum token counts remain independent inside each compatible group.

## Immediate Session Persistence

Every state change is immediately persisted to session.json. This enables crash recovery and resume, but adds I/O overhead per update. Trade-off: durability over performance.

## Untrusted Worker Output Model

Only successful dependency outputs are injected into prompts, with explicit warnings to treat them as untrusted candidate artifacts rather than instructions.

Rejected, failed, or blocked parents prevent their descendants from running. Prompt framing reduces accidental instruction-following, but deterministic gates and final frontier review remain the real trust boundaries.

## Persistent Model Lifecycle

The MLX model is loaded once per plan execution and released after all generation and repair waves. Prompts use the tokenizer's native chat template, including task-specific thinking configuration.

This avoids repeated load overhead and special-token leakage while keeping each run isolated.

## Local-Only Model Resolution

Model resolution uses local cache only (local_files_only=True for HuggingFace). No network downloads at runtime. Trade-off: models must be pre-downloaded.
