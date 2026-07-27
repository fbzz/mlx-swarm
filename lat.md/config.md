# Config

Swarm configuration JSON schema — validates model, batch, and artifacts settings.

The config file (typically `swarm.json`) defines which model to use, how many workers can run in parallel, and where session artifacts are stored. It is validated by `load_config` in [[src/swarm_agents/contracts.py#load_config]].

## Schema

JSON structure for the config file.

```json
{
  "schemaVersion": 1,
  "model": {
    "repository": "mlx-community/model-name",
    "revision": "",
    "localPath": "/path/to/local/model"
  },
  "batch": {
    "maxWorkers": 32,
    "prefillStepSize": 512,
    "maxPromptCharacters": 120000
  },
  "artifacts": ".swarm/runs",
  "enableThinking": false,
  "seed": 20260727
}
```

## Field Reference

All config fields with types, defaults, and constraints.

- **schemaVersion** (required, int): Must be 1.
- **model.repository** (required, string): HuggingFace repo ID for the MLX model.
- **model.revision** (optional, string): Git revision/tag. Empty string means latest.
- **model.localPath** (optional, string): Local filesystem path. Takes priority over repository.
- **batch.maxWorkers** (optional, int, default 32): Maximum parallel workers in a batch. 1–32.
- **batch.prefillStepSize** (optional, int, default 512): Prefill chunk size. 64–8192.
- **batch.maxPromptCharacters** (optional, int, default 120000): Max prompt length. 1024–500000.
- **artifacts** (required, string): Directory for session artifacts. Relative to config file.
- **enableThinking** (optional, strict bool, default false): Configure the model chat template's thinking mode. Completed thinking blocks are never propagated as task artifacts.
- **seed** (optional, int, default 20260727): Random seed for reproducibility. 0–2^31-1.

## Validation

Config validation uses strict key checking — unknown fields raise ContractError.

Empty strings are allowed for `revision` and `localPath` (both optional). The `artifacts` path is resolved relative to the config file's parent directory.

See [[Architecture]] for how config flows into the executor.
