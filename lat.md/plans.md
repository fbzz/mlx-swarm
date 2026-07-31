# Plans

Plan JSON schema — defines a DAG of tasks with dependencies, gates, and shared context.

A plan is the master LLM's decomposition of work into a dependency-ordered task graph. It is validated by `load_plan` in [[src/mlx_swarm/contracts.py#load_plan]].

## Schema

Top-level JSON structure for a plan.

```json
{
  "schemaVersion": 1,
  "planId": "unique-plan-id",
  "objective": "What this plan accomplishes",
  "context": { ... },
  "tasks": [ ... ]
}
```

## Context

Optional shared context injected into every task prompt. See [[Prompting]].

```json
{
  "objective": "Produce a correct, tested module",
  "authoritativeSources": [
    { "label": "API Spec", "content": "def foo(): pass" }
  ],
  "diagnosis": {
    "observedFailure": "The required implementation is absent.",
    "causalHypothesis": "The API function is still a stub.",
    "validationMethod": "source-trace",
    "validationEvidence": "The exact API Spec excerpt contains pass.",
    "falsificationCondition": "Another authoritative path implements it.",
    "evidenceSources": ["API Spec"]
  },
  "constraints": ["Must be pure Python"],
  "rejectionCriteria": ["No external deps"],
  "outputProtocol": "Return complete code only."
}
```

`diagnosis` remains optional for externally-authored legacy plans, but is
mandatory for every new [[Commander]] response. Its evidence-source labels must
refer to exact entries in `authoritativeSources`.

## Tasks

Each task has an id, role, prompt, optional gate, and optional dependsOn.

```json
{
  "id": "implement",
  "role": "implementation",
  "prompt": "Implement the function.",
  "outputProtocol": "Return Python source only.",
  "gate": {
    "requiredPatterns": [{ "id": "has-def", "pattern": "def " }],
    "forbiddenPatterns": [{ "id": "no-import", "pattern": "^import " }],
    "maxCharacters": 5000,
    "format": "text",
    "stripSingleCodeFence": true,
    "pythonSyntax": true
  },
  "dependsOn": ["other-task-id"],
  "maxRepairAttempts": 0,
  "generationOverride": { "temperature": 0.1, "max_tokens": 600 }
}
```

### Roles

Available task roles and their default generation parameters.

- **implementation**: Code generation (temp 0.15, top_p 0.9, max_tokens 1024)
- **test**: Test writing (temp 0.10, top_p 0.95, max_tokens 1024)
- **review**: Code review (temp 0.0, top_p 1.0, max_tokens 768)
- **general**: General purpose (temp 0.2, top_p 0.9, max_tokens 1536)

### Dependency Ordering

Tasks are topologically sorted by `dependsOn`. Tasks at the same level are chunked by `maxWorkers` and grouped by compatible sampling settings. Circular dependencies are detected and rejected. See [[src/mlx_swarm/contracts.py#Plan#topological_order]].

### Gates

Gates validate worker output deterministically. Text gates can require valid Python syntax. JSON gates can require/allow exact keys and constrain scalar field values. See [[Gates]].

## Validation

Rules for plan and task field validation.

- Plan IDs must match `^[a-z0-9][a-z0-9._-]{0,63}$`
- Task IDs must be unique
- All `dependsOn` targets must exist
- No self-dependencies or dependency cycles
- Maximum 128 tasks per plan
- Gate patterns are compiled (regex) at load time to catch syntax errors early
- Boolean, output-format, generation-override, and JSON-schema fields are type-checked
- Task-specific `outputProtocol` overrides the shared context protocol

## Workspace plan schemas

A plan with `"schemaVersion": 2` requires a schema-v2 config and retains the
legacy typed-artifact contract. A plan with `"schemaVersion": 3` uses the same
config authority and adds the delegation and integration contract:

```json
{
  "id": "implement",
  "role": "implementation",
  "prompt": "Return exact search/replace edits.",
  "artifactType": "patch",
  "workerOutputProtocol": "edit-manifest-v1",
  "executionMode": "local-agent",
  "contextRefs": ["implementation-source"],
  "interfaceContract": "Preserve the public API and its return type.",
  "expectedOutputTokens": 450,
  "allowedPaths": ["src/package"],
  "verification": ["pytest"]
}
```

`artifactType` is `patch`, `test-suite`, `review`, or `report`. Patch and
test-suite tasks require one or more task path ceilings below configured
`workspace.writeRoots`; their verification array contains only configured
profile IDs. Review and report tasks require empty path and verification
arrays. Plans and workers cannot provide commands.

Schema v3 requires `edit-manifest-v1` for every local mutating worker and
validates `expectedOutputTokens` at no more than 70% of that task's generation
budget. `contextRefs` selects unique labels from
`context.authoritativeSources`; unselected sources are omitted from the worker
prompt. `interfaceContract` freezes the boundary the worker must preserve.

When the exact transformation is already known, `executionMode:
"deterministic-edit"` embeds `deterministicEdits`, permits no generation
override or repair, and consumes zero local generation calls. Plan validation
rejects a deterministic-edit task whose compact serialized `{"edits": [...]}`
payload exceeds its own `gate.maxCharacters`, so a self-contradictory task
fails at import instead of at runtime. A task that omits `maxRepairAttempts`
defaults to zero repair; the CLI and cockpit global cap defaults to one, and
the effective budget is the minimum of the two. Plan validation accumulates
per-task errors and reports them all in one `PlanValidationError` instead of
stopping at the first.

Independent patch/test-suite tasks may occur in one topological level only
when their `allowedPaths` are pairwise disjoint, including directory-prefix
overlap. They generate together against the wave base and apply safely only
while the affected paths remain unchanged. Legacy schema-v2 plans retain the
one-mutation-per-level rule.

Schema v3 also requires top-level `integrationVerification`, a non-empty list
of configured profile IDs run against the combined head after every task
completes. Supervised execution waits for human decisions; YOLO uses the
separately approved execution policy. See [[workspace-execution]].
