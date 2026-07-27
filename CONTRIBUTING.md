# Contributing

Thanks for helping improve Swarm Agents.

## Development setup

Swarm execution requires an Apple silicon Mac, but the contract, gate, session,
executor, CLI, and HTTP tests are deliberately model-free and run on any
platform supported by Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q
```

## Pull requests

- Keep worker execution bounded and local.
- Preserve strict JSON contracts; new fields need validation and tests.
- Treat model output as untrusted data.
- Add a regression test for every bug fix.
- Update the relevant file under `lat.md/` when behavior or architecture
  changes.
- Do not commit model weights, session artifacts, local paths, or secrets.

For UI changes, launch the cockpit and verify the loaded, empty, running,
completed, partial, and failed states:

```bash
swarm --config examples/swarm.json ui
```
