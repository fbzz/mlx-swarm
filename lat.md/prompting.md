# Prompting

Prompt composition — assembles authoritative context, dependency outputs, and role-specific tasks.

Prompts are composed by `compose_prompt` in [[src/swarm_agents/prompting.py#compose_prompt]]. The composition depends on whether shared context is provided.

## With Context

When a plan includes a [[Plans|context object]], the prompt is assembled from these sections in order:

1. **AUTHORITY**: Warning that the contract is authoritative and worker output is untrusted.
2. **OBJECTIVE**: The context objective.
3. **AUTHORITATIVE SOURCE**: One section per source, with label, origin, and sha256.
4. **GLOBAL CONSTRAINTS**: Numbered list of constraints.
5. **AUTOMATIC REJECTION CONDITIONS**: Numbered list of rejection criteria.
6. **DEPENDENCY OUTPUT**: For each `dependsOn` task, the normalized output of that task (if completed).
7. **WORKER IDENTITY**: The task id and role.
8. **ROLE-SPECIFIC TASK**: The task's prompt text.
9. **DETERMINISTIC VALIDATION**: Exact gate checks the worker must satisfy.
10. **OUTPUT PROTOCOL**: Task-specific instructions, falling back to shared context.

## Without Context

When no context is provided, the prompt is simpler:

1. WORKER IDENTITY (id and role)
2. Task prompt
3. Successful dependency outputs (if session is provided)
4. Deterministic validation requirements
5. Extra sections and optional task output protocol

## Dependency Injection

How completed task outputs are injected into dependent task prompts.

For each task in `dependsOn`, only a successfully completed normalized output is injected. Dependency outputs are framed as untrusted candidate artifacts; the plan contract remains authoritative. Rejected or failed dependencies are blocked by the [[Executor]] before prompting. See [[src/swarm_agents/prompting.py#_dependency_sections]].

## Repair Prompts

When a task fails its gate, `compose_repair_prompt` appends:
- The original prompt
- REPAIR FEEDBACK section with gate violations
- The previous rejected output (truncated to 4000 chars)
- Instructions to return only corrected output

See [[src/swarm_agents/prompting.py#compose_repair_prompt]].
