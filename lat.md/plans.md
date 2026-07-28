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
  "constraints": ["Must be pure Python"],
  "rejectionCriteria": ["No external deps"],
  "outputProtocol": "Return complete code only."
}
```

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
  "maxRepairAttempts": 2,
  "generationOverride": { "temperature": 0.1, "max_tokens": 600 }
}
```

### Roles

Available task roles and their default generation parameters.

- **implementation**: Code generation (temp 0.15, top_p 0.9, max_tokens 1800)
- **test**: Test writing (temp 0.10, top_p 0.95, max_tokens 1600)
- **review**: Code review (temp 0.0, top_p 1.0, max_tokens 700)
- **general**: General purpose (temp 0.2, top_p 0.9, max_tokens 1200)

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

## Schema-v2 workspace tasks

A plan with `"schemaVersion": 2` requires a schema-v2 config and adds three
required task fields:

```json
{
  "id": "implement",
  "role": "implementation",
  "prompt": "Return one unified Git diff.",
  "artifactType": "patch",
  "allowedPaths": ["src/package"],
  "verification": ["pytest"]
}
```

`artifactType` is `patch`, `test-suite`, `review`, or `report`. Patch and
test-suite tasks require one or more task path ceilings below configured
`workspace.writeRoots`; their verification array contains only configured
profile IDs. Review and report tasks require empty path and verification
arrays. Plans and workers cannot provide commands.

At most one patch/test-suite task may occur in each topological level. This
serializes human-controlled mutations while preserving parallel local
generation for non-mutating artifacts. See [[workspace-execution]].
