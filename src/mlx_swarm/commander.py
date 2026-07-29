"""Frontier Commander contracts, prompts, claims, and durable receipts."""
# @lat: [[Commander]]

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .contracts import (
    MAX_PROMPT_CHARS,
    MAX_REPAIR_ATTEMPTS,
    MAX_TASKS_PER_PLAN,
    MAX_WORKERS,
    ContractError,
    Plan,
    SwarmConfig,
    load_plan,
    worker_capabilities_payload,
)

COMMANDER_SCHEMA_VERSION = 1
FRONTIER_REVIEW_SCHEMA_VERSION = 1
MAX_FRONTIER_RESPONSE_BYTES = 1_000_000
MAX_OBJECTIVE_CHARS = 20_000
MAX_CONSTRAINTS = 128
MAX_CONSTRAINT_CHARS = 4_000
MAX_REVIEW_FINDINGS = 128
MAX_REVIEW_TEXT_CHARS = 40_000
REVIEW_VERDICTS = {"approved", "changes_requested", "rejected"}
REVIEW_SEVERITIES = {"critical", "high", "medium", "low"}
USAGE_STATUSES = {"reported", "unavailable"}
PHASE_STATUSES = {"open", "claimed", "accepted", "invalid"}

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_SAFE_LINEAGE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"
)


class CommanderError(RuntimeError):
    """Raised when a commander workflow transition is invalid."""


@dataclass(frozen=True)
class FrontierUsage:
    """Usage reported by one frontier phase."""

    usage_status: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "usageStatus": self.usage_status,
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "totalTokens": self.total_tokens,
        }


@dataclass(frozen=True)
class FrontierReceipt:
    """Immutable receipt for one accepted frontier artifact."""

    phase: str
    adapter: str
    artifact_sha256: str
    accepted_at: str
    usage: FrontierUsage
    provider: str | None = None
    model: str | None = None
    input_artifact_sha256: str | None = None

    def to_json(self) -> dict[str, Any]:
        value = {
            "schemaVersion": COMMANDER_SCHEMA_VERSION,
            "phase": self.phase,
            "adapter": self.adapter,
            "provider": self.provider,
            "model": self.model,
            "artifactSha256": self.artifact_sha256,
            "acceptedAt": self.accepted_at,
            "acceptedResponses": 1,
            "attemptedResponses": 1,
            "usage": self.usage.to_json(),
        }
        if self.input_artifact_sha256 is not None:
            value["inputArtifactSha256"] = self.input_artifact_sha256
        return value


@dataclass(frozen=True)
class PlanApproval:
    """Digest-bound operator approval for a validated frontier plan."""

    request_id: str
    plan_sha256: str
    approved_at: str
    source: str = "cockpit"
    execution_digest: str | None = None
    workspace_root: str | None = None
    base_sha: str | None = None
    approval_mode: str | None = None
    workspace_target: str | None = None
    execution_policy_sha256: str | None = None

    def to_json(self) -> dict[str, Any]:
        value = {
            "schemaVersion": COMMANDER_SCHEMA_VERSION,
            "requestId": self.request_id,
            "planSha256": self.plan_sha256,
            "approvedAt": self.approved_at,
            "source": self.source,
        }
        if self.execution_digest is not None:
            value.update({
                "executionDigest": self.execution_digest,
                "workspaceRoot": self.workspace_root,
                "baseSha": self.base_sha,
            })
            if self.approval_mode is not None:
                value["approvalMode"] = self.approval_mode
            if self.workspace_target is not None:
                value["workspaceTarget"] = self.workspace_target
            if self.execution_policy_sha256 is not None:
                value["executionPolicySha256"] = (
                    self.execution_policy_sha256
                )
        return value


@dataclass(frozen=True)
class ReviewFinding:
    id: str
    severity: str
    title: str
    evidence: str
    recommendation: str
    task_id: str | None = None


