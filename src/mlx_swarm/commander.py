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

    def to_json(self) -> dict[str, Any]:
        return {
            "schemaVersion": COMMANDER_SCHEMA_VERSION,
            "phase": self.phase,
            "adapter": self.adapter,
            "provider": self.provider,
            "model": self.model,
            "artifactSha256": self.artifact_sha256,
            "acceptedAt": self.accepted_at,
            "acceptedResponses": 1,
            "usage": self.usage.to_json(),
        }


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
    workspace_contract = ""
    task_workspace_fields = ""
    schema_version = 1
    if config.workspace is not None:
        schema_version = 2
        profiles = ", ".join(
            sorted(config.workspace.verification_profiles)
        ) or "(none)"
        roots = ", ".join(config.workspace.write_roots)
        workspace_contract = f"""
WORKSPACE EXECUTION CONTRACT
- Every task must declare artifactType, allowedPaths, and verification.
- artifactType is patch, test-suite, review, or report.
- Every task should declare workerOutputProtocol.
- For patch and test-suite tasks, prefer workerOutputProtocol
  edit-manifest-v1: workers return strict exact search/replace JSON and the
  runtime materializes the operator-visible unified Git diff. Such tasks
  require a JSON gate whose jsonRequiredKeys and jsonAllowedKeys are both
  exactly ["edits"].
- workerOutputProtocol artifact keeps the original direct unified-diff path.
- review and report tasks use workerOutputProtocol artifact.
- patch and test-suite require at least one allowed path.
- review and report use empty allowedPaths and verification arrays.
- task allowedPaths must stay within configured write roots: {roots}
- verification may contain only these profile IDs: {profiles}
- workers never receive or produce command arrays.
- at most one patch or test-suite task may exist in each DAG level.
"""
        task_workspace_fields = """
      "artifactType": "patch|test-suite|review|report",
      "workerOutputProtocol": "artifact|edit-manifest-v1",
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
{workspace_contract}

TOP-LEVEL SHAPE
{{
  "schemaVersion": {schema_version},
  "planId": "lowercase-id",
  "objective": "string",
  "context": {{
    "objective": "string",
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
        "max_tokens": 1200
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
        return self.request_detail(request_id)

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
        execution_error = None
        if plan_payload is not None and plan.workspace_execution:
            from .workspace import WorkspaceError, execution_preview as preview

            try:
                execution_preview = preview(self.config, plan)
            except WorkspaceError as exc:
                execution_error = str(exc)
        return {
            "request": request,
            "plan": plan_payload,
            "planningReceipt": _optional_json(
                request_dir / "frontier-plan-receipt.json"
            ),
            "validationError": _optional_json(request_dir / "plan.error.json"),
            "executionPreview": execution_preview,
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
        response = _read_bounded_text(response_path)
        _exclusive_text(request_dir / "frontier-plan.raw.txt", response)
        request["updatedAt"] = utc_now()
        try:
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
            finally:
                if candidate.exists() and candidate.name.endswith(".tmp"):
                    candidate.unlink()
        except (CommanderError, ContractError, OSError, ValueError) as exc:
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
        _atomic_json(request_dir / "plan.validated.json", canonical_plan)
        receipt = FrontierReceipt(
            phase="plan",
            adapter=normalized_adapter,
            provider=normalized_provider,
            model=normalized_model,
            artifact_sha256=plan_digest,
            accepted_at=utc_now(),
            usage=usage,
        ).to_json()
        _atomic_json(request_dir / "frontier-plan-receipt.json", receipt)
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
                preview = execution_preview(self.config, plan)
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
        prompt_path = session_dir / "frontier-review-prompt.txt"
        if not prompt_path.exists():
            _atomic_text(prompt_path, build_review_prompt(result))
        claim = self._create_claim(
            session_dir / "frontier-review.claim.json",
            phase="review",
            owner_id=f"{state['planId']}/{state['sessionId']}",
            adapter=adapter,
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
        if (session_dir / "frontier-review.json").exists():
            raise CommanderError("Frontier review has already been accepted.")
        claim = _required_json(session_dir / "frontier-review.claim.json")
        self._require_claim(claim, claim_id, "review")
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
        response = _read_bounded_text(response_path)
        _exclusive_text(session_dir / "frontier-review.raw.txt", response)
        try:
            raw_review = parse_json_response(response)
            review = parse_frontier_review(
                raw_review,
                expected_plan_id=state["planId"],
                expected_session_id=state["sessionId"],
            )
        except (CommanderError, ValueError) as exc:
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
            raise CommanderError(
                f"Frontier review is invalid and this phase is sealed: {exc}"
            ) from exc
        review_payload = review.to_json()
        review_digest = canonical_json_sha256(review_payload)
        _atomic_json(session_dir / "frontier-review.json", review_payload)
        receipt = FrontierReceipt(
            phase="review",
            adapter=normalized_adapter,
            provider=normalized_provider,
            model=normalized_model,
            artifact_sha256=review_digest,
            accepted_at=utc_now(),
            usage=usage,
        ).to_json()
        _atomic_json(session_dir / "frontier-review-receipt.json", receipt)
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
            "frontierUsage": _optional_json(
                session_dir / "frontier-usage.json"
            ),
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
    review_receipt = _optional_json(
        session_dir / "frontier-review-receipt.json"
    )
    planning = _usage_phase(plan_receipt, pending=False)
    review = _usage_phase(review_receipt, pending=review_receipt is None)
    phases = [phase for phase in (planning, review) if phase["acceptedResponses"]]
    all_reported = bool(phases) and all(
        phase["usageStatus"] == "reported" for phase in phases
    )
    totals = {
        "acceptedResponses": sum(
            phase["acceptedResponses"] for phase in (planning, review)
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


def _usage_phase(
    receipt: dict[str, Any] | None,
    *,
    pending: bool,
) -> dict[str, Any]:
    if receipt is None:
        return {
            "acceptedResponses": 0,
            "usageStatus": "pending" if pending else "not_recorded",
            "promptTokens": None,
            "completionTokens": None,
            "totalTokens": None,
        }
    usage = receipt.get("usage", {})
    return {
        "acceptedResponses": 1,
        "usageStatus": usage.get("usageStatus", "unavailable"),
        "promptTokens": usage.get("promptTokens"),
        "completionTokens": usage.get("completionTokens"),
        "totalTokens": usage.get("totalTokens"),
        "adapter": receipt.get("adapter"),
        "provider": receipt.get("provider"),
        "model": receipt.get("model"),
        "artifactSha256": receipt.get("artifactSha256"),
        "acceptedAt": receipt.get("acceptedAt"),
    }


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
    )
    if value["schemaVersion"] != COMMANDER_SCHEMA_VERSION:
        raise CommanderError("Unsupported plan approval schema version.")
    _identifier(value["requestId"], "request.approval.requestId")
    _sha256(value["planSha256"], "request.approval.planSha256")
    _text(value["approvedAt"], "request.approval.approvedAt", 100)
    _text(value["source"], "request.approval.source", 100)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
