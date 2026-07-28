# Economics Evaluation

The economics harness implements a paired, pass-at-one comparison between a
frontier-only repair and the Frontier Commander plus local MLX execution path.
See [[src/mlx_swarm/evaluation.py]].

## Frozen suite

`mlx-swarm eval prepare PROFILE` clones the pinned BugsInPy metadata revision,
filters unsupported cases, and freezes the calibration and measured cases.

The committed profile uses seed `20260728`, six projects, no more than five
measured cases per project, and balanced reference patch-size strata. It
selects six calibration cases and thirty measured cases.

The frozen suite contains only metadata needed to reproduce the study. Fixed
patch text is never copied into an arm workspace or model prompt. Every arm
starts from a history-free repository containing only the buggy tree and the
fixed revision's designated tests.

Preparation requires a clean MLX Swarm checkout and records its source commit.
After selection, the BugsInPy metadata clone and project mirrors are deleted.
Fixed-revision compilation bypasses the shared ccache so fixed objects are not
retained for model execution.

## Paired arms

Each case runs sequentially with seeded arm order and an independent oracle.

Frontier Alone receives one clean buggy repository and one end-to-end Codex
turn. MLX Swarm receives one frontier plan, local worker execution with at
most two repairs, and one final frontier review only after a completed local
run. The evaluation harness can approve typed artifacts only inside disposable
case workspaces; normal sessions retain their human approval boundary.

Both candidate diffs are scored in fresh oracle workspaces using the same
frozen verifier. A score of one means the clean executable oracle passed.
Review verdicts are retained separately and never change this score.

## Evidence and economics

Raw evidence lives below `.swarm/evaluations/<evaluationId>/`. Per-arm results
are immutable and retain the timing, usage, patch, and oracle record.

Each result includes wall-clock phase timing, all `turn.completed` usage,
local tokens, repair and model-load counts, patch digest, changed-file count,
and oracle evidence. Missing frontier usage makes an arm measurement invalid;
it is never converted to zero.

`mlx-swarm eval report` writes a sanitized immutable export below
`benchmarks/results/<evaluationId>/` and deterministically renders the README
tables. The token-saving claim is emitted only when all thirty usage pairs are
valid, swarm completion and executable score are not lower, and the seeded 95%
bootstrap lower bound for paired frontier-token savings is positive.
