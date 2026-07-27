# UI

The Local Swarm Work Cockpit is a dependency-free dashboard served by
[[src/swarm_agents/ui.py]] and launched with:

```sh
swarm --config examples/swarm.json ui
```

It binds to `127.0.0.1:8765` by default. `--plans-dir` selects the approved plan
root, `--port 0` selects a free port, and `--no-open` prevents automatic browser
launch. Non-local bind hosts are rejected.

## API

- `GET /api/status` reports model/config readiness and the approved roots.
- `GET /api/plans` discovers and validates frontier-authored plan files.
- `GET /api/runs` lists immutable sessions newest first.
- `GET /api/runs/{planId}/{sessionId}` returns the plan snapshot, topological
  levels, task/gate/output state, batch evidence, local usage, and final packet.
- `POST /api/runs` launches a selected plan.
- `POST /api/runs/{planId}/{sessionId}/resume` resumes pending work.
- `POST /api/runs/{planId}/{sessionId}/retry` creates a linked fresh run.

Mutation requests accept small JSON objects only and reject cross-origin browser
requests. Plan and session identifiers are resolved only below their configured
roots.

## Run Lifecycle

Launching creates the session and plan snapshot before an isolated CLI subprocess
starts. The subprocess receives an argument array and never invokes a shell.
`runner.log` is stored beside `session.json` for local diagnostics.

True resume reuses the session, preserves completed tasks, and continues pending
or interrupted work with the original repair cap. A partial or failed session is
never reset in place: Retry creates a new session whose `retryOf` field points to
the original.

## Dashboard

The packaged interface is a dense dark operator cockpit:

- readiness and local token metrics in the top strip;
- approved plans and searchable run history in the left rail;
- a keyboard-selectable DAG grouped by topological wave in the center;
- Overview, Output, Gate, and Runtime task evidence in the right inspector.

Live work polls once per second, completed work every five seconds, and hidden
pages do not poll. All task output and JSON evidence is rendered as text rather
than executable HTML.