@dataclass(frozen=True)
class FrontierReview:
    schema_version: int
    session_id: str
    plan_id: str
    verdict: str
    summary: str
    findings: tuple[ReviewFinding, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "sessionId": self.session_id,
            "planId": self.plan_id,
            "verdict": self.verdict,
            "summary": self.summary,
            "findings": [
                {
                    "id": finding.id,
                    "severity": finding.severity,
                    "title": finding.title,
                    "evidence": finding.evidence,
                    "recommendation": finding.recommendation,
                    **(
                        {"taskId": finding.task_id}
                        if finding.task_id is not None
                        else {}
                    ),
                }
                for finding in self.findings
            ],
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json_sha256(value: Any) -> str:
    """Hash a JSON value using stable UTF-8 canonical serialization."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def frontier_usage(
    *,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
) -> FrontierUsage:
    """Validate reported usage, or return an explicit unavailable receipt."""
    values = (prompt_tokens, completion_tokens, total_tokens)
    if all(value is None for value in values):
        return FrontierUsage("unavailable")
    if any(
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        for value in values
    ):
        raise CommanderError(
            "prompt, completion, and total tokens must all be non-negative "
            "integers when frontier usage is reported."
        )
    assert prompt_tokens is not None
    assert completion_tokens is not None
    assert total_tokens is not None
    if total_tokens != prompt_tokens + completion_tokens:
        raise CommanderError(
            "total frontier tokens must equal prompt plus completion tokens."
        )
    return FrontierUsage(
        "reported",
        prompt_tokens,
        completion_tokens,
        total_tokens,
    )


def parse_frontier_review(
    raw: Any,
    *,
    expected_plan_id: str,
    expected_session_id: str,
) -> FrontierReview:
    """Strictly parse one frontier review response."""
    value = _object(raw, "review")
    _exact_keys(
        value,
        "review",
        {
            "schemaVersion",
            "sessionId",
            "planId",
            "verdict",
            "summary",
            "findings",
        },
    )
    schema_version = _integer(
        value["schemaVersion"],
        "review.schemaVersion",
        1,
        100,
    )
    if schema_version != FRONTIER_REVIEW_SCHEMA_VERSION:
        raise CommanderError(
            f"Unsupported frontier review schema version: {schema_version}"
        )
    session_id = _identifier(value["sessionId"], "review.sessionId")
    plan_id = _identifier(value["planId"], "review.planId")
    if session_id != expected_session_id or plan_id != expected_plan_id:
        raise CommanderError("Frontier review identity does not match the session.")
    verdict = _text(value["verdict"], "review.verdict", 64)
    if verdict not in REVIEW_VERDICTS:
        raise CommanderError(
            "review.verdict must be approved, changes_requested, or rejected."
        )
    summary = _text(
        value["summary"],
        "review.summary",
        MAX_REVIEW_TEXT_CHARS,
    )
    findings_raw = value["findings"]
    if not isinstance(findings_raw, list):
        raise CommanderError("review.findings must be an array.")
    if len(findings_raw) > MAX_REVIEW_FINDINGS:
        raise CommanderError(
            f"review.findings cannot exceed {MAX_REVIEW_FINDINGS} entries."
        )
    findings: list[ReviewFinding] = []
    seen: set[str] = set()
    for index, item in enumerate(findings_raw):
        name = f"review.findings[{index}]"
        finding = _object(item, name)
        _exact_keys(
            finding,
            name,
            {"id", "severity", "title", "evidence", "recommendation"},
            {"taskId"},
        )
        finding_id = _identifier(finding["id"], f"{name}.id")
        if finding_id in seen:
            raise CommanderError(f"Duplicate review finding id: {finding_id}")
        seen.add(finding_id)
        severity = _text(finding["severity"], f"{name}.severity", 32)
        if severity not in REVIEW_SEVERITIES:
            raise CommanderError(
                f"{name}.severity must be critical, high, medium, or low."
            )
        task_id = None
        if "taskId" in finding:
            task_id = _identifier(finding["taskId"], f"{name}.taskId")
        findings.append(
            ReviewFinding(
                id=finding_id,
                severity=severity,
                title=_text(finding["title"], f"{name}.title", 500),
                evidence=_text(
                    finding["evidence"],
                    f"{name}.evidence",
                    MAX_REVIEW_TEXT_CHARS,
                ),
                recommendation=_text(
                    finding["recommendation"],
                    f"{name}.recommendation",
                    MAX_REVIEW_TEXT_CHARS,
                ),
                task_id=task_id,
            )
        )
    return FrontierReview(
        schema_version=schema_version,
        session_id=session_id,
        plan_id=plan_id,
        verdict=verdict,
        summary=summary,
        findings=tuple(findings),
    )


def parse_json_response(text: str) -> Any:
    """Parse exact JSON, allowing only one authorized outer code fence."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            raise CommanderError("Frontier response has an incomplete code fence.")
        opener = lines[0].strip().lower()
        if opener not in {"```", "```json"}:
            raise CommanderError("Only an outer JSON code fence is allowed.")
        stripped = "\n".join(lines[1:-1]).strip()
        if "```" in stripped:
            raise CommanderError("Nested code fences are not allowed.")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise CommanderError(f"Frontier response is not exact JSON: {exc}") from exc


def build_plan_prompt(
    request: dict[str, Any],
    config: SwarmConfig,
) -> str:
    """Build the deterministic planning instruction used by all adapters."""
    constraints = "\n".join(
        f"- {constraint}" for constraint in request["constraints"]
    ) or "- None supplied."
    worker_capability_contract = _worker_capability_contract(config)
    workspace_contract = ""
    task_workspace_fields = ""
    integration_shape = ""
    schema_version = 1
    if config.workspace is not None:
        schema_version = 3
        profile_ids = sorted(config.workspace.verification_profiles)
        if not profile_ids:
            raise CommanderError(
                "Workspace commander plan schema v3 requires at least one "
                "configured verification profile."
            )
        profiles = ", ".join(profile_ids)
        integration_shape = (
            '\n  "integrationVerification": ["approved-profile-id"],'
        )
        roots = ", ".join(config.workspace.write_roots)
        workspace_contract = f"""
WORKSPACE EXECUTION CONTRACT
- Every task must declare artifactType, allowedPaths, and verification.
- Every task must declare executionMode, contextRefs, interfaceContract, and
  expectedOutputTokens.
- artifactType is patch, test-suite, review, or report.
- executionMode local-agent delegates one bounded operation to MLX.
- executionMode deterministic-edit embeds already-known exact old/new edits in
  deterministicEdits. The runtime applies those bytes without loading a model.
- Every patch and test-suite task must use workerOutputProtocol
  edit-manifest-v1. Direct unified-diff generation is retired in schema v3.
- edit-manifest-v1 requires a JSON gate whose jsonRequiredKeys and
  jsonAllowedKeys are both exactly ["edits"].
- review and report tasks use workerOutputProtocol artifact.
- patch and test-suite require at least one allowed path.
- review and report use empty allowedPaths and verification arrays.
- contextRefs contains only authoritative source labels needed by that task.
- interfaceContract freezes the exact behavior shared with sibling tasks.
- expectedOutputTokens must be zero for deterministic-edit. For a local
  mutating task it must be positive and no more than 70 percent of max_tokens.
- deterministic-edit uses maxRepairAttempts 0, expectedOutputTokens 0, no
  generationOverride, and embeds at least one exact deterministicEdits item.
- task allowedPaths must stay within configured write roots: {roots}
- verification may contain only these profile IDs: {profiles}
- integrationVerification lists allowlisted profiles run once after every task
  has completed.
- workers never receive or produce command arrays.
- Multiple patch or test-suite tasks may share a DAG level only when their
  allowedPaths are pairwise disjoint and their interface contracts make them
  semantically independent. The runtime generates them together and applies
  them in deterministic plan order.
"""
        task_workspace_fields = """
      "artifactType": "patch|test-suite|review|report",
      "workerOutputProtocol": "artifact|edit-manifest-v1",
      "executionMode": "local-agent|deterministic-edit",
      "contextRefs": ["authoritative source label"],
      "interfaceContract": "frozen behavior shared with sibling tasks",
      "expectedOutputTokens": 400,
      "deterministicEdits": [
        {"path": "relative/file", "old": "exact old", "new": "exact new"}
      ],
      "allowedPaths": ["relative/path"],
      "verification": ["approved-profile-id"],"""
    return f"""You are the frontier commander for MLX Swarm.

Create exactly one strict JSON plan. Return JSON only: no prose and no markdown.
The operator will preview and approve the plan before any local work begins.

OBJECTIVE
{request["objective"]}

OPERATOR CONSTRAINTS
{constraints}

APPROVED WORKSPACE ROOT
{request["workspaceRoot"]}

Inspect only files below the approved workspace root. Put any material source
text needed by workers into context.authoritativeSources as inline content.
When a source comes from a workspace file, include its repository-relative
path in the label and copy one exact contiguous excerpt. Never summarize,
rewrite, or silently remove lines inside a source excerpt.
Do not ask local workers to call tools, execute code, or read arbitrary paths.
{worker_capability_contract}

CAUSAL DIAGNOSIS GATE
- Before emitting the plan, inspect the supplied failure evidence and trace the
  relevant source path during this same planning call.
- context.diagnosis is mandatory. State the observed failure, one falsifiable
  causal hypothesis, how it was validated, the concrete evidence, and the
  condition that would prove it wrong.
- validationMethod is source-trace or approved-verification. Never execute an
  unapproved command. Use approved-verification only when its receipt/output is
  available in the request; otherwise perform a source-trace.
- evidenceSources must name authoritativeSources labels containing exact
  excerpts that support the diagnosis. Do not claim validation from a source
  excerpt that does not expose the relevant control or data flow.
- If the causal hypothesis cannot be supported, do not substitute a speculative
  repair. Continue inspection within the same call until it can be supported.

CANDIDATE CHANGE SPECIFICITY GATE
- context.diagnosis.changeValidation is mandatory for commander plans.
- Validation must cover the proposed change, not only the suspected root cause.
- candidateChange states the exact behavioral effect encoded by the task edits.
- failingPathPrediction traces how the proposed change alters the observed
  failing input or state through the cited source.
- preservedControlPrediction names at least one passing or non-target control
  path and traces why the proposed change leaves its behavior correct.
- minimalityEvidence proves the changed predicate or transformation uses the
  narrowest distinguishing property supported by the failure evidence. Do not
  replace the observed discriminator with a broader correlated proxy.
- changeValidation.evidenceSources must name exact authoritative excerpts that
  expose the failing path, proposed target, and preserved control reasoning.
- For exact-edit delegation, the candidateChange must match the literal old-to-
  new transformations in the mutating task prompts.
- A source trace that only explains current code is insufficient. Simulate the
  proposed change on both the failing path and the preserved control during this
  same call. If either prediction is unsupported, continue inspection and do
  not emit a plan.

PLAN LIMITS
- schemaVersion must be {schema_version}.
- planId and task IDs use lowercase letters, digits, dot, underscore, or hyphen.
- 1 to {MAX_TASKS_PER_PLAN} tasks.
- roles: implementation, test, review, or general.
- dependency graph must be acyclic.
- per-task maxRepairAttempts: 0 to 5; normal default {MAX_REPAIR_ATTEMPTS}.
- maximum local batch workers: {min(config.batch.max_workers, MAX_WORKERS)}.
- worker prompt budget: {min(config.batch.max_prompt_characters, MAX_PROMPT_CHARS)} characters.
- use deterministic gates wherever artifact shape can be checked.
- no task generationOverride.max_tokens may exceed \
{config.worker.capabilities.max_generation_tokens}.
- For local-agent patch or test-suite tasks, target 350 to 500 expected output
  tokens and set max_tokens to at most \
{min(800, config.worker.capabilities.max_generation_tokens)}.
- Reject or deterministically split a local task whose expected output would
  exceed 70 percent of max_tokens.
- For review tasks, set max_tokens to at most \
{min(1000, config.worker.capabilities.max_generation_tokens)}.
- For report tasks, set max_tokens to at most \
{min(1800, config.worker.capabilities.max_generation_tokens)}.
{workspace_contract}

TOP-LEVEL SHAPE
{{
  "schemaVersion": {schema_version},
  "planId": "lowercase-id",
  "objective": "string",{integration_shape}
  "context": {{
    "objective": "string",
    "diagnosis": {{
      "observedFailure": "string",
      "causalHypothesis": "string",
      "validationMethod": "source-trace|approved-verification",
      "validationEvidence": "string",
      "falsificationCondition": "string",
      "evidenceSources": ["authoritative source label"],
      "changeValidation": {{
        "candidateChange": "exact behavioral effect encoded by task edits",
        "failingPathPrediction": "candidate effect on observed failing path",
        "preservedControlPrediction": "named passing or non-target path preserved",
        "minimalityEvidence": "why this is the narrowest supported discriminator",
        "evidenceSources": ["authoritative source label"]
      }}
    }},
    "authoritativeSources": [{{"label": "string", "content": "string"}}],
    "constraints": ["string"],
    "rejectionCriteria": ["string"],
    "outputProtocol": "string"
  }},
  "tasks": [
    {{
      "id": "lowercase-id",
      "role": "implementation|test|review|general",
      "prompt": "string",
      "dependsOn": ["task-id"],
{task_workspace_fields}
      "maxRepairAttempts": 0,
      "outputProtocol": "string",
      "generationOverride": {{
        "temperature": 0.0,
        "top_p": 1.0,
        "max_tokens": {min(800, config.worker.capabilities.max_generation_tokens)}
      }},
      "gate": {{
        "requiredPatterns": [{{"id": "rule-id", "pattern": "regex"}}],
        "forbiddenPatterns": [{{"id": "rule-id", "pattern": "regex"}}],
        "maxCharacters": 20000,
        "format": "text|json",
        "stripSingleCodeFence": true,
        "pythonSyntax": false,
        "jsonRequiredKeys": [],
        "jsonAllowedKeys": [],
        "jsonFieldEnums": {{}}
      }}
    }}
  ]
}}
"""


def _worker_capability_contract(config: SwarmConfig) -> str:
    capability = worker_capabilities_payload(config.worker.capabilities)
    calibration = capability["calibration"]
    strengths = "\n".join(
        f"- {item}" for item in capability["strengths"]
    ) or "- No strengths have been measured."
    limitations = "\n".join(
        f"- {item}" for item in capability["limitations"]
    ) or "- Capability is unmeasured; assume limited independent diagnosis."
    delegation_rules = {
        "exact-edit": (
            "- You own the causal diagnosis and edit design.\n"
            "- If you already know the literal old and new bytes, use "
            "executionMode deterministic-edit; do not ask MLX to copy them.\n"
            "- Delegate one mechanical, bounded source transformation per "
            "mutating task.\n"
            "- Name exact files and symbols, include exact source anchors, "
            "and state the precise old-to-new transformation. Do not "
            "delegate discovery, architecture, API inference, or choosing "
            "among fixes to the local worker."
        ),
        "bounded-implementation": (
            "- You own and validate the causal diagnosis.\n"
            "- Delegate only a bounded implementation with named files, "
            "interfaces, invariants, and falsifiable acceptance conditions.\n"
            "- Split broad changes so each worker has one locally checkable "
            "responsibility."
        ),
        "autonomous": (
            "- Workers may investigate within the exact supplied "
            "authoritative sources, but still cannot inspect arbitrary files "
            "or run commands.\n"
            "- Preserve bounded tasks and deterministic gates."
        ),
    }[capability["delegationLevel"]]
    return f"""
WORKER CAPABILITY CONTRACT
- This describes local generation capability, not worker concurrency.
- model repository: {config.model.repository}
- model revision: {config.model.revision or "(unreported)"}
- parameter scale: {capability["parameterScale"]}
- model context window: \
{capability["contextWindowTokens"] or "(unreported)"} tokens
- maximum generation per worker: {capability["maxGenerationTokens"]} tokens
- specialization: {capability["specialization"]}
- delegation level: {capability["delegationLevel"]}
- generation mode: {config.worker.mode}
- reasoning-stage token ceiling: {config.worker.reasoning_max_tokens}
- prompt ceiling: {min(config.batch.max_prompt_characters, MAX_PROMPT_CHARS)} characters
- local workers cannot inspect the workspace, call tools, run verification,
  invent commands, or recover missing source context.
- calibration: {calibration["status"]} \
({calibration["passedCases"]}/{calibration["totalCases"]} passed; \
evidence SHA-256: {calibration["evidenceSha256"] or "(none)"})
Observed strengths:
{strengths}
Observed limitations:
{limitations}
Delegation policy:
{delegation_rules}
- The plan is authoritative. Do not ask the local model to validate or replace
  your diagnosis; give it the minimum exact edit it must render.
"""


def build_review_prompt(frontier_result: dict[str, Any]) -> str:
    """Build the deterministic final-review prompt from one result packet."""
    packet = json.dumps(
        frontier_result,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return f"""You are the final frontier reviewer for MLX Swarm.

Review only the completed frontier-result.json packet below. Do not request
another worker wave and do not propose mutating this completed session.
Return exactly one JSON object with this strict shape:

{{
  "schemaVersion": 1,
  "sessionId": "{frontier_result.get("sessionId", "")}",
  "planId": "{frontier_result.get("planId", "")}",
  "verdict": "approved|changes_requested|rejected",
  "summary": "concise final assessment",
  "findings": [
    {{
      "id": "lowercase-id",
      "severity": "critical|high|medium|low",
      "taskId": "optional-task-id",
      "title": "short title",
      "evidence": "specific packet evidence",
      "recommendation": "action for a separately approved follow-up"
    }}
  ]
}}

FRONTIER RESULT
{packet}
"""


class CommanderStore:
    """Filesystem-backed commander request and frontier-review ledger."""

    def __init__(self, config: SwarmConfig):
        self.config = config
        self.artifacts_root = config.artifacts_dir.resolve()
        if config.workspace is not None:
            from .workspace import discover_git_root

            self.workspace_root = discover_git_root(config.source.parent)
        else:
            self.workspace_root = config.source.parent.resolve()
        self.requests_root = (
            self.artifacts_root / "_commander" / "requests"
        ).resolve()
        self.requests_root.mkdir(parents=True, exist_ok=True)

    def create_request(
        self,
        objective: str,
        constraints: Iterable[str] = (),
        *,
        revision_of: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        objective = _text(objective, "objective", MAX_OBJECTIVE_CHARS)
        constraint_values = tuple(constraints)
        if len(constraint_values) > MAX_CONSTRAINTS:
            raise CommanderError(
                f"constraints cannot exceed {MAX_CONSTRAINTS} entries."
            )
        normalized_constraints = [
            _text(value, f"constraints[{index}]", MAX_CONSTRAINT_CHARS)
            for index, value in enumerate(constraint_values)
        ]
        if revision_of is not None:
            revision_of = _lineage(revision_of, "revisionOf")
        request_id = (
            _identifier(request_id, "requestId")
            if request_id is not None
            else _request_id()
        )
        predecessor = (
            self._revision_predecessor(revision_of)
            if revision_of is not None
            else None
        )
        if predecessor is not None:
            existing_successor = predecessor[1].get(
                "supersededByRequestId"
            )
            if (
                isinstance(existing_successor, str)
                and existing_successor != request_id
            ):
                raise CommanderError(
                    "Revision predecessor is already superseded by request "
                    f"{existing_successor}."
                )
        request_dir = self._request_dir(request_id)
        try:
            request_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise CommanderError(f"Commander request already exists: {request_id}") from exc
        now = utc_now()
        request: dict[str, Any] = {
            "schemaVersion": COMMANDER_SCHEMA_VERSION,
            "requestId": request_id,
            "objective": objective,
            "constraints": normalized_constraints,
            "workspaceRoot": str(self.workspace_root),
            "status": "awaiting_plan",
            "createdAt": now,
            "updatedAt": now,
            "planPhase": {"status": "open"},
        }
        if revision_of is not None:
            request["revisionOf"] = revision_of
        _atomic_json(request_dir / "request.json", request)
        _atomic_text(
            request_dir / "plan-prompt.txt",
            build_plan_prompt(request, self.config),
        )
        if predecessor is not None:
            self._supersede_revision_predecessor(
                predecessor,
                successor_request_id=request_id,
            )
        return self.request_detail(request_id)

    def _revision_predecessor(
        self,
        revision_of: str,
    ) -> tuple[Path, dict[str, Any]] | None:
        plan_id, session_id = revision_of.split("/", 1)
        session_dir = (
            self.artifacts_root / plan_id / session_id
        ).resolve()
        if not _is_within(session_dir, self.artifacts_root):
            raise CommanderError("Revision predecessor escapes artifacts.")
        state_path = session_dir / "session.json"
        if not state_path.is_file():
            return None
        state = _required_json(state_path)
        if (
            state.get("planId") != plan_id
            or state.get("sessionId") != session_id
        ):
            raise CommanderError("Revision predecessor identity mismatch.")
        return session_dir, state

    def _supersede_revision_predecessor(
        self,
        predecessor: tuple[Path, dict[str, Any]],
        *,
        successor_request_id: str,
    ) -> None:
        """Retain predecessor evidence while releasing only a safe checkout lease."""
        session_dir, state = predecessor
        state["supersededByRequestId"] = successor_request_id
        state["supersededAt"] = utc_now()
        snapshot_path = session_dir / "workspace.snapshot.json"
        if snapshot_path.is_file():
            from .workspace import (
                WorkspaceError,
                checkout_lease,
                load_workspace_snapshot,
                release_checkout_lease,
            )

            try:
                workspace = load_workspace_snapshot(session_dir)
                policy = workspace.get("executionPolicy")
                if (
                    isinstance(policy, dict)
                    and policy.get("workspaceTarget") == "checkout"
                ):
                    tasks = state.get("tasks", {})
                    unresolved = any(
                        isinstance(task, dict)
                        and task.get("status") in {
                            "applying",
                            "verifying",
                            "verification_failed",
                        }
                        for task in tasks.values()
                    )
                    for task_id, task in tasks.items():
                        if not isinstance(task, dict):
                            unresolved = True
                            continue
                        artifact_dir = (
                            session_dir / "artifacts" / str(task_id)
                        )
                        if (
                            (artifact_dir / "apply-receipt.json").is_file()
                            and not (
                                artifact_dir / "revert-receipt.json"
                            ).is_file()
                            and task.get("status") != "completed"
                        ):
                            unresolved = True
                    lease = checkout_lease(
                        Path(workspace["workspaceRoot"])
                    )
                    owns_lease = (
                        isinstance(lease, dict)
                        and lease.get("planId") == state.get("planId")
                        and lease.get("sessionId") == state.get("sessionId")
                    )
                    if owns_lease and not unresolved:
                        release_checkout_lease(
                            workspace,
                            plan_id=str(state["planId"]),
                            session_id=str(state["sessionId"]),
                        )
                        state["checkoutLeaseReleasedAt"] = utc_now()
                        state["checkoutLeaseReleaseReason"] = "superseded"
                        state["supersessionLeaseStatus"] = "released"
                    elif owns_lease:
                        state["supersessionLeaseStatus"] = (
                            "blocked_unresolved_workspace"
                        )
                    else:
                        state["supersessionLeaseStatus"] = "not_owned"
            except WorkspaceError as exc:
                state["supersessionLeaseStatus"] = "validation_failed"
                state["supersessionLeaseError"] = str(exc)
        _atomic_json(session_dir / "session.json", state)

    def list_requests(self) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        for path in self.requests_root.glob("*/request.json"):
            try:
                request = self._load_request_file(path)
            except (CommanderError, OSError, ValueError):
                continue
            requests.append(self._request_summary(request))
        requests.sort(
            key=lambda value: value.get("createdAt") or "",
            reverse=True,
        )
        return requests

    def request_detail(self, request_id: str) -> dict[str, Any]:
        request_dir, request = self._load_request(request_id)
        plan_payload = None
        plan_path = request_dir / "plan.validated.json"
        if plan_path.is_file():
            plan = load_plan(plan_path, self.config)
            plan_payload = {
                **plan.raw,
                "source": str(plan_path),
                "digest": request.get("planDigest"),
                "levels": [
                    [task.id for task in level]
                    for level in plan.topological_order()
                ],
            }
        execution_preview = None
        execution_previews = None
        execution_error = None
        if plan_payload is not None and plan.workspace_execution:
            from .workspace import (
                WorkspaceError,
                execution_policy,
                execution_preview as preview,
                execution_previews as previews,
            )

            approval = request.get("approval")
            if request.get("status") == "launched" and isinstance(
                approval,
                dict,
            ):
                mode = approval.get("approvalMode", "supervised")
                target = approval.get("workspaceTarget", "worktree")
                execution_preview = {
                    "ready": True,
                    "executionDigest": approval.get("executionDigest"),
                    "workspaceRoot": approval.get("workspaceRoot"),
                    "baseSha": approval.get("baseSha"),
                    "executionPolicySha256": approval.get(
                        "executionPolicySha256"
                    ),
                    "executionPolicy": execution_policy(
                        approval_mode=str(mode),
                        workspace_target=str(target),
                    ),
                    "historical": True,
                }
                execution_previews = {
                    str(mode): {str(target): execution_preview}
                }
            else:
                try:
                    execution_preview = preview(self.config, plan)
                    execution_previews = previews(self.config, plan)
                except WorkspaceError as exc:
                    execution_error = str(exc)
        return {
            "request": request,
            "plan": plan_payload,
            "planningReceipt": _optional_json(
                request_dir / "frontier-plan-receipt.json"
            ),
            "planningAttemptReceipt": _optional_json(
                request_dir / "frontier-plan-attempt-receipt.json"
            ),
            "validationError": _optional_json(request_dir / "plan.error.json"),
            "executionPreview": execution_preview,
            "executionPreviews": execution_previews,
            "executionError": execution_error,
            "planPrompt": str(request_dir / "plan-prompt.txt"),
            "handoff": {
                "planCommand": (
                    "Use $mlx-swarm-commander to plan commander request "
                    f"{request_id} with config {self.config.source}"
                ),
            },
        }

    def claim_plan(
        self,
        request_id: str,
        *,
        adapter: str = "codex-skill",
    ) -> dict[str, Any]:
        request_dir, request = self._load_request(request_id)
        if request["status"] != "awaiting_plan":
            raise CommanderError(
                f"Planning phase is already {request['planPhase']['status']}."
            )
        claim = self._create_claim(
            request_dir / "frontier-plan.claim.json",
            phase="plan",
            owner_id=request_id,
            adapter=adapter,
        )
        request["planPhase"] = {
            "status": "claimed",
            "claimId": claim["claimId"],
            "claimedAt": claim["claimedAt"],
            "adapter": claim["adapter"],
        }
        request["updatedAt"] = utc_now()
        _atomic_json(request_dir / "request.json", request)
        return {
            **claim,
            "requestId": request_id,
            "promptPath": str(request_dir / "plan-prompt.txt"),
            "workspaceRoot": str(self.workspace_root),
        }

    def release_plan_claim(self, request_id: str, claim_id: str) -> None:
        request_dir, request = self._load_request(request_id)
        claim_path = request_dir / "frontier-plan.claim.json"
        claim = _required_json(claim_path)
        self._require_claim(claim, claim_id, "plan")
        if (request_dir / "frontier-plan.raw.txt").exists():
            raise CommanderError(
                "The planning response was already recorded and cannot be released."
            )
        claim_path.unlink()
        request["planPhase"] = {"status": "open"}
        request["updatedAt"] = utc_now()
        _atomic_json(request_dir / "request.json", request)

    def import_plan(
        self,
        request_id: str,
        response_path: Path,
        *,
        claim_id: str,
        adapter: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> dict[str, Any]:
        request_dir, request = self._load_request(request_id)
        if request["status"] != "awaiting_plan":
            raise CommanderError("Planning response has already been recorded.")
        claim = _required_json(request_dir / "frontier-plan.claim.json")
        self._require_claim(claim, claim_id, "plan")
        validated_plan_path = request_dir / "plan.validated.json"
        planning_receipt_path = (
            request_dir / "frontier-plan-receipt.json"
        )
        existing_plan = _optional_json(validated_plan_path)
        existing_receipt = _optional_json(planning_receipt_path)
        if existing_receipt is not None:
            if existing_plan is None:
                raise CommanderError(
                    "Frontier planning receipt exists without its plan."
                )
            plan_digest = canonical_json_sha256(existing_plan)
            existing_receipt = validate_frontier_receipt(
                existing_receipt,
                expected_phase="plan",
                expected_artifact_sha256=plan_digest,
            )
            request["status"] = "plan_ready"
            request["planDigest"] = plan_digest
            request["planPhase"] = {
                **request["planPhase"],
                "status": "accepted",
                "acceptedAt": existing_receipt["acceptedAt"],
                "artifactSha256": plan_digest,
            }
            request["updatedAt"] = utc_now()
            _atomic_json(request_dir / "request.json", request)
            return self.request_detail(request_id)
        usage = frontier_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
        normalized_provider = _optional_text(provider, "provider", 200)
        normalized_model = _optional_text(model, "model", 300)
        normalized_adapter = _text(
            adapter or claim["adapter"],
            "adapter",
            100,
        )
        response = ""
        raw_response_path = request_dir / "frontier-plan.raw.txt"
        request["updatedAt"] = utc_now()
        try:
            response = _capture_frontier_response(
                raw_response_path,
                response_path,
            )
            raw_plan = parse_json_response(response)
            if not isinstance(raw_plan, dict):
                raise CommanderError("Frontier plan response must be a JSON object.")
            candidate = request_dir / "plan.validated.json.tmp"
            candidate.write_text(
                json.dumps(raw_plan, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                plan = load_plan(candidate, self.config)
                if (
                    self.config.workspace is not None
                    and not plan.workspace_execution
                ):
                    raise CommanderError(
                        "Workspace-enabled commander requests require "
                        "plan schema version 2."
                    )
                if plan.context is None or plan.context.diagnosis is None:
                    raise CommanderError(
                        "Commander plans require an evidence-backed "
                        "context.diagnosis produced during the planning call."
                    )
                if plan.context.diagnosis.change_validation is None:
                    raise CommanderError(
                        "Commander plans require "
                        "context.diagnosis.changeValidation covering the "
                        "candidate edit, failing path, preserved control, and "
                        "minimality during the same planning call."
                    )
            finally:
                if candidate.exists() and candidate.name.endswith(".tmp"):
                    candidate.unlink()
        except (CommanderError, ContractError, OSError, ValueError) as exc:
            if (
                not raw_response_path.exists()
                and not raw_response_path.is_symlink()
            ):
                _exclusive_text(raw_response_path, response)
            attempt = _invalid_frontier_attempt_receipt(
                phase="plan",
                adapter=normalized_adapter,
                provider=normalized_provider,
                model=normalized_model,
                response=response,
                usage=usage,
            )
            _atomic_json(
                request_dir / "frontier-plan-attempt-receipt.json",
                attempt,
            )
            error = {
                "schemaVersion": COMMANDER_SCHEMA_VERSION,
                "phase": "plan",
                "error": str(exc),
                "recordedAt": utc_now(),
            }
            _atomic_json(request_dir / "plan.error.json", error)
            request["status"] = "plan_invalid"
            request["planPhase"] = {
                **request["planPhase"],
                "status": "invalid",
                "recordedAt": error["recordedAt"],
            }
            _atomic_json(request_dir / "request.json", request)
            raise CommanderError(
                f"Frontier plan is invalid and this request is sealed: {exc}"
            ) from exc
        canonical_plan = plan.raw
        plan_digest = canonical_json_sha256(canonical_plan)
        if (
            existing_plan is not None
            and existing_plan != canonical_plan
        ):
            raise CommanderError(
                "The imported plan differs from the durable validated plan "
                "recorded before interruption."
            )
        if existing_plan is None:
            _atomic_json(validated_plan_path, canonical_plan)
        receipt = FrontierReceipt(
            phase="plan",
            adapter=normalized_adapter,
            provider=normalized_provider,
            model=normalized_model,
            artifact_sha256=plan_digest,
            accepted_at=utc_now(),
            usage=usage,
        ).to_json()
        _atomic_json(planning_receipt_path, receipt)
        request["status"] = "plan_ready"
        request["planDigest"] = plan_digest
        request["planPhase"] = {
            **request["planPhase"],
            "status": "accepted",
            "acceptedAt": receipt["acceptedAt"],
            "artifactSha256": plan_digest,
        }
        request["updatedAt"] = utc_now()
        _atomic_json(request_dir / "request.json", request)
        return self.request_detail(request_id)

    def approved_plan(
        self,
        request_id: str,
        plan_digest: str,
        *,
        source: str = "cockpit",
        execution_digest: str | None = None,
        approval_mode: str = "supervised",
        workspace_target: str = "worktree",
    ) -> tuple[Plan, Path, PlanApproval, dict[str, Any], dict[str, Any]]:
        request_dir, request = self._load_request(request_id)
        if request["status"] != "plan_ready":
            raise CommanderError("Commander request does not have a valid plan.")
        expected = request.get("planDigest")
        if not isinstance(plan_digest, str) or plan_digest != expected:
            raise CommanderError(
                "Plan digest mismatch; refresh and approve the displayed plan."
            )
        plan_path = request_dir / "plan.validated.json"
        plan = load_plan(plan_path, self.config)
        actual = canonical_json_sha256(plan.raw)
        if actual != expected:
            raise CommanderError("Validated plan changed after frontier import.")
        workspace_root = None
        base_sha = None
        if plan.workspace_execution:
            from .workspace import WorkspaceError, execution_preview

            try:
                preview = execution_preview(
                    self.config,
                    plan,
                    approval_mode=approval_mode,
                    workspace_target=workspace_target,
                )
            except WorkspaceError as exc:
                raise CommanderError(str(exc)) from exc
            if execution_digest != preview["executionDigest"]:
                raise CommanderError(
                    "Execution digest mismatch; refresh the workspace preview."
                )
            workspace_root = preview["workspaceRoot"]
            base_sha = preview["baseSha"]
        approval = PlanApproval(
            request_id=request_id,
            plan_sha256=actual,
            approved_at=utc_now(),
            source=source,
            execution_digest=execution_digest,
            workspace_root=workspace_root,
            base_sha=base_sha,
            approval_mode=(
                approval_mode if plan.workspace_execution else None
            ),
            workspace_target=(
                workspace_target if plan.workspace_execution else None
            ),
            execution_policy_sha256=(
                preview["executionPolicySha256"]
                if plan.workspace_execution
                else None
            ),
        )
        receipt = _required_json(request_dir / "frontier-plan-receipt.json")
        return plan, plan_path, approval, receipt, request

    def mark_launched(
        self,
        request_id: str,
        approval: PlanApproval,
        *,
        plan_id: str,
        session_id: str,
    ) -> None:
        request_dir, request = self._load_request(request_id)
        if request["status"] == "launched":
            raise CommanderError("Commander request has already been launched.")
        if request["status"] != "plan_ready":
            raise CommanderError("Commander request is not ready to launch.")
        request["status"] = "launched"
        request["approval"] = approval.to_json()
        request["sessionRef"] = f"{plan_id}/{session_id}"
        request["updatedAt"] = utc_now()
        _atomic_json(request_dir / "request.json", request)

    def claim_review(
        self,
        session_dir: Path,
        *,
        adapter: str = "codex-skill",
    ) -> dict[str, Any]:
        session_dir, state = self._load_session(session_dir)
        if state.get("status") != "completed":
            raise CommanderError(
                "Only completed local runs are eligible for frontier review."
            )
        if (session_dir / "frontier-review.json").exists():
            raise CommanderError("Frontier review has already been accepted.")
        if state.get("reviewStatus") == "review_error":
            raise CommanderError(
                "This review phase is sealed after an invalid response."
            )
        result_path = session_dir / "frontier-result.json"
        result = _required_json(result_path)
        result_digest = canonical_json_sha256(result)
        prompt_path = session_dir / "frontier-review-prompt.txt"
        expected_prompt = build_review_prompt(result)
        if prompt_path.exists():
            if prompt_path.read_text(encoding="utf-8") != expected_prompt:
                raise CommanderError(
                    "Existing review prompt differs from frontier-result.json."
                )
        else:
            _atomic_text(prompt_path, expected_prompt)
        claim = self._create_claim(
            session_dir / "frontier-review.claim.json",
            phase="review",
            owner_id=f"{state['planId']}/{state['sessionId']}",
            adapter=adapter,
            input_artifact_sha256=result_digest,
        )
        state["reviewStatus"] = "review_claimed"
        state["reviewClaim"] = {
            "claimId": claim["claimId"],
            "claimedAt": claim["claimedAt"],
            "adapter": claim["adapter"],
        }
        _atomic_json(session_dir / "session.json", state)
        return {
            **claim,
            "planId": state["planId"],
            "sessionId": state["sessionId"],
            "promptPath": str(prompt_path),
            "frontierResult": str(result_path),
            "inputArtifactSha256": result_digest,
        }

    def release_review_claim(self, session_dir: Path, claim_id: str) -> None:
        session_dir, state = self._load_session(session_dir)
        claim_path = session_dir / "frontier-review.claim.json"
        claim = _required_json(claim_path)
        self._require_claim(claim, claim_id, "review")
        if (session_dir / "frontier-review.raw.txt").exists():
            raise CommanderError(
                "The review response was already recorded and cannot be released."
            )
        claim_path.unlink()
        state["reviewStatus"] = "awaiting_review"
        state.pop("reviewClaim", None)
        _atomic_json(session_dir / "session.json", state)

    def import_review(
        self,
        session_dir: Path,
        response_path: Path,
        *,
        claim_id: str,
        adapter: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> dict[str, Any]:
        session_dir, state = self._load_session(session_dir)
        if state.get("status") != "completed":
            raise CommanderError(
                "Only completed local runs are eligible for frontier review."
            )
        claim = _required_json(session_dir / "frontier-review.claim.json")
        self._require_claim(claim, claim_id, "review")
        review_path = session_dir / "frontier-review.json"
        receipt_path = session_dir / "frontier-review-receipt.json"
        existing_review = _optional_json(review_path)
        existing_receipt = _optional_json(receipt_path)
        if existing_receipt is not None:
            if existing_review is None:
                raise CommanderError(
                    "Frontier review receipt exists without its artifact."
                )
            review_input = _required_json(
                session_dir / "frontier-result.json"
            )
            input_digest = canonical_json_sha256(review_input)
            review_digest = canonical_json_sha256(existing_review)
            existing_receipt = validate_frontier_receipt(
                existing_receipt,
                expected_phase="review",
                expected_artifact_sha256=review_digest,
                expected_input_artifact_sha256=input_digest,
            )
            recovered_review = parse_frontier_review(
                existing_review,
                expected_plan_id=state["planId"],
                expected_session_id=state["sessionId"],
            )
            state["reviewStatus"] = recovered_review.verdict
            state["frontierReview"] = "frontier-review.json"
            state["frontierReviewReceipt"] = (
                "frontier-review-receipt.json"
            )
            state.pop("reviewError", None)
            _atomic_json(session_dir / "session.json", state)
            usage_payload = write_frontier_usage(session_dir)
            return {
                "review": existing_review,
                "receipt": existing_receipt,
                "frontierUsage": usage_payload,
            }
        usage = frontier_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
        normalized_provider = _optional_text(provider, "provider", 200)
        normalized_model = _optional_text(model, "model", 300)
        normalized_adapter = _text(
            adapter or claim["adapter"],
            "adapter",
            100,
        )
        response = ""
        raw_response_path = session_dir / "frontier-review.raw.txt"
        claimed_input_digest = claim.get("inputArtifactSha256")
        review_input_digest = (
            claimed_input_digest
            if isinstance(claimed_input_digest, str)
            else None
        )
        try:
            response = _capture_frontier_response(
                raw_response_path,
                response_path,
            )
            review_input = _required_json(
                session_dir / "frontier-result.json"
            )
            review_input_digest = canonical_json_sha256(review_input)
            if claim.get("inputArtifactSha256") != review_input_digest:
                raise CommanderError(
                    "frontier-result.json changed after the review claim."
                )
            raw_review = parse_json_response(response)
            review = parse_frontier_review(
                raw_review,
                expected_plan_id=state["planId"],
                expected_session_id=state["sessionId"],
            )
            review_payload = review.to_json()
            if (
                existing_review is not None
                and existing_review != review_payload
            ):
                raise CommanderError(
                    "The imported review differs from the durable review "
                    "artifact recorded before interruption."
                )
        except (CommanderError, ValueError) as exc:
            if (
                not raw_response_path.exists()
                and not raw_response_path.is_symlink()
            ):
                _exclusive_text(raw_response_path, response)
            attempt = _invalid_frontier_attempt_receipt(
                phase="review",
                adapter=normalized_adapter,
                provider=normalized_provider,
                model=normalized_model,
                response=response,
                usage=usage,
                input_artifact_sha256=(
                    claimed_input_digest or review_input_digest
                ),
            )
            _atomic_json(
                session_dir / "frontier-review-attempt-receipt.json",
                attempt,
            )
            error = {
                "schemaVersion": COMMANDER_SCHEMA_VERSION,
                "phase": "review",
                "error": str(exc),
                "recordedAt": utc_now(),
            }
            _atomic_json(session_dir / "frontier-review.error.json", error)
            state["reviewStatus"] = "review_error"
            state["reviewError"] = error
            _atomic_json(session_dir / "session.json", state)
            write_frontier_usage(session_dir)
            raise CommanderError(
                f"Frontier review is invalid and this phase is sealed: {exc}"
            ) from exc
        review_digest = canonical_json_sha256(review_payload)
        if existing_review is None:
            _atomic_json(review_path, review_payload)
        receipt = FrontierReceipt(
            phase="review",
            adapter=normalized_adapter,
            provider=normalized_provider,
            model=normalized_model,
            artifact_sha256=review_digest,
            accepted_at=utc_now(),
            usage=usage,
            input_artifact_sha256=review_input_digest,
        ).to_json()
        _atomic_json(receipt_path, receipt)
        state["reviewStatus"] = review.verdict
        state["frontierReview"] = "frontier-review.json"
        state["frontierReviewReceipt"] = "frontier-review-receipt.json"
        state.pop("reviewError", None)
        _atomic_json(session_dir / "session.json", state)
        write_frontier_usage(session_dir)
        return {
            "review": review_payload,
            "receipt": receipt,
            "frontierUsage": _required_json(
                session_dir / "frontier-usage.json"
            ),
        }

    def review_detail(self, session_dir: Path) -> dict[str, Any]:
        session_dir, state = self._load_session(session_dir)
        frontier_usage_payload = write_frontier_usage(session_dir)
        return {
            "reviewStatus": state.get(
                "reviewStatus",
                (
                    "awaiting_review"
                    if state.get("status") == "completed"
                    else "not_eligible"
                ),
            ),
            "review": _optional_json(session_dir / "frontier-review.json"),
            "reviewReceipt": _optional_json(
                session_dir / "frontier-review-receipt.json"
            ),
            "reviewAttemptReceipt": _optional_json(
                session_dir / "frontier-review-attempt-receipt.json"
            ),
            "frontierUsage": frontier_usage_payload,
            "reviewError": _optional_json(
                session_dir / "frontier-review.error.json"
            ),
            "handoff": {
                "reviewCommand": (
                    "Use $mlx-swarm-commander to review completed run "
                    f"{state.get('planId')}/{state.get('sessionId')} "
                    f"with config {self.config.source}"
                )
            },
        }

    def _request_summary(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "requestId": request["requestId"],
            "objective": request["objective"],
            "status": request["status"],
            "createdAt": request["createdAt"],
            "updatedAt": request["updatedAt"],
            "planDigest": request.get("planDigest"),
            "sessionRef": request.get("sessionRef"),
            "revisionOf": request.get("revisionOf"),
        }

    def _request_dir(self, request_id: str) -> Path:
        request_id = _identifier(request_id, "requestId")
        path = (self.requests_root / request_id).resolve()
        if not _is_within(path, self.requests_root):
            raise CommanderError("Commander request path escapes the artifact root.")
        return path

    def _load_request(
        self,
        request_id: str,
    ) -> tuple[Path, dict[str, Any]]:
        request_dir = self._request_dir(request_id)
        path = request_dir / "request.json"
        if not path.is_file():
            raise CommanderError(f"Commander request not found: {request_id}")
        request = self._load_request_file(path)
        if request["requestId"] != request_id:
            raise CommanderError("Commander request identity mismatch.")
        return request_dir, request

    def _load_request_file(self, path: Path) -> dict[str, Any]:
        request = _required_json(path)
        _exact_keys(
            request,
            "request",
            {
                "schemaVersion",
                "requestId",
                "objective",
                "constraints",
                "workspaceRoot",
                "status",
                "createdAt",
                "updatedAt",
                "planPhase",
            },
            {
                "revisionOf",
                "planDigest",
                "approval",
                "sessionRef",
            },
        )
        if request["schemaVersion"] != COMMANDER_SCHEMA_VERSION:
            raise CommanderError("Unsupported commander request schema version.")
        _identifier(request["requestId"], "request.requestId")
        _text(request["objective"], "request.objective", MAX_OBJECTIVE_CHARS)
        if not isinstance(request["constraints"], list):
            raise CommanderError("request.constraints must be an array.")
        if len(request["constraints"]) > MAX_CONSTRAINTS:
            raise CommanderError("request.constraints has too many entries.")
        for index, value in enumerate(request["constraints"]):
            _text(value, f"request.constraints[{index}]", MAX_CONSTRAINT_CHARS)
        workspace_root = Path(
            _text(request["workspaceRoot"], "request.workspaceRoot", 10_000)
        ).resolve()
        if workspace_root != self.workspace_root:
            raise CommanderError(
                "Commander request workspace differs from the config directory."
            )
        if request["status"] not in {
            "awaiting_plan",
            "plan_invalid",
            "plan_ready",
            "launched",
        }:
            raise CommanderError("Invalid commander request status.")
        phase = _object(request["planPhase"], "request.planPhase")
        phase_status = phase.get("status")
        if phase_status not in PHASE_STATUSES:
            raise CommanderError("Invalid commander planning phase status.")
        phase_required = {"status"}
        phase_optional: set[str] = set()
        if phase_status in {"claimed", "accepted", "invalid"}:
            phase_optional.update(
                {
                    "claimId",
                    "claimedAt",
                    "adapter",
                    "acceptedAt",
                    "artifactSha256",
                    "recordedAt",
                }
            )
        _exact_keys(
            phase,
            "request.planPhase",
            phase_required,
            phase_optional,
        )
        expected_phase = {
            "awaiting_plan": {"open", "claimed"},
            "plan_invalid": {"invalid"},
            "plan_ready": {"accepted"},
            "launched": {"accepted"},
        }[request["status"]]
        if phase_status not in expected_phase:
            raise CommanderError(
                "Commander request status and planning phase disagree."
            )
        for key in ("claimId", "claimedAt", "adapter", "acceptedAt", "recordedAt"):
            if key in phase:
                _text(phase[key], f"request.planPhase.{key}", 300)
        if "artifactSha256" in phase:
            _sha256(phase["artifactSha256"], "request.planPhase.artifactSha256")
        if "revisionOf" in request:
            _lineage(request["revisionOf"], "request.revisionOf")
        if request["status"] in {"plan_ready", "launched"}:
            _sha256(request.get("planDigest"), "request.planDigest")
        if request["status"] == "launched":
            approval = _object(request.get("approval"), "request.approval")
            _parse_approval(approval)
            _lineage(request.get("sessionRef"), "request.sessionRef")
        return request

    def _load_session(
        self,
        session_dir: Path,
    ) -> tuple[Path, dict[str, Any]]:
        resolved = session_dir.resolve()
        if not _is_within(resolved, self.artifacts_root):
            raise CommanderError("Session path escapes the artifact root.")
        path = resolved / "session.json"
        if not path.is_file():
            raise CommanderError("Session not found.")
        state = _required_json(path)
        plan_id = _identifier(state.get("planId"), "session.planId")
        session_id = _identifier(state.get("sessionId"), "session.sessionId")
        expected = (self.artifacts_root / plan_id / session_id).resolve()
        if expected != resolved:
            raise CommanderError("Session directory identity mismatch.")
        return resolved, state

    def _create_claim(
        self,
        path: Path,
        *,
        phase: str,
        owner_id: str,
        adapter: str,
        input_artifact_sha256: str | None = None,
    ) -> dict[str, Any]:
        adapter = _text(adapter, "adapter", 100)
        claim = {
            "schemaVersion": COMMANDER_SCHEMA_VERSION,
            "claimId": uuid.uuid4().hex,
            "phase": phase,
            "ownerId": owner_id,
            "adapter": adapter,
            "claimedAt": utc_now(),
        }
        if input_artifact_sha256 is not None:
            claim["inputArtifactSha256"] = _sha256(
                input_artifact_sha256,
                "inputArtifactSha256",
            )
        _exclusive_text(
            path,
            json.dumps(claim, ensure_ascii=False, indent=2) + "\n",
            exists_message=f"The {phase} phase is already claimed.",
        )
        return claim

    @staticmethod
    def _require_claim(
        claim: dict[str, Any],
        claim_id: str,
        phase: str,
    ) -> None:
        if (
            claim.get("phase") != phase
            or claim.get("claimId") != claim_id
        ):
            raise CommanderError(f"Invalid {phase} claim.")


def write_frontier_usage(session_dir: Path) -> dict[str, Any]:
    """Persist separate planning, review, and completeness-aware totals."""
    plan_receipt = _optional_json(
        session_dir / "frontier-plan-receipt.json"
    )
    if plan_receipt is not None:
        plan_path = session_dir / "plan.snapshot.json"
        expected_plan_digest = canonical_json_sha256(
            _required_json(plan_path)
        )
        plan_receipt = validate_frontier_receipt(
            plan_receipt,
            expected_phase="plan",
            expected_artifact_sha256=expected_plan_digest,
        )
    review_receipt = _optional_json(
        session_dir / "frontier-review-receipt.json"
    )
    review_is_attempt = False
    if review_receipt is None:
        review_receipt = _optional_json(
            session_dir / "frontier-review-attempt-receipt.json"
        )
        review_is_attempt = review_receipt is not None
    if review_receipt is not None:
        expected_review_digest = None
        expected_response_digest = None
        if not review_is_attempt:
            review_path = session_dir / "frontier-review.json"
            expected_review_digest = canonical_json_sha256(
                _required_json(review_path)
            )
        expected_input_digest = None
        if review_is_attempt:
            claim = _required_json(
                session_dir / "frontier-review.claim.json"
            )
            expected_input_digest = claim.get(
                "inputArtifactSha256"
            )
            raw_response = _read_bounded_text(
                session_dir / "frontier-review.raw.txt"
            )
            expected_response_digest = hashlib.sha256(
                raw_response.encode("utf-8")
            ).hexdigest()
        else:
            result_path = session_dir / "frontier-result.json"
            expected_input_digest = canonical_json_sha256(
                _required_json(result_path)
            )
        review_receipt = validate_frontier_receipt(
            review_receipt,
            expected_phase="review",
            expected_artifact_sha256=expected_review_digest,
            expected_input_artifact_sha256=expected_input_digest,
            expected_response_sha256=expected_response_digest,
            expected_outcome=(
                "invalid" if review_is_attempt else "accepted"
            ),
        )
    planning = _usage_phase(plan_receipt, pending=False)
    review = _usage_phase(review_receipt, pending=review_receipt is None)
    phases = [
        phase
        for phase in (planning, review)
        if phase["attemptedResponses"]
    ]
    all_reported = bool(phases) and all(
        phase["usageStatus"] == "reported" for phase in phases
    )
    totals = {
        "acceptedResponses": sum(
            phase["acceptedResponses"] for phase in (planning, review)
        ),
        "attemptedResponses": sum(
            phase["attemptedResponses"] for phase in (planning, review)
        ),
        "usageStatus": "reported" if all_reported else "unavailable",
        "promptTokens": (
            sum(int(phase["promptTokens"]) for phase in phases)
            if all_reported
            else None
        ),
        "completionTokens": (
            sum(int(phase["completionTokens"]) for phase in phases)
            if all_reported
            else None
        ),
        "totalTokens": (
            sum(int(phase["totalTokens"]) for phase in phases)
            if all_reported
            else None
        ),
    }
    payload = {
        "schemaVersion": COMMANDER_SCHEMA_VERSION,
        "planning": planning,
        "review": review,
        "total": totals,
    }
    _atomic_json(session_dir / "frontier-usage.json", payload)
    return payload


def validate_frontier_receipt(
    raw: Any,
    *,
    expected_phase: str,
    expected_artifact_sha256: str | None = None,
    expected_input_artifact_sha256: str | None = None,
    expected_response_sha256: str | None = None,
    expected_outcome: str = "accepted",
) -> dict[str, Any]:
    """Strictly validate immutable accepted or invalid frontier evidence."""
    receipt = _object(raw, "frontierReceipt")
    if expected_outcome == "accepted":
        required = {
            "schemaVersion",
            "phase",
            "adapter",
            "provider",
            "model",
            "artifactSha256",
            "acceptedAt",
            "acceptedResponses",
            "attemptedResponses",
            "usage",
        }
        optional = {"inputArtifactSha256"}
    elif expected_outcome == "invalid":
        required = {
            "schemaVersion",
            "phase",
            "outcome",
            "adapter",
            "provider",
            "model",
            "responseSha256",
            "recordedAt",
            "acceptedResponses",
            "attemptedResponses",
            "usage",
        }
        optional = {"inputArtifactSha256"}
    else:
        raise CommanderError("Unsupported frontier receipt outcome.")
    _exact_keys(receipt, "frontierReceipt", required, optional)
    if receipt.get("schemaVersion") != COMMANDER_SCHEMA_VERSION:
        raise CommanderError("Unsupported frontier receipt schema version.")
    if receipt.get("phase") != expected_phase:
        raise CommanderError("Frontier receipt phase is invalid.")
    _text(receipt.get("adapter"), "frontierReceipt.adapter", 100)
    _optional_text(
        receipt.get("provider"),
        "frontierReceipt.provider",
        200,
    )
    _optional_text(receipt.get("model"), "frontierReceipt.model", 300)
    expected_counts = (
        (1, 1) if expected_outcome == "accepted" else (0, 1)
    )
    counts = (
        receipt.get("acceptedResponses"),
        receipt.get("attemptedResponses"),
    )
    if counts != expected_counts:
        raise CommanderError("Frontier receipt response counts are invalid.")
    if expected_outcome == "accepted":
        artifact_digest = _sha256(
            receipt.get("artifactSha256"),
            "frontierReceipt.artifactSha256",
        )
        _text(
            receipt.get("acceptedAt"),
            "frontierReceipt.acceptedAt",
            100,
        )
        if (
            expected_artifact_sha256 is not None
            and artifact_digest != expected_artifact_sha256
        ):
            raise CommanderError(
                "Frontier receipt artifact digest does not match its artifact."
            )
    else:
        if receipt.get("outcome") != "invalid":
            raise CommanderError("Frontier attempt outcome is invalid.")
        response_digest = _sha256(
            receipt.get("responseSha256"),
            "frontierReceipt.responseSha256",
        )
        if (
            expected_response_sha256 is not None
            and response_digest != expected_response_sha256
        ):
            raise CommanderError(
                "Frontier attempt response digest does not match its raw "
                "evidence."
            )
        _text(
            receipt.get("recordedAt"),
            "frontierReceipt.recordedAt",
            100,
        )
    input_digest = receipt.get("inputArtifactSha256")
    if input_digest is not None:
        input_digest = _sha256(
            input_digest,
            "frontierReceipt.inputArtifactSha256",
        )
    if (
        expected_input_artifact_sha256 is not None
        and input_digest != expected_input_artifact_sha256
    ):
        raise CommanderError(
            "Frontier receipt input digest does not match its artifact."
        )
    usage_raw = _object(receipt.get("usage"), "frontierReceipt.usage")
    _exact_keys(
        usage_raw,
        "frontierReceipt.usage",
        {
            "usageStatus",
            "promptTokens",
            "completionTokens",
            "totalTokens",
        },
    )
    usage_status = usage_raw.get("usageStatus")
    if usage_status not in USAGE_STATUSES:
        raise CommanderError("Frontier receipt usage status is invalid.")
    if usage_status == "unavailable":
        if any(
            usage_raw.get(key) is not None
            for key in (
                "promptTokens",
                "completionTokens",
                "totalTokens",
            )
        ):
            raise CommanderError(
                "Unavailable frontier usage cannot contain token values."
            )
    else:
        frontier_usage(
            prompt_tokens=usage_raw.get("promptTokens"),
            completion_tokens=usage_raw.get("completionTokens"),
            total_tokens=usage_raw.get("totalTokens"),
        )
    return receipt


def _usage_phase(
    receipt: dict[str, Any] | None,
    *,
    pending: bool,
) -> dict[str, Any]:
    if receipt is None:
        return {
            "acceptedResponses": 0,
            "attemptedResponses": 0,
            "usageStatus": "pending" if pending else "not_recorded",
            "promptTokens": None,
            "completionTokens": None,
            "totalTokens": None,
        }
    usage = receipt.get("usage", {})
    return {
        "acceptedResponses": receipt.get("acceptedResponses", 1),
        "attemptedResponses": receipt.get("attemptedResponses", 1),
        "usageStatus": usage.get("usageStatus", "unavailable"),
        "promptTokens": usage.get("promptTokens"),
        "completionTokens": usage.get("completionTokens"),
        "totalTokens": usage.get("totalTokens"),
        "adapter": receipt.get("adapter"),
        "provider": receipt.get("provider"),
        "model": receipt.get("model"),
        "artifactSha256": receipt.get("artifactSha256"),
        "acceptedAt": receipt.get("acceptedAt"),
        "recordedAt": receipt.get("recordedAt"),
        "outcome": receipt.get("outcome", "accepted"),
    }


def _invalid_frontier_attempt_receipt(
    *,
    phase: str,
    adapter: str,
    provider: str | None,
    model: str | None,
    response: str,
    usage: FrontierUsage,
    input_artifact_sha256: str | None = None,
) -> dict[str, Any]:
    """Record spent frontier usage even when its structured artifact is invalid."""
    value = {
        "schemaVersion": COMMANDER_SCHEMA_VERSION,
        "phase": phase,
        "outcome": "invalid",
        "adapter": adapter,
        "provider": provider,
        "model": model,
        "responseSha256": hashlib.sha256(
            response.encode("utf-8")
        ).hexdigest(),
        "recordedAt": utc_now(),
        "acceptedResponses": 0,
        "attemptedResponses": 1,
        "usage": usage.to_json(),
    }
    if input_artifact_sha256 is not None:
        value["inputArtifactSha256"] = input_artifact_sha256
    return value


def _request_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ").lower()
    return f"request-{timestamp}-{uuid.uuid4().hex[:8]}"


def _read_bounded_text(path: Path) -> str:
    resolved = path.resolve()
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise CommanderError(f"Frontier response is unavailable: {exc}") from exc
    if size > MAX_FRONTIER_RESPONSE_BYTES:
        raise CommanderError(
            f"Frontier response exceeds {MAX_FRONTIER_RESPONSE_BYTES} bytes."
        )
    return resolved.read_text(encoding="utf-8")


def _capture_frontier_response(
    destination: Path,
    source: Path,
) -> str:
    """Publish one raw response, or resume from the identical durable copy."""
    if destination.is_symlink():
        raise CommanderError(
            f"Frontier response evidence cannot be a symlink: "
            f"{destination.name}"
        )
    if destination.is_file():
        existing = _read_bounded_text(destination)
        try:
            incoming = _read_bounded_text(source)
        except CommanderError:
            # A crash may occur after the raw response becomes durable but
            # before its receipt/state. The immutable local copy is sufficient
            # to finish that same claimed phase.
            return existing
        durable_incoming = (
            incoming
            if not incoming or incoming.endswith("\n")
            else incoming + "\n"
        )
        if durable_incoming != existing:
            raise CommanderError(
                "The supplied frontier response differs from the already "
                "recorded raw evidence."
            )
        return existing
    response = _read_bounded_text(source)
    _exclusive_text(destination, response)
    return (
        response
        if not response or response.endswith("\n")
        else response + "\n"
    )


def _exclusive_text(
    path: Path,
    value: str,
    *,
    exists_message: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            if value and not value.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise CommanderError(
                exists_message
                or f"Immutable artifact already exists: {path.name}"
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
    )


def _required_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CommanderError(f"Required artifact is missing: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise CommanderError(f"Invalid JSON artifact {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise CommanderError(f"{path.name} must contain a JSON object.")
    return value


def _optional_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return _required_json(path)


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CommanderError(f"{name} must be an object.")
    return value


def _exact_keys(
    value: dict[str, Any],
    name: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise CommanderError(
            f"{name} is missing required fields: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise CommanderError(
            f"{name} has unknown fields: {', '.join(sorted(unknown))}"
        )


def _text(value: Any, name: str, max_characters: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommanderError(f"{name} must be a non-empty string.")
    normalized = value.strip()
    if len(normalized) > max_characters:
        raise CommanderError(
            f"{name} cannot exceed {max_characters} characters."
        )
    return normalized


def _optional_text(
    value: str | None,
    name: str,
    max_characters: int,
) -> str | None:
    if value is None:
        return None
    return _text(value, name, max_characters)


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CommanderError(f"{name} must be an integer.")
    if not minimum <= value <= maximum:
        raise CommanderError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise CommanderError(
            f"{name} must be a safe identifier."
        )
    return value


def _lineage(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SAFE_LINEAGE.fullmatch(value) is None:
        raise CommanderError(f"{name} must be planId/sessionId.")
    if ".." in value.split("/"):
        raise CommanderError(f"{name} cannot contain traversal.")
    return value


def _sha256(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{64}", value) is None
    ):
        raise CommanderError(f"{name} must be a lowercase SHA-256 digest.")
    return value


def _git_oid(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None
    ):
        raise CommanderError(
            f"{name} must be a lowercase Git object ID."
        )
    return value


def _parse_approval(value: dict[str, Any]) -> None:
    _exact_keys(
        value,
        "request.approval",
        {
            "schemaVersion",
            "requestId",
            "planSha256",
            "approvedAt",
            "source",
        },
        {
            "executionDigest",
            "workspaceRoot",
            "baseSha",
            "approvalMode",
            "workspaceTarget",
            "executionPolicySha256",
        },
    )
    if value["schemaVersion"] != COMMANDER_SCHEMA_VERSION:
        raise CommanderError("Unsupported plan approval schema version.")
    _identifier(value["requestId"], "request.approval.requestId")
    _sha256(value["planSha256"], "request.approval.planSha256")
    _text(value["approvedAt"], "request.approval.approvedAt", 100)
    _text(value["source"], "request.approval.source", 100)
    legacy_workspace_fields = {
        "executionDigest",
        "workspaceRoot",
        "baseSha",
    }
    policy_fields = {
        "approvalMode",
        "workspaceTarget",
        "executionPolicySha256",
    }
    workspace_fields = legacy_workspace_fields | policy_fields
    present = workspace_fields.intersection(value)
    if frozenset(present) not in {
        frozenset(),
        frozenset(legacy_workspace_fields),
        frozenset(workspace_fields),
    }:
        raise CommanderError(
            "Workspace plan approval fields must be recorded together."
        )
    if present:
        _sha256(
            value["executionDigest"],
            "request.approval.executionDigest",
        )
        _text(
            value["workspaceRoot"],
            "request.approval.workspaceRoot",
            10_000,
        )
        _git_oid(value["baseSha"], "request.approval.baseSha")
        if policy_fields.issubset(present):
            from .workspace import (
                WorkspaceError,
                canonical_sha256,
                execution_policy,
            )

            try:
                policy = execution_policy(
                    approval_mode=value["approvalMode"],
                    workspace_target=value["workspaceTarget"],
                )
            except WorkspaceError as exc:
                raise CommanderError(str(exc)) from exc
            recorded_policy_digest = _sha256(
                value["executionPolicySha256"],
                "request.approval.executionPolicySha256",
            )
            if recorded_policy_digest != canonical_sha256(policy):
                raise CommanderError(
                    "Request approval execution policy digest is invalid."
                )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
