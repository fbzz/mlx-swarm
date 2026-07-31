# Gates

Deterministic output validation for untrusted worker responses.

Gates validate local-worker output using regex patterns, Python compilation, structured JSON rules, and character limits. No frontier call is spent on intermediate gates; one final frontier review consumes the compact result packet. Implemented in [[src/mlx_swarm/gates.py]].

## Gate Structure

Python dataclass defining gate rules.

```python
OutputGate(
    required_patterns=(GatePattern("has-def", r"def "),),
    forbidden_patterns=(GatePattern("no-import", r"^import "),),
    max_characters=5000,
    output_format="text",  # "text" or "json"
    strip_single_code_fence=True,
    python_syntax=True,
)
```

## Evaluation Pipeline

1. **Normalize model protocol**: Remove completed thinking blocks, trailing model role tokens, common preambles, outer whitespace, and an authorized single code fence.
2. **Size check**: Reject if output exceeds `max_characters`.
3. **Format check**: If format is "json", validate JSON parsing.
4. **Typed checks**: Optionally compile Python or validate required/allowed JSON keys and enum fields.
5. **Required patterns**: Each regex must match (MULTILINE mode).
6. **Forbidden patterns**: No regex may match (MULTILINE mode).

See [[src/mlx_swarm/gates.py#evaluate_gate]].

## Gate Feedback

When a gate fails, `gate_feedback_for_repair` generates a structured feedback string listing all violations. This is injected into a repair prompt that re-runs the task.

The [[Executor]] checks each rejected task's `maxRepairAttempts` budget and the
global `--max-repair` cap; the effective budget is their minimum. The CLI and
cockpit default the global cap to one, while a plan task that omits
`maxRepairAttempts` still defaults to zero. When both permit a repair, the
executor composes a prompt with gate feedback and the previous failed output,
then re-runs through MLX until the task passes or the budget is exhausted.
Token-limited output stays repairable only for one bounded ceiling
escalation; without escalation headroom it fails fast. A repair that would
deterministically replay a recorded prior dispatch is skipped without
spending the generation call. Deterministic-edit payloads are size-checked
against `maxCharacters` at plan import, before any run starts. See
[[src/mlx_swarm/executor.py#execute_plan]].

## Normalization

Output cleaning before gate evaluation.

- **Model protocol cleanup**: Finished thinking content and trailing chat-role tokens are excluded from the artifact.
- **Preamble stripping**: Regex matches "Here's/Here is/Sure/Let me/I'll" patterns at start of output.
- **Code fence stripping**: If the entire output is a single markdown code block, the fence is removed. Only applies when `strip_single_code_fence=True`.
- Normalizations are logged in the gate result for auditability.
