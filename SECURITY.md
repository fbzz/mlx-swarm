# Security

## Supported version

Security fixes currently target the latest `0.1.x` release.

## Operating boundary

Swarm Agents is designed for local execution:

- the cockpit only accepts `127.0.0.1`, `localhost`, or `::1`;
- runtime model resolution is cache-only;
- plans are discovered below an explicitly approved directory;
- mutating HTTP requests require same-origin browser requests;
- worker output is treated as untrusted and rendered as text;
- subprocesses receive argument arrays and never invoke a shell.

Do not expose the cockpit through a reverse proxy or bind it to a public
interface. Do not place secrets in plan context or worker prompts: session
artifacts persist prompts, model output, validation evidence, and runtime
metadata to disk.

## Reporting a vulnerability

Please use the repository's private GitHub security advisory flow. Include a
minimal reproduction, affected version, and impact. Avoid opening a public
issue until a fix is available.
