# Config

Swarm configuration JSON schema — validates model, batch, artifacts, and
optional workspace-execution authority.

The config file (typically `swarm.json`) defines which model to use, how many workers can run in parallel, and where session artifacts are stored. It is validated by `load_config` in [[src/mlx_swarm/contracts.py#load_config]].

## Schema

JSON structure for the config file.

```json
{
  "schemaVersion": 2,
  "model": {
    "repository": "mlx-community/model-name",
    "revision": "",
    "localPath": "/path/to/local/model"
  },
  "batch": {
    "maxWorkers": 2,
    "prefillStepSize": 1024,
    "maxPromptCharacters": 80000,
    "maxBatchPromptTokens": 49152
  },
  "artifacts": ".swarm/runs",
  "enableThinking": false,
  "seed": 20260727,
  "worker": {
    "mode": "direct",
    "reasoningMaxTokens": 768,
    "capabilities": {
      "parameterScale": "4B",
      "contextWindowTokens": 262144,
      "maxGenerationTokens": 2048,
      "specialization": "general",
      "delegationLevel": "exact-edit",
      "strengths": [],
      "limitations": ["Unreliable independent diagnosis."],
      "calibration": {
        "status": "failed",
        "passedCases": 0,
        "totalCases": 2,
        "evidenceSha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      }
    }
  },
  "workspace": {
    "writeRoots": ["src", "tests"],
    "verificationProfiles": {
      "pytest": {
        "argv": ["python", "-m", "pytest", "-q"],
        "cwd": ".",
        "timeoutSeconds": 300,
        "inheritEnv": ["PATH", "LANG"],
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"}
      }
    }
  }
}
```

## Field Reference

All config fields with types, defaults, and constraints.

- **schemaVersion** (required, int): `1` is generation-only; `2` enables the
  required workspace contract.
- **model.repository** (required, string): HuggingFace repo ID for the MLX model.
- **model.revision** (optional, string): Git revision/tag. Empty string means latest.
- **model.localPath** (optional, string): Local filesystem path. Takes priority over repository.
- **batch.maxWorkers** (optional, int, default 2): Maximum tasks in one bounded
  dependency-level chunk. 1–32. Two is the measured default for the local 4B
  profile.
- **batch.prefillStepSize** (optional, int, default 1024): Prefill chunk size. 64–8192.
- **batch.maxPromptCharacters** (optional, int, default 80000): Max prompt length. 1024–500000.
- **batch.maxBatchPromptTokens** (optional, int, default 49152): Hard ceiling
  for aggregate rendered input tokens in one physical MLX batch. A larger
  compatible wave is split deterministically; one prompt above the ceiling is
  rejected. Per-task completion budgets are separate.
- **artifacts** (required, string): Directory for session artifacts. Relative to config file.
- **enableThinking** (optional, strict bool, default false): Configure the model chat template's thinking mode. Completed thinking blocks are never propagated as task artifacts.
- **seed** (optional, int, default 20260727): Random seed for reproducibility. 0–2^31-1.
- **worker.mode** (optional, default `direct`): `direct` performs one local
  artifact generation. `reasoning-edit` performs a local reasoning pass and a
  separate strict editing pass for mutating tasks only.
- **worker.reasoningMaxTokens** (optional, int, default 768): Local reasoning
  pass limit, 64–8192. These tokens remain in `localUsage`.
- **worker.capabilities** (optional, strict object): Auditable local-model
  envelope copied into commander prompts, evaluation configs, environment
  fingerprints, and session worker strategy.
- **worker.capabilities.parameterScale** (default `unknown`): Operator-reported
  model scale such as `4B`.
- **worker.capabilities.contextWindowTokens** (default 0/unreported): Declared
  model context window. When reported, the backend rejects any rendered prompt
  plus requested generation that exceeds it.
- **worker.capabilities.maxGenerationTokens** (default 2048): Hard per-task
  generation ceiling for the bundled profile. Values through 8192 are
  accepted by the contract; a plan exceeding the configured value is rejected.
- **worker.capabilities.specialization**: `unknown`, `general`, `code`, or
  `mixed`.
- **worker.capabilities.delegationLevel**: `exact-edit`,
  `bounded-implementation`, or `autonomous`. The default is the conservative
  `exact-edit`.
- **worker.capabilities.strengths / limitations**: Unique bounded statements
  supplied to the frontier.
- **worker.capabilities.calibration**: `unmeasured`, `passed`, or `failed`,
  passed/total case counts, and the immutable replay SHA-256 for measured
  status. Unmeasured profiles use zero counts and no digest.
- **workspace.writeRoots** (schema v2, required, non-empty array): Unique POSIX
  relative path ceilings. Absolute paths, traversal, backslashes, NUL, and
  `.git` are rejected.
- **workspace.verificationProfiles** (schema v2, required, object): Named,
  immutable verification authority. A profile has a non-empty fixed `argv`,
  optional relative `cwd` (default `.`), `timeoutSeconds` (1–3600), unique
  `inheritEnv` names, and explicit string `environment` values.

## Validation

Config validation uses strict key checking — unknown fields raise ContractError.

Empty strings are allowed for `revision` and `localPath` (both optional). The `artifacts` path is resolved relative to the config file's parent directory.

`doctor` reports the active batch and worker capability envelope. When the
checkpoint exposes `max_position_embeddings`, it compares that metadata with
`worker.capabilities.contextWindowTokens` and refuses readiness if the
configured value overstates the checkpoint.

Schema-v1 configs reject `workspace` as an unknown field. Schema-v2 workspace
execution auto-detects the nearest Git top-level above the config directory;
see [[workspace-execution]].

See [[Architecture]] for how config flows into the executor.
