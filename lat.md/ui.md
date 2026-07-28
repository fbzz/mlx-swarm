# UI

The Local MLX Swarm Cockpit is a dependency-free dashboard served by
[[src/mlx_swarm/ui.py]] and launched with:

```sh
mlx-swarm --config examples/swarm.json ui
```

It binds to `127.0.0.1:8765` by default. `--plans-dir` selects the approved plan
root, `--port 0` selects a free port, and `--no-open` prevents automatic browser
launch. Non-local bind hosts are rejected.

## API

- `GET /api/status` reports model/config readiness and the approved roots.
- `GET /api/plans` discovers and validates frontier-authored plan files.
- `GET /api/commander/requests` lists frontier planning requests.
- `GET /api/commander/requests/{requestId}` returns its prompt, validation,
  full plan preview, digest, receipt, and handoff.
- `GET /api/runs` lists immutable sessions newest first.
- `GET /api/runs/{planId}/{sessionId}` returns the plan snapshot, topological
  levels, task/gate/output state, batch evidence, local usage, and final packet.
- `POST /api/runs` launches a selected plan.
- `POST /api/commander/requests` creates an objective and constraints request.
- `POST /api/commander/requests/{requestId}/approve-run` verifies the displayed
  plan digest, records approval, and launches its immutable snapshot.
- `POST /api/runs/{planId}/{sessionId}/resume` resumes pending work.
- `POST /api/runs/{planId}/{sessionId}/retry` creates a linked fresh run.
- `POST /api/runs/{planId}/{sessionId}/artifacts/{taskId}/apply` submits the
  displayed artifact digest for application.
- `POST /api/runs/{planId}/{sessionId}/artifacts/{taskId}/reject` rejects a
  pending artifact or reverts an applied artifact after failed verification.
- `POST /api/runs/{planId}/{sessionId}/artifacts/{taskId}/verify` reruns only
  the approved profile IDs after failed verification.
- `POST /api/runs/{planId}/{sessionId}/workspace/cleanup` removes a terminal
  worktree while retaining its branch.

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
- commander composition, approved plans, and searchable run history in the left rail;
- a keyboard-selectable DAG grouped by topological wave in the center;
- Overview, Output, Gate, and Runtime task evidence in the right inspector.
- separate local/frontier usage plus final verdict findings.
- workspace root/base/head/branch/worktree evidence, dirty-source warning, full
  escaped diff preview, digest-bound Apply/Reject controls, configured command
  evidence, bounded logs, and the final branch diff.

Live work polls once per second, completed work every five seconds, and hidden
pages do not poll. All task output and JSON evidence is rendered as text rather
than executable HTML.

Workspace plan launch submits both the displayed canonical plan digest and the
execution digest. The browser and server never invoke a frontier provider
directly. See [[workspace-execution]].
