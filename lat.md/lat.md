This directory defines the high-level concepts, business logic, and architecture of this project using markdown. It is managed by [lat.md](https://www.npmjs.com/package/lat.md) — a tool that anchors source code to these definitions. Install the `lat` command with `npm i -g lat.md` and run `lat --help`.

- [[index]] — Root index of all lat.md sections.
- [[architecture]] — Overall architecture: how config, plans, DAG execution, gates, and MLX batch inference fit together.
- [[backend]] — MLX batch backend: model resolution, loading, and batched generation.
- [[config]] — Swarm configuration JSON schema: model, batch, and artifacts settings.
- [[commander]] — Frontier planning requests, digest approval, final review, and separate usage receipts.
- [[decisions]] — Key design decisions and their trade-offs.
- [[economics-evaluation]] — Reproducible BugsInPy paired study, immutable evidence, metrics, and claim gate.
- [[executor]] — DAG executor: topological sort, batch-by-level, repair loops.
- [[gates]] — Deterministic local validation using regex, Python syntax, and structured JSON rules.
- [[plans]] — Plan JSON schema: DAG of tasks with dependencies, gates, and shared context.
- [[prompting]] — Prompt composition with context injection and dependency outputs.
- [[session]] — Persistent session state plus the compact final frontier-review packet.
- [[tests]] — Test specifications for backend, contracts, gates, prompting, session, executor, and CLI.
- [[ui]] — Localhost work cockpit, same-origin API, packaged dashboard, and immutable retry lineage.
- [[workspace-execution]] — Typed artifacts, isolated Git worktrees, human decisions, and allowlisted verification.
