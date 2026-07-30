# Security

## Supported version

Security fixes currently target the latest `0.4.x` release.

## Operating boundary

MLX Swarm is designed for local execution:

- the cockpit only accepts `127.0.0.1`, `localhost`, or `::1`;
- runtime model resolution is cache-only;
- plans are discovered below an explicitly approved directory;
- workspace execution uses the nearest resolved Git top-level above the config
  directory and records it before approval;
- frontier response slots are immutable and protected by exclusive claims;
- workspace launch approval is bound to both the canonical plan SHA-256 and an
  execution digest covering Git root, base HEAD, write roots, and referenced
  verification profiles, approval mode, and execution target;
- mutating HTTP requests require same-origin browser requests;
- local-agent output is treated as untrusted and rendered as text;
- subprocesses receive argument arrays and never invoke a shell.

Schema-v3 mutations use a retained session worktree by default. Explicit
main-checkout YOLO is available only for a completely clean repository and
only after approval of an execution digest that names that checkout target.
Unified diffs reject absolute/traversal paths, `.git`, runtime roots, symlink
traversal, binary data, rename/copy metadata, special Git modes, and paths
outside both configured and task allowlists. Apply rechecks the artifact
digest, target HEAD, and cleanliness before creating a hook-free, unsigned
commit. The original checkout is unchanged in worktree modes; checkout YOLO
changes it by explicit operator choice.

Verification commands come only from operator-authored config profiles. The
approved snapshot fixes argv, cwd, timeout, inherited environment names, and
explicit environment values for the session. Verification uses `shell=False`,
closed stdin, a sanitized environment, a confined cwd, process-group timeout
handling, and bounded output. Internal Git subprocesses ignore global and
system Git configuration and inherited `GIT_*` overrides. External filters,
diff commands, or text conversion commands remaining in repository-local
config—and unignored in-repository worktree roots—make workspace readiness
fail.

Verification subprocesses are not network-sandboxed. An operator-authored test
or tool may communicate externally, so verification profiles must be reviewed
as trusted execution authority before approval.

Do not expose the cockpit through a reverse proxy or bind it to a public
interface. Do not place secrets in plan context or local-agent prompts:
session artifacts persist prompts, model output, full diffs, decision receipts,
verification logs, validation evidence, and runtime metadata to disk.

## Reporting a vulnerability

Please use the repository's private GitHub security advisory flow. Include a
minimal reproduction, affected version, and impact. Avoid opening a public
issue until a fix is available.
