"""Safe Git worktree execution for typed MLX Swarm artifacts."""
# @lat: [[workspace-execution]]

from __future__ import annotations

import difflib
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from .contracts import (
    MAX_COMMAND_OUTPUT_BYTES,
    MUTATING_ARTIFACT_TYPES,
    Plan,
    SwarmConfig,
    TaskDef,
)

WORKSPACE_CONTRACT_VERSION = 2
REVISION_WORKSPACE_CONTRACT_VERSION = 3
ARTIFACT_SCHEMA_VERSION = 1
DECISION_SCHEMA_VERSION = 1
EXECUTION_POLICY_VERSION = 2
CHECKOUT_LEASE_SCHEMA_VERSION = 2
VERIFICATION_RECEIPT_SCHEMA_VERSION = 2
MAX_CARRIED_NON_MUTATING_CHARS = 8_000
MAX_CARRIED_MUTATING_DIFF_CHARS = 8_000
APPROVAL_MODES = {"supervised", "yolo"}
WORKSPACE_TARGETS = {"worktree", "checkout"}
DEFAULT_APPROVAL_MODE = "supervised"
DEFAULT_WORKSPACE_TARGET = "worktree"
_DIFF_HEADER = re.compile(r"^diff --git a/([^\t\r\n]+) b/([^\t\r\n]+)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^[0-9a-f]{40,64}$")
_FORBIDDEN_DIFF_MARKERS = (
    "GIT binary patch",
    "Binary files ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "new file mode 120000",
    "old mode 120000",
    "new file mode 160000",
    "old mode 160000",
)
_SPECIAL_GIT_MODE = re.compile(
    r"^(?:index [^\r\n]+ |(?:old|new) file mode )(?:120000|160000)$",
    re.MULTILINE,
)
_GIT_PREFIX = (
    "git",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "diff.external=",
)


class WorkspaceError(RuntimeError):
    """Raised when a workspace operation violates the execution boundary."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_revision_authority(
    root: Path,
    raw: dict[str, Any],
    *,
    require_predecessor_branch: bool,
) -> dict[str, Any]:
    """Validate the frozen predecessor head used by an incremental revision."""
    required = {
        "schemaVersion",
        "revisionOf",
        "revisionInputSha256",
        "predecessorExecutionDigest",
        "predecessorBranch",
        "baseSha",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise WorkspaceError("Incremental revision authority is invalid.")
    if raw.get("schemaVersion") != 1:
        raise WorkspaceError(
            "Unsupported incremental revision authority version."
        )
    for key in ("revisionInputSha256", "predecessorExecutionDigest"):
        if _SHA256.fullmatch(str(raw.get(key))) is None:
            raise WorkspaceError(
                f"Incremental revision {key} is invalid."
            )
    if _GIT_OID.fullmatch(str(raw.get("baseSha"))) is None:
        raise WorkspaceError("Incremental revision baseSha is invalid.")
    revision_of = raw.get("revisionOf")
    if (
        not isinstance(revision_of, str)
        or revision_of.count("/") != 1
        or any(not part for part in revision_of.split("/", 1))
    ):
        raise WorkspaceError("Incremental revision lineage is invalid.")
    branch = raw.get("predecessorBranch")
    if not isinstance(branch, str) or not branch:
        raise WorkspaceError(
            "Incremental revision predecessor branch is invalid."
        )
    _git(root, ["check-ref-format", "--branch", branch])
    base_sha = str(raw["baseSha"])
    _git(root, ["cat-file", "-e", f"{base_sha}^{{commit}}"])
    if require_predecessor_branch:
        branch_head = _git_text(
            root,
            ["rev-parse", f"refs/heads/{branch}"],
        )
        if branch_head != base_sha:
            raise WorkspaceError(
                "Incremental revision predecessor branch moved after "
                "evidence was frozen."
            )
    return {
        "schemaVersion": 1,
        "revisionOf": revision_of,
        "revisionInputSha256": str(raw["revisionInputSha256"]),
        "predecessorExecutionDigest": str(
            raw["predecessorExecutionDigest"]
        ),
        "predecessorBranch": branch,
        "baseSha": base_sha,
    }


def execution_preview(
    config: SwarmConfig,
    plan: Plan,
    *,
    approval_mode: str = DEFAULT_APPROVAL_MODE,
    workspace_target: str = DEFAULT_WORKSPACE_TARGET,
    revision_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve and hash the operator-visible workspace execution contract."""
    if config.workspace is None or not plan.workspace_execution:
        raise WorkspaceError(
            "Workspace execution requires schema-v2 config and plan contracts."
        )
    policy = execution_policy(
        approval_mode=approval_mode,
        workspace_target=workspace_target,
    )
    root = discover_git_root(config.source.parent)
    base_sha = _git_text(root, ["rev-parse", "HEAD"])
    branch = _current_branch(
        root,
        required=workspace_target == "checkout",
    )
    normalized_revision_authority = None
    if revision_authority is not None:
        if workspace_target != "worktree":
            raise WorkspaceError(
                "Incremental revisions require an isolated worktree."
            )
        normalized_revision_authority = _validate_revision_authority(
            root,
            revision_authority,
            require_predecessor_branch=True,
        )
        base_sha = normalized_revision_authority["baseSha"]
    _reject_external_filters(root)
    worktrees_root = (config.artifacts_dir / "_worktrees").resolve()
    _require_runtime_root_safe(root, config.artifacts_dir.resolve())
    _require_runtime_root_safe(root, worktrees_root)
    dirty = _dirty_entries(root)
    if workspace_target == "checkout" and dirty:
        raise WorkspaceError(
            "Main-checkout YOLO requires a completely clean repository "
            "(staged, unstaged, and untracked files are all refused)."
        )
    if workspace_target == "checkout":
        lease = checkout_lease(root)
        if lease is not None:
            raise WorkspaceError(
                "Main checkout is reserved by unresolved session "
                f"{lease.get('planId')}/{lease.get('sessionId')}."
            )
    referenced = sorted(
        {
            identifier
            for task in plan.tasks
            for identifier in task.verification
        }
        | set(plan.integration_verification)
    )
    profiles: dict[str, Any] = {}
    for identifier in referenced:
        profile = config.workspace.verification_profiles[identifier]
        profiles[identifier] = {
            "argv": list(profile.argv),
            "cwd": profile.cwd,
            "timeoutSeconds": profile.timeout_seconds,
            "inheritEnv": list(profile.inherit_env),
            "environment": dict(profile.environment),
        }
    contract = {
        "schemaVersion": (
            REVISION_WORKSPACE_CONTRACT_VERSION
            if normalized_revision_authority is not None
            else WORKSPACE_CONTRACT_VERSION
        ),
        "planSha256": canonical_sha256(plan.raw),
        "workspaceRoot": str(root),
        "baseSha": base_sha,
        "writeRoots": list(config.workspace.write_roots),
        "verificationProfiles": profiles,
        "worktreesRoot": str(worktrees_root),
        "startingBranch": branch,
        "executionPolicy": policy,
        "executionPolicySha256": canonical_sha256(policy),
    }
    if normalized_revision_authority is not None:
        contract["revisionAuthority"] = normalized_revision_authority
    return {
        **contract,
        "executionDigest": canonical_sha256(contract),
        "ready": True,
        "dirty": bool(dirty),
        "dirtyEntries": dirty[:256],
        "dirtyEntriesTruncated": len(dirty) > 256,
    }


def execution_policy(
    *,
    approval_mode: str = DEFAULT_APPROVAL_MODE,
    workspace_target: str = DEFAULT_WORKSPACE_TARGET,
    policy_version: int = EXECUTION_POLICY_VERSION,
) -> dict[str, Any]:
    """Validate the operator-owned run policy that is bound into approval."""
    if (
        not isinstance(approval_mode, str)
        or approval_mode not in APPROVAL_MODES
    ):
        raise WorkspaceError(
            "Approval mode must be one of: "
            + ", ".join(sorted(APPROVAL_MODES))
        )
    if (
        not isinstance(workspace_target, str)
        or workspace_target not in WORKSPACE_TARGETS
    ):
        raise WorkspaceError(
            "Workspace target must be one of: "
            + ", ".join(sorted(WORKSPACE_TARGETS))
        )
    if workspace_target == "checkout" and approval_mode != "yolo":
        raise WorkspaceError(
            "The main checkout is available only with explicit YOLO mode."
        )
    if policy_version not in {1, EXECUTION_POLICY_VERSION}:
        raise WorkspaceError("Unsupported execution policy version.")
    return {
        "schemaVersion": policy_version,
        "approvalMode": approval_mode,
        "workspaceTarget": workspace_target,
        "onVerificationFailure": (
            "repair-once"
            if policy_version >= 2
            and approval_mode == "yolo"
            and workspace_target == "worktree"
            else "pause"
        ),
    }


def validate_execution_snapshot(
    workspace: dict[str, Any],
    *,
    plan: Plan,
    approval: dict[str, Any] | None,
    session_id: str | None = None,
) -> None:
    """Recompute immutable execution authority before every runner starts."""
    version = workspace.get("schemaVersion")
    if version not in {
        1,
        WORKSPACE_CONTRACT_VERSION,
        REVISION_WORKSPACE_CONTRACT_VERSION,
    }:
        raise WorkspaceError(
            "Unsupported workspace execution snapshot version."
        )
    if version == 1:
        legacy_keys = (
            "schemaVersion",
            "planSha256",
            "workspaceRoot",
            "baseSha",
            "writeRoots",
            "verificationProfiles",
            "worktreesRoot",
        )
        contract = {key: workspace.get(key) for key in legacy_keys}
        if workspace.get("planSha256") != canonical_sha256(plan.raw):
            raise WorkspaceError("Execution snapshot plan digest is invalid.")
        if workspace.get("executionDigest") != canonical_sha256(contract):
            raise WorkspaceError("Execution snapshot digest is invalid.")
        if not isinstance(approval, dict):
            raise WorkspaceError("Execution approval is missing.")
        required = {
            "planSha256": workspace["planSha256"],
            "executionDigest": workspace["executionDigest"],
        }
        optional = {
            "workspaceRoot": workspace["workspaceRoot"],
            "baseSha": workspace["baseSha"],
        }
        if any(approval.get(key) != value for key, value in required.items()):
            raise WorkspaceError(
                "Execution approval differs from the immutable snapshot."
            )
        if any(
            key in approval and approval[key] != value
            for key, value in optional.items()
        ):
            raise WorkspaceError(
                "Execution approval differs from the immutable snapshot."
            )
        return
    raw_policy = workspace.get("executionPolicy")
    if not isinstance(raw_policy, dict):
        raise WorkspaceError("Execution policy is missing from the snapshot.")
    policy = execution_policy(
        approval_mode=raw_policy.get("approvalMode"),
        workspace_target=raw_policy.get("workspaceTarget"),
        policy_version=raw_policy.get("schemaVersion", 1),
    )
    if raw_policy != policy:
        raise WorkspaceError("Execution policy snapshot is invalid.")
    policy_digest = canonical_sha256(policy)
    if workspace.get("executionPolicySha256") != policy_digest:
        raise WorkspaceError("Execution policy digest is invalid.")
    if workspace.get("planSha256") != canonical_sha256(plan.raw):
        raise WorkspaceError("Execution snapshot plan digest is invalid.")
    if workspace.get("planId") != plan.plan_id:
        raise WorkspaceError("Execution snapshot plan identity is invalid.")
    if session_id is not None and workspace.get("sessionId") != session_id:
        raise WorkspaceError("Execution snapshot session identity is invalid.")
    runtime_session_id = workspace.get("sessionId")
    if not isinstance(runtime_session_id, str) or not runtime_session_id:
        raise WorkspaceError("Execution snapshot session identity is invalid.")
    if policy["workspaceTarget"] == "checkout":
        if workspace.get("branch") != workspace.get("startingBranch"):
            raise WorkspaceError(
                "Main-checkout branch differs from the approved contract."
            )
    else:
        expected_branch = (
            f"mlx-swarm/{plan.plan_id}/{runtime_session_id}"
        )
        if workspace.get("branch") != expected_branch:
            raise WorkspaceError(
                "Session worktree branch identity is invalid."
            )
    contract_keys = [
        "schemaVersion",
        "planSha256",
        "workspaceRoot",
        "baseSha",
        "writeRoots",
        "verificationProfiles",
        "worktreesRoot",
        "startingBranch",
        "executionPolicy",
        "executionPolicySha256",
    ]
    if version == REVISION_WORKSPACE_CONTRACT_VERSION:
        if policy["workspaceTarget"] != "worktree":
            raise WorkspaceError(
                "Incremental revisions require an isolated worktree."
            )
        authority = _validate_revision_authority(
            Path(workspace["workspaceRoot"]).resolve(),
            workspace.get("revisionAuthority"),
            require_predecessor_branch=False,
        )
        if workspace.get("baseSha") != authority["baseSha"]:
            raise WorkspaceError(
                "Incremental revision base differs from its authority."
            )
        contract_keys.append("revisionAuthority")
    elif workspace.get("revisionAuthority") is not None:
        raise WorkspaceError(
            "Legacy workspace snapshots cannot contain revision authority."
        )
    contract = {key: workspace.get(key) for key in contract_keys}
    execution_digest = canonical_sha256(contract)
    if workspace.get("executionDigest") != execution_digest:
        raise WorkspaceError("Execution snapshot digest is invalid.")
    if not isinstance(approval, dict):
        raise WorkspaceError("Execution approval is missing.")
    expected = {
        "planSha256": workspace["planSha256"],
        "executionDigest": execution_digest,
        "workspaceRoot": workspace["workspaceRoot"],
        "baseSha": workspace["baseSha"],
        "approvalMode": policy["approvalMode"],
        "workspaceTarget": policy["workspaceTarget"],
        "executionPolicySha256": policy_digest,
    }
    if any(approval.get(key) != value for key, value in expected.items()):
        raise WorkspaceError(
            "Execution approval differs from the immutable snapshot."
        )


def execution_previews(
    config: SwarmConfig,
    plan: Plan,
    *,
    revision_authority: dict[str, Any] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return every supported operator choice with errors kept local."""
    result: dict[str, dict[str, dict[str, Any]]] = {
        "supervised": {},
        "yolo": {},
    }
    for approval_mode, targets in (
        ("supervised", ("worktree",)),
        ("yolo", ("worktree", "checkout")),
    ):
        for workspace_target in targets:
            try:
                result[approval_mode][workspace_target] = execution_preview(
                    config,
                    plan,
                    approval_mode=approval_mode,
                    workspace_target=workspace_target,
                    revision_authority=revision_authority,
                )
            except WorkspaceError as exc:
                policy = execution_policy(
                    approval_mode=approval_mode,
                    workspace_target=workspace_target,
                )
                result[approval_mode][workspace_target] = {
                    "ready": False,
                    "executionPolicy": policy,
                    "error": str(exc),
                }
    return result


def workspace_readiness(config: SwarmConfig) -> dict[str, Any]:
    if config.workspace is None:
        return {
            "enabled": False,
            "ready": False,
            "error": "Config schema v1 is generation-only.",
        }
    try:
        root = discover_git_root(config.source.parent)
        _reject_external_filters(root)
        worktrees_root = (config.artifacts_dir / "_worktrees").resolve()
        _require_runtime_root_safe(root, config.artifacts_dir.resolve())
        _require_runtime_root_safe(root, worktrees_root)
        base_sha = _git_text(root, ["rev-parse", "HEAD"])
        dirty = _dirty_entries(root)
        branch = _current_branch(root, required=False)
        lease = checkout_lease(root)
        return {
            "enabled": True,
            "ready": True,
            "workspaceRoot": str(root),
            "baseSha": base_sha,
            "currentBranch": branch,
            "worktreesRoot": str(worktrees_root),
            "dirty": bool(dirty),
            "dirtyEntries": dirty[:256],
            "dirtyEntriesTruncated": len(dirty) > 256,
            "writeRoots": list(config.workspace.write_roots),
            "verificationProfiles": sorted(
                config.workspace.verification_profiles
            ),
            "executionChoices": {
                "supervised": ["worktree"],
                "yolo": ["worktree", "checkout"],
            },
            "checkoutReady": (
                not dirty and branch is not None and lease is None
            ),
            "checkoutLease": lease,
            "checkoutError": (
                None
                if not dirty and branch is not None and lease is None
                else (
                    "Main-checkout YOLO requires a checked-out branch."
                    if branch is None
                    else (
                        "Main checkout is reserved by an unresolved session."
                        if lease is not None
                        else "Main-checkout YOLO requires a completely clean repository."
                    )
                )
            ),
        }
    except WorkspaceError as exc:
        return {
            "enabled": True,
            "ready": False,
            "error": str(exc),
        }


def discover_git_root(start: Path) -> Path:
    result = _run(
        [
            *_GIT_PREFIX,
            "-C",
            str(start.resolve()),
            "rev-parse",
            "--show-toplevel",
        ],
        check=False,
    )
    if result.returncode != 0:
        raise WorkspaceError(
            "No Git repository was found above the config directory."
        )
    root = Path(result.stdout.decode("utf-8", "replace").strip()).resolve()
    if not root.is_dir():
        raise WorkspaceError("Detected Git workspace root does not exist.")
    return root


def prepare_worktree(
    config: SwarmConfig,
    plan: Plan,
    *,
    session_id: str,
    expected_execution_digest: str,
) -> dict[str, Any]:
    """Create one durable session branch and isolated worktree."""
    return prepare_workspace(
        config,
        plan,
        session_id=session_id,
        expected_execution_digest=expected_execution_digest,
        approval_mode=DEFAULT_APPROVAL_MODE,
        workspace_target=DEFAULT_WORKSPACE_TARGET,
    )


def prepare_workspace(
    config: SwarmConfig,
    plan: Plan,
    *,
    session_id: str,
    expected_execution_digest: str,
    approval_mode: str = DEFAULT_APPROVAL_MODE,
    workspace_target: str = DEFAULT_WORKSPACE_TARGET,
    revision_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare either an isolated worktree or the explicit main checkout."""
    preview = execution_preview(
        config,
        plan,
        approval_mode=approval_mode,
        workspace_target=workspace_target,
        revision_authority=revision_authority,
    )
    if expected_execution_digest != preview["executionDigest"]:
        raise WorkspaceError(
            "Execution digest mismatch; refresh the workspace preview."
        )
    root = Path(preview["workspaceRoot"])
    if workspace_target == "checkout":
        if _dirty_entries(root):
            raise WorkspaceError(
                "Main-checkout YOLO requires a completely clean repository."
            )
        actual_head = _git_text(root, ["rev-parse", "HEAD"])
        actual_branch = _current_branch(root)
        if (
            actual_head != preview["baseSha"]
            or actual_branch != preview["startingBranch"]
        ):
            raise WorkspaceError(
                "Main checkout changed after preview; refresh approval."
            )
        snapshot = {
            key: value
            for key, value in preview.items()
            if key not in {"dirtyEntries", "dirtyEntriesTruncated", "ready"}
        }
        snapshot.update({
            "planId": plan.plan_id,
            "sessionId": session_id,
            "branch": actual_branch,
            "executionPath": str(root),
            # Compatibility for pre-policy workspace consumers.
            "worktreePath": str(root),
            "headSha": preview["baseSha"],
            "dirtyEntries": [],
            "dirtyEntriesTruncated": False,
            "preparedAt": utc_now(),
            "cleanedUp": False,
            "cleanupAllowed": False,
        })
        return snapshot

    worktree = (
        Path(preview["worktreesRoot"]) / plan.plan_id / session_id
    ).resolve()
    if not _is_within(worktree, Path(preview["worktreesRoot"])):
        raise WorkspaceError("Derived worktree path escapes its runtime root.")
    if worktree.exists():
        raise WorkspaceError(f"Session worktree already exists: {worktree}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    branch = f"mlx-swarm/{plan.plan_id}/{session_id}"
    _git(
        root,
        [
            "check-ref-format",
            "--branch",
            branch,
        ],
    )
    _git(
        root,
        [
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            preview["baseSha"],
        ],
    )
    snapshot = {
        key: value
        for key, value in preview.items()
        if key not in {"dirtyEntries", "dirtyEntriesTruncated", "ready"}
    }
    snapshot.update({
        "planId": plan.plan_id,
        "sessionId": session_id,
        "branch": branch,
        "executionPath": str(worktree),
        "worktreePath": str(worktree),
        "headSha": preview["baseSha"],
        "dirtyEntries": preview["dirtyEntries"],
        "dirtyEntriesTruncated": preview["dirtyEntriesTruncated"],
        "preparedAt": utc_now(),
        "cleanedUp": False,
        "cleanupAllowed": True,
    })
    return snapshot


def load_workspace_snapshot(session_dir: Path) -> dict[str, Any]:
    path = session_dir / "workspace.snapshot.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkspaceError("Workspace snapshot is missing.") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise WorkspaceError(f"Workspace snapshot is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkspaceError("Workspace snapshot must be a JSON object.")
    return value


def checkout_runner_lock_path(workspace: dict[str, Any]) -> Path:
    """Return one Git-metadata lock shared by every config for this checkout."""
    if _workspace_target(workspace) != "checkout":
        raise WorkspaceError("Checkout runner locks require a checkout target.")
    root = _execution_path(workspace)
    return _checkout_git_path(root, "mlx-swarm-checkout.runner.lock")


def checkout_lease(root: Path) -> dict[str, Any] | None:
    """Read the persisted unresolved-checkout lease, if one exists."""
    path = _checkout_git_path(root, "mlx-swarm-checkout.lease.json")
    if not path.exists():
        return None
    if path.is_symlink():
        raise WorkspaceError("Checkout lease cannot be a symlink.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise WorkspaceError("Checkout lease is invalid.") from exc
    if not isinstance(value, dict):
        raise WorkspaceError("Checkout lease contract is invalid.")
    version = value.get("schemaVersion")
    required = {
            "schemaVersion",
            "leaseId",
            "planId",
            "sessionId",
            "workspaceRoot",
            "executionDigest",
            "branch",
            "baseSha",
            "acquiredAt",
    }
    if version == CHECKOUT_LEASE_SCHEMA_VERSION:
        required.add("sessionDir")
    if (
        version not in {1, CHECKOUT_LEASE_SCHEMA_VERSION}
        or set(value) != required
        or _SHA256.fullmatch(str(value.get("leaseId"))) is None
    ):
        raise WorkspaceError("Checkout lease contract is invalid.")
    return value


def acquire_checkout_lease(
    root: Path,
    *,
    plan_id: str,
    session_id: str,
    execution_digest: str,
    branch: str,
    base_sha: str,
    session_dir: Path,
) -> dict[str, Any]:
    """Reserve one checkout across configs until its session is resolved."""
    root = root.resolve()
    identity = {
        "planId": plan_id,
        "sessionId": session_id,
        "workspaceRoot": str(root),
        "executionDigest": execution_digest,
        "branch": branch,
        "baseSha": base_sha,
        "sessionDir": str(session_dir.resolve()),
    }
    lease = {
        "schemaVersion": CHECKOUT_LEASE_SCHEMA_VERSION,
        "leaseId": canonical_sha256(identity),
        **identity,
        "acquiredAt": utc_now(),
    }
    path = _checkout_git_path(root, "mlx-swarm-checkout.lease.json")
    try:
        _exclusive_json(path, lease)
    except WorkspaceError as exc:
        existing = checkout_lease(root)
        if existing is not None and all(
            existing.get(key) == value for key, value in identity.items()
        ):
            return existing
        raise WorkspaceError(
            "Main checkout is already reserved by another unresolved session."
        ) from exc
    return lease


def require_checkout_lease(
    workspace: dict[str, Any],
    *,
    plan_id: str,
    session_id: str,
) -> dict[str, Any]:
    if _workspace_target(workspace) != "checkout":
        raise WorkspaceError("Checkout lease validation requires checkout mode.")
    root = Path(workspace["workspaceRoot"]).resolve()
    lease = checkout_lease(root)
    if (
        lease is None
        or lease.get("leaseId") != workspace.get("checkoutLeaseId")
        or lease.get("planId") != plan_id
        or lease.get("sessionId") != session_id
        or lease.get("executionDigest") != workspace.get("executionDigest")
        or (
            lease.get("schemaVersion") == CHECKOUT_LEASE_SCHEMA_VERSION
            and lease.get("sessionDir") != workspace.get("sessionDir")
        )
    ):
        raise WorkspaceError(
            "Main checkout is not leased to this execution session."
        )
    return lease


def release_checkout_lease(
    workspace: dict[str, Any],
    *,
    plan_id: str,
    session_id: str,
) -> None:
    """Release only the exact checkout lease owned by this session."""
    require_checkout_lease(
        workspace,
        plan_id=plan_id,
        session_id=session_id,
    )
    path = _checkout_git_path(
        Path(workspace["workspaceRoot"]),
        "mlx-swarm-checkout.lease.json",
    )
    path.unlink()


def _checkout_git_path(root: Path, name: str) -> Path:
    root = root.resolve()
    raw = _git_text(root, ["rev-parse", "--git-path", name])
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    path = path.parent.resolve() / path.name
    git_dir = Path(
        _git_text(root, ["rev-parse", "--absolute-git-dir"])
    ).resolve()
    try:
        path.parent.relative_to(git_dir)
    except ValueError:
        raise WorkspaceError(
            "Derived checkout coordination path escapes Git metadata."
        )
    if path.is_symlink():
        raise WorkspaceError(
            "Checkout coordination files cannot be symlinks."
        )
    return path


def persist_artifact(
    session_dir: Path,
    task: TaskDef,
    payload: str,
    workspace: dict[str, Any] | None,
) -> dict[str, Any]:
    """Persist one immutable normalized worker artifact."""
    if task.artifact_type in MUTATING_ARTIFACT_TYPES and not payload.endswith("\n"):
        payload += "\n"
    artifact_dir = session_dir / "artifacts" / task.id
    affected_paths: list[str] = []
    base_commit: str | None = None
    if task.artifact_type in MUTATING_ARTIFACT_TYPES:
        if workspace is None:
            raise WorkspaceError("Mutating artifacts require a workspace.")
        base_commit = _workspace_head(workspace)
        affected_paths = validate_patch(
            payload,
            task=task,
            workspace=workspace,
        )
    elif task.artifact_type == "review":
        try:
            review = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise WorkspaceError(
                "Review artifacts must be exact JSON objects."
            ) from exc
        if not isinstance(review, dict):
            raise WorkspaceError("Review artifacts must be JSON objects.")

    digest_value = {
        "taskId": task.id,
        "artifactType": task.artifact_type,
        "baseCommit": base_commit,
        "payload": payload,
    }
    digest = canonical_sha256(digest_value)
    if artifact_dir.exists():
        existing, existing_payload = load_artifact(session_dir, task.id)
        if existing.get("sha256") == digest and existing_payload == payload:
            return existing
        raise WorkspaceError(f"Artifact already exists for task {task.id}.")
    artifact_dir.parent.mkdir(parents=True, exist_ok=True)
    suffix = (
        ".diff"
        if task.artifact_type in MUTATING_ARTIFACT_TYPES
        else ".json"
        if task.artifact_type == "review"
        else ".md"
    )
    payload_name = f"payload{suffix}"
    manifest = {
        "schemaVersion": ARTIFACT_SCHEMA_VERSION,
        "taskId": task.id,
        "artifactType": task.artifact_type,
        "sha256": digest,
        "payload": payload_name,
        "affectedPaths": affected_paths,
        "allowedPaths": list(task.allowed_paths),
        "verification": list(task.verification),
        "baseCommit": base_commit,
        "createdAt": utc_now(),
        "decisionStatus": (
            "awaiting_approval"
            if task.artifact_type in MUTATING_ARTIFACT_TYPES
            else "not_required"
        ),
    }
    staging_dir = artifact_dir.with_name(
        f".{artifact_dir.name}.artifact-{uuid.uuid4().hex}.tmp"
    )
    staging_dir.mkdir(exist_ok=False)
    try:
        _exclusive_text(staging_dir / payload_name, payload)
        _atomic_json(staging_dir / "manifest.json", manifest)
        try:
            staging_dir.rename(artifact_dir)
        except OSError:
            if not artifact_dir.exists():
                raise
            existing, existing_payload = load_artifact(
                session_dir,
                task.id,
            )
            if (
                existing.get("sha256") == digest
                and existing_payload == payload
            ):
                return existing
            raise WorkspaceError(
                f"Artifact already exists for task {task.id}."
            )
    finally:
        if staging_dir.exists():
            for name in ("manifest.json", payload_name):
                (staging_dir / name).unlink(missing_ok=True)
            staging_dir.rmdir()
    return manifest


def materialize_edit_manifest(
    payload: str,
    *,
    task: TaskDef,
    workspace: dict[str, Any] | None,
) -> str:
    """Convert exact, bounded search/replace edits into one unified Git diff."""
    if task.worker_output_protocol != "edit-manifest-v1":
        raise WorkspaceError(
            "Task does not use the edit-manifest-v1 worker protocol."
        )
    if task.artifact_type not in MUTATING_ARTIFACT_TYPES:
        raise WorkspaceError(
            "Edit manifests are supported only for patch and test-suite tasks."
        )
    if workspace is None:
        raise WorkspaceError("Edit manifests require a workspace snapshot.")
    try:
        manifest = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise WorkspaceError("Edit manifest must be exact JSON.") from exc
    if not isinstance(manifest, dict) or set(manifest) != {"edits"}:
        raise WorkspaceError(
            "Edit manifest must contain exactly one top-level edits key."
        )
    edits = manifest["edits"]
    if not isinstance(edits, list) or not 1 <= len(edits) <= 64:
        raise WorkspaceError("Edit manifest must contain 1 to 64 edits.")

    worktree = _execution_path(workspace)
    root = Path(workspace["workspaceRoot"]).resolve()
    worktrees_root = Path(workspace["worktreesRoot"]).resolve()
    runtime_roots = [worktrees_root, worktrees_root.parent]
    originals: dict[str, str] = {}
    modified: dict[str, str] = {}
    order: list[str] = []
    created_paths: set[str] = set()
    for index, raw_edit in enumerate(edits):
        if not isinstance(raw_edit, dict) or set(raw_edit) != {
            "path",
            "old",
            "new",
        }:
            raise WorkspaceError(
                f"Edit {index + 1} must contain exactly path, old, and new."
            )
        path_value = raw_edit["path"]
        old = raw_edit["old"]
        new = raw_edit["new"]
        if not all(isinstance(value, str) for value in (path_value, old, new)):
            raise WorkspaceError(
                f"Edit {index + 1} path, old, and new must be strings."
            )
        path = _safe_patch_path(path_value)
        if old == new:
            raise WorkspaceError(f"Edit {index + 1} is a no-op.")
        if not old and not new:
            raise WorkspaceError(
                f"Edit {index + 1} new-file content must not be empty."
            )
        if not any(_path_within(path, value) for value in task.allowed_paths):
            raise WorkspaceError(
                f"Edit path is outside task.allowedPaths: {path}"
            )
        if not any(
            _path_within(path, value)
            for value in workspace["writeRoots"]
        ):
            raise WorkspaceError(
                f"Edit path is outside workspace.writeRoots: {path}"
            )
        original_path = (root / path).resolve(strict=False)
        if any(
            _is_within(original_path, runtime_root)
            for runtime_root in runtime_roots
        ):
            raise WorkspaceError(
                f"Edit path targets MLX Swarm runtime data: {path}"
            )
        candidate = worktree / path
        _reject_symlink_components(worktree, candidate, path)
        if not old:
            if candidate.exists():
                raise WorkspaceError(
                    f"Edit {index + 1} new-file path already exists: {path}"
                )
            if path in originals:
                raise WorkspaceError(
                    f"Edit {index + 1} duplicates new-file path: {path}"
                )
            originals[path] = ""
            modified[path] = new
            order.append(path)
            created_paths.add(path)
            continue
        if not candidate.is_file():
            raise WorkspaceError(f"Edit path is not a regular file: {path}")
        if path not in originals:
            try:
                content = candidate.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                raise WorkspaceError(
                    f"Edit path is not readable UTF-8 text: {path}"
                ) from exc
            if "\x00" in content:
                raise WorkspaceError(f"Edit path contains binary data: {path}")
            originals[path] = content
            modified[path] = content
            order.append(path)
        occurrences = modified[path].count(old)
        if occurrences != 1:
            raise WorkspaceError(
                f"Edit {index + 1} old text must match exactly once in "
                f"{path}; found {occurrences}."
            )
        modified[path] = modified[path].replace(old, new, 1)

    sections: list[str] = []
    for path in order:
        before = originals[path]
        after = modified[path]
        if before == after:
            continue
        unified = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=(
                    "/dev/null"
                    if path in created_paths
                    else f"a/{path}"
                ),
                tofile=f"b/{path}",
                n=3,
                lineterm="\n",
            )
        )
        if not unified:
            raise WorkspaceError(f"Could not materialize edit diff: {path}")
        mode = "new file mode 100644\n" if path in created_paths else ""
        sections.append(
            f"diff --git a/{path} b/{path}\n{mode}{unified}"
        )
    if not sections:
        raise WorkspaceError("Edit manifest produced no workspace changes.")
    diff = "".join(sections)
    if not diff.endswith("\n"):
        diff += "\n"
    return diff


def load_artifact(session_dir: Path, task_id: str) -> tuple[dict[str, Any], str]:
    artifact_dir = session_dir / "artifacts" / task_id
    try:
        manifest_path = artifact_dir / "manifest.json"
        if manifest_path.is_symlink():
            raise WorkspaceError("Artifact manifest cannot be a symlink.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise WorkspaceError("Artifact manifest must be a JSON object.")
        required = {
            "schemaVersion",
            "taskId",
            "artifactType",
            "sha256",
            "payload",
            "affectedPaths",
            "allowedPaths",
            "verification",
            "baseCommit",
            "createdAt",
            "decisionStatus",
        }
        if set(manifest) != required:
            raise WorkspaceError("Artifact manifest fields are invalid.")
        if (
            manifest["schemaVersion"] != ARTIFACT_SCHEMA_VERSION
            or manifest["taskId"] != task_id
            or manifest["artifactType"]
            not in {"patch", "test-suite", "review", "report"}
            or not isinstance(manifest["payload"], str)
            or "/" in manifest["payload"]
            or "\\" in manifest["payload"]
            or manifest["payload"] in {"", ".", ".."}
            or not isinstance(manifest["affectedPaths"], list)
            or not isinstance(manifest["allowedPaths"], list)
            or not isinstance(manifest["verification"], list)
            or not isinstance(manifest["createdAt"], str)
            or manifest["decisionStatus"]
            not in {"awaiting_approval", "not_required"}
            or not isinstance(manifest["sha256"], str)
        ):
            raise WorkspaceError("Artifact manifest contract is invalid.")
        for field in ("affectedPaths", "allowedPaths", "verification"):
            if not all(isinstance(value, str) for value in manifest[field]):
                raise WorkspaceError(
                    f"Artifact manifest {field} must contain strings."
                )
        expected_payload = (
            "payload.diff"
            if manifest["artifactType"] in MUTATING_ARTIFACT_TYPES
            else "payload.json"
            if manifest["artifactType"] == "review"
            else "payload.md"
        )
        if manifest["payload"] != expected_payload:
            raise WorkspaceError("Artifact payload filename is invalid.")
        if manifest["artifactType"] in MUTATING_ARTIFACT_TYPES:
            if (
                not isinstance(manifest["baseCommit"], str)
                or re.fullmatch(
                    r"(?:[0-9a-f]{40}|[0-9a-f]{64})",
                    manifest["baseCommit"],
                )
                is None
                or manifest["decisionStatus"] != "awaiting_approval"
            ):
                raise WorkspaceError(
                    "Mutating artifact lineage is invalid."
                )
        elif (
            manifest["baseCommit"] is not None
            or manifest["affectedPaths"]
            or manifest["decisionStatus"] != "not_required"
        ):
            raise WorkspaceError("Non-mutating artifact lineage is invalid.")
        payload_name = manifest["payload"]
        raw_payload_path = artifact_dir / payload_name
        if raw_payload_path.is_symlink():
            raise WorkspaceError("Artifact payload cannot be a symlink.")
        payload_path = raw_payload_path.resolve()
        if not _is_within(payload_path, artifact_dir.resolve()):
            raise WorkspaceError("Artifact payload path escapes its directory.")
        payload = payload_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WorkspaceError(f"Artifact not found for task {task_id}.") from exc
    except (KeyError, json.JSONDecodeError, OSError) as exc:
        raise WorkspaceError(f"Artifact for task {task_id} is invalid.") from exc
    digest = canonical_sha256({
        "taskId": manifest["taskId"],
        "artifactType": manifest["artifactType"],
        "baseCommit": manifest["baseCommit"],
        "payload": payload,
    })
    if (
        _SHA256.fullmatch(str(manifest["sha256"])) is None
        or manifest["sha256"] != digest
    ):
        raise WorkspaceError("Artifact payload or manifest digest is invalid.")
    return manifest, payload


def validate_patch(
    payload: str,
    *,
    task: TaskDef,
    workspace: dict[str, Any],
) -> list[str]:
    """Validate and preflight one text-only unified Git diff."""
    if task.artifact_type not in MUTATING_ARTIFACT_TYPES:
        raise WorkspaceError("Only patch and test-suite artifacts contain diffs.")
    worktree = _execution_path(workspace)
    paths = _strict_patch_paths(worktree, payload)

    root = Path(workspace["workspaceRoot"]).resolve()
    worktrees_root = Path(workspace["worktreesRoot"]).resolve()
    runtime_roots = [worktrees_root, worktrees_root.parent]
    for path in paths:
        if not any(_path_within(path, value) for value in task.allowed_paths):
            raise WorkspaceError(
                f"Diff path is outside task.allowedPaths: {path}"
            )
        if not any(
            _path_within(path, value)
            for value in workspace["writeRoots"]
        ):
            raise WorkspaceError(
                f"Diff path is outside workspace.writeRoots: {path}"
            )
        original_path = (root / path).resolve(strict=False)
        if any(_is_within(original_path, runtime_root) for runtime_root in runtime_roots):
            raise WorkspaceError(f"Diff path targets MLX Swarm runtime data: {path}")
        candidate = worktree / path
        _reject_symlink_components(worktree, candidate, path)

    result = _git(
        worktree,
        ["apply", "--check", "--index", "--recount", "-"],
        input_bytes=payload.encode("utf-8"),
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise WorkspaceError(f"git apply --check failed: {message}")
    return paths


def _strict_patch_paths(worktree: Path, payload: str) -> list[str]:
    """Parse every patch section without applying it."""
    for marker in _FORBIDDEN_DIFF_MARKERS:
        if marker in payload:
            raise WorkspaceError(f"Diff contains forbidden metadata: {marker}")
    if _SPECIAL_GIT_MODE.search(payload):
        raise WorkspaceError(
            "Diff cannot modify symlinks or Git submodules."
        )
    paths: list[str] = []
    for line in payload.splitlines():
        if not line.startswith("diff --git "):
            continue
        match = _DIFF_HEADER.fullmatch(line)
        if match is None:
            raise WorkspaceError(
                "Diff paths must use unquoted a/path and b/path headers."
            )
        old_path = _safe_patch_path(match.group(1))
        new_path = _safe_patch_path(match.group(2))
        if old_path != new_path:
            raise WorkspaceError("Rename and copy diffs are not supported.")
        paths.append(new_path)
    if not paths:
        raise WorkspaceError("Artifact does not contain a Git unified diff.")
    if len(set(paths)) != len(paths):
        raise WorkspaceError("Diff contains duplicate file sections.")
    parsed_paths = _git_patch_paths(worktree, payload)
    if parsed_paths != paths:
        raise WorkspaceError(
            "Diff contains patch sections not represented by strict "
            "diff --git headers."
        )
    return paths


def _git_patch_paths(worktree: Path, payload: str) -> list[str]:
    """Ask Git to enumerate every patch section, including headerless ones."""
    result = _git(
        worktree,
        ["apply", "--numstat", "-z", "--recount", "-"],
        input_bytes=payload.encode("utf-8"),
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise WorkspaceError(f"git apply could not parse the diff: {message}")
    paths: list[str] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            raise WorkspaceError("git apply returned invalid path metadata.")
        paths.append(
            _safe_patch_path(
                fields[2].decode("utf-8", "surrogateescape")
            )
        )
    return paths


def _validate_artifact_task_contract(
    manifest: dict[str, Any],
    payload: str,
    *,
    task: TaskDef,
    workspace: dict[str, Any],
) -> None:
    if (
        manifest.get("taskId") != task.id
        or manifest.get("artifactType") != task.artifact_type
        or manifest.get("allowedPaths") != list(task.allowed_paths)
        or manifest.get("verification") != list(task.verification)
    ):
        raise WorkspaceError(
            "Persisted artifact authority differs from the task snapshot."
        )
    if task.mutates_workspace:
        paths = _strict_patch_paths(_execution_path(workspace), payload)
        if manifest.get("affectedPaths") != paths:
            raise WorkspaceError(
                "Persisted artifact affected paths are invalid."
            )
    elif (
        manifest.get("affectedPaths") != []
        or manifest.get("baseCommit") is not None
    ):
        raise WorkspaceError("Non-mutating artifact manifest is invalid.")


def submit_artifact_decision(
    session_dir: Path,
    task_id: str,
    *,
    action: str,
    artifact_sha256: str,
    source: str,
    reason: str | None = None,
) -> dict[str, Any]:
    if action not in {"apply", "reject", "verify"}:
        raise WorkspaceError("Artifact action must be apply, reject, or verify.")
    if _SHA256.fullmatch(artifact_sha256) is None:
        raise WorkspaceError("Artifact digest must be lowercase SHA-256.")
    manifest, _ = load_artifact(session_dir, task_id)
    if manifest.get("sha256") != artifact_sha256:
        raise WorkspaceError("Artifact digest mismatch; refresh the preview.")
    artifact_dir = session_dir / "artifacts" / task_id
    if action in {"apply", "reject"}:
        initial_path = artifact_dir / "decision.json"
        if action == "reject" and initial_path.is_file():
            try:
                initial = json.loads(initial_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise WorkspaceError("Initial artifact decision is invalid.") from exc
            initial = _validate_decision(initial, task_id=task_id)
            if initial.get("action") != "apply":
                raise WorkspaceError("Artifact already has a terminal decision.")
            if not (artifact_dir / "apply-receipt.json").is_file():
                raise WorkspaceError(
                    "Artifact application is already sealed and still in "
                    "progress."
                )
            decision_path = artifact_dir / "rejection.json"
        else:
            decision_path = initial_path
    else:
        decision_path = (
            artifact_dir
            / f"verify-request-{uuid.uuid4().hex}.json"
        )
    decision = {
        "schemaVersion": DECISION_SCHEMA_VERSION,
        "taskId": task_id,
        "action": action,
        "artifactSha256": artifact_sha256,
        "source": source,
        "decidedAt": utc_now(),
    }
    try:
        workspace = load_workspace_snapshot(session_dir)
    except WorkspaceError:
        workspace = None
    if workspace is not None:
        policy_digest = workspace.get("executionPolicySha256")
        if isinstance(policy_digest, str):
            decision["executionPolicySha256"] = policy_digest
    if reason:
        decision["reason"] = reason[:4000]
    _exclusive_json(decision_path, decision)
    return decision


def read_initial_decision(session_dir: Path, task_id: str) -> dict[str, Any] | None:
    path = session_dir / "artifacts" / task_id / "decision.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise WorkspaceError("Artifact decision is invalid.") from exc
    return _validate_decision(value, task_id=task_id)


def read_failed_verification_action(
    session_dir: Path,
    task_id: str,
    processed_requests: list[str],
) -> tuple[str, dict[str, Any]] | None:
    artifact_dir = session_dir / "artifacts" / task_id
    rejection_path = artifact_dir / "rejection.json"
    if rejection_path.is_file() and "rejection.json" not in processed_requests:
        try:
            value = json.loads(rejection_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise WorkspaceError(
                "Artifact rejection decision is invalid."
            ) from exc
        return "rejection.json", _validate_decision(
            value,
            task_id=task_id,
            expected_action="reject",
        )
    for path in sorted(artifact_dir.glob("verify-request-*.json")):
        if path.name in processed_requests:
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise WorkspaceError("Verification request is invalid.") from exc
        return path.name, _validate_decision(
            value,
            task_id=task_id,
            expected_action="verify",
        )
    return None


def _validate_decision(
    value: Any,
    *,
    task_id: str,
    expected_action: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkspaceError("Artifact decision is invalid.")
    required = {
        "schemaVersion",
        "taskId",
        "action",
        "artifactSha256",
        "source",
        "decidedAt",
    }
    optional = {"reason", "executionPolicySha256"}
    if set(value) - required - optional or required - set(value):
        raise WorkspaceError("Artifact decision contract is invalid.")
    action = value.get("action")
    if (
        value.get("schemaVersion") != DECISION_SCHEMA_VERSION
        or value.get("taskId") != task_id
        or action not in {"apply", "reject", "verify"}
        or (
            expected_action is not None
            and action != expected_action
        )
        or not isinstance(value.get("source"), str)
        or not value["source"].strip()
        or not isinstance(value.get("decidedAt"), str)
        or not value["decidedAt"].strip()
        or _SHA256.fullmatch(str(value.get("artifactSha256"))) is None
    ):
        raise WorkspaceError("Artifact decision contract is invalid.")
    if "executionPolicySha256" in value and _SHA256.fullmatch(
        str(value["executionPolicySha256"])
    ) is None:
        raise WorkspaceError("Artifact decision contract is invalid.")
    if "reason" in value and not isinstance(value["reason"], str):
        raise WorkspaceError("Artifact decision contract is invalid.")
    return value


def _artifact_apply_head(
    manifest: dict[str, Any],
    workspace: dict[str, Any],
) -> str:
    """Permit rebasing only across earlier commits on disjoint owned paths."""
    worktree = _execution_path(workspace)
    actual_head = _workspace_head(workspace)
    base_commit = manifest.get("baseCommit")
    if actual_head == base_commit:
        return actual_head
    if not isinstance(base_commit, str):
        raise WorkspaceError("Artifact base commit is invalid.")
    ancestor = _git(
        worktree,
        ["merge-base", "--is-ancestor", base_commit, actual_head],
        check=False,
    )
    if ancestor.returncode != 0:
        raise WorkspaceError(
            "Session worktree HEAD is not a descendant of the artifact base."
        )
    affected = manifest.get("affectedPaths")
    if not isinstance(affected, list) or not all(
        isinstance(path, str) for path in affected
    ):
        raise WorkspaceError("Artifact affected paths are invalid.")
    changed = _nul_paths(
        _git(
            worktree,
            [
                "diff",
                "--name-only",
                "-z",
                base_commit,
                actual_head,
                "--",
                *affected,
            ],
        ).stdout
    )
    if changed:
        raise WorkspaceError(
            "A prior artifact changed this task's owned paths: "
            + ", ".join(changed)
        )
    return actual_head


def apply_artifact(
    session_dir: Path,
    task: TaskDef,
    workspace: dict[str, Any],
    *,
    expected_artifact_sha256: str,
) -> dict[str, Any]:
    manifest, payload = load_artifact(session_dir, task.id)
    if manifest["sha256"] != expected_artifact_sha256:
        raise WorkspaceError(
            "Persisted artifact differs from the approved digest."
        )
    _validate_artifact_task_contract(
        manifest,
        payload,
        task=task,
        workspace=workspace,
    )
    if manifest.get("decisionStatus") not in {"awaiting_approval", "applying"}:
        raise WorkspaceError("Artifact is not awaiting application.")
    worktree = _execution_path(workspace)
    expected_head = _artifact_apply_head(manifest, workspace)
    _require_clean_tracked_worktree(worktree)
    validate_patch(payload, task=task, workspace=workspace)
    _git(
        worktree,
        ["apply", "--index", "--recount", "-"],
        input_bytes=payload.encode("utf-8"),
    )
    return _commit_applied_artifact(
        session_dir,
        task,
        workspace,
        manifest,
        expected_head=str(expected_head),
        payload=payload,
    )


def recover_artifact_application(
    session_dir: Path,
    task: TaskDef,
    workspace: dict[str, Any],
    *,
    expected_artifact_sha256: str,
) -> dict[str, Any]:
    """Reconcile a crash around git apply/commit without applying twice."""
    manifest, payload = load_artifact(session_dir, task.id)
    if manifest["sha256"] != expected_artifact_sha256:
        raise WorkspaceError(
            "Persisted artifact differs from the approved digest."
        )
    _validate_artifact_task_contract(
        manifest,
        payload,
        task=task,
        workspace=workspace,
    )
    worktree = _execution_path(workspace)
    _require_approved_branch(workspace, worktree)
    actual_head = _git_text(worktree, ["rev-parse", "HEAD"])
    base_commit = manifest.get("baseCommit")
    receipt_path = (
        session_dir / "artifacts" / task.id / "apply-receipt.json"
    )
    if receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise WorkspaceError("Applied artifact receipt is invalid.") from exc
        receipt = _validate_apply_receipt(
            receipt,
            task_id=task.id,
            artifact_sha256=str(manifest.get("sha256")),
        )
        if receipt.get("commitSha") != actual_head:
            raise WorkspaceError(
                "Applied artifact receipt does not match the worktree."
            )
        _require_clean_tracked_worktree(worktree)
        workspace["headSha"] = actual_head
        return {"state": "applied", "receipt": receipt}

    changes = _worktree_status_entries(worktree)
    if changes:
        application_head = _artifact_apply_head(manifest, workspace)
        staged_paths = _nul_paths(
            _git(
                worktree,
                ["diff", "--cached", "--name-only", "-z"],
            ).stdout
        )
        unstaged_paths = _nul_paths(
            _git(
                worktree,
                ["diff", "--name-only", "-z"],
            ).stdout
        )
        untracked_paths = _nul_paths(
            _git(
                worktree,
                ["ls-files", "--others", "--exclude-standard", "-z"],
            ).stdout
        )
        reverse_check = _git(
            worktree,
            ["apply", "--reverse", "--check", "--cached", "--recount", "-"],
            input_bytes=payload.encode("utf-8"),
            check=False,
        )
        expected_tree = _expected_patched_tree(
            session_dir,
            worktree,
            application_head,
            payload,
        )
        staged_tree = _git_text(worktree, ["write-tree"])
        if (
            staged_paths == sorted(manifest.get("affectedPaths", []))
            and not unstaged_paths
            and not untracked_paths
            and reverse_check.returncode == 0
            and staged_tree == expected_tree
        ):
            receipt = _commit_applied_artifact(
                session_dir,
                task,
                workspace,
                manifest,
                expected_head=application_head,
                payload=payload,
                recovered_after_crash=True,
            )
            return {"state": "applied", "receipt": receipt}
        raise WorkspaceError(
            "Interrupted artifact left workspace changes that do not exactly "
            "match the sealed patch."
        )

    if actual_head == base_commit:
        workspace["headSha"] = actual_head
        return {"state": "not_applied", "receipt": None}

    parent = _git_text(worktree, ["rev-parse", f"{actual_head}^"])
    changed_result = _git(
        worktree,
        [
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            actual_head,
        ],
    )
    changed_paths = sorted(
        value.decode("utf-8", "surrogateescape")
        for value in changed_result.stdout.split(b"\0")
        if value
    )
    if (
        _git(
            worktree,
            ["merge-base", "--is-ancestor", str(base_commit), parent],
            check=False,
        ).returncode != 0
        or changed_paths != sorted(manifest.get("affectedPaths", []))
        or _git_text(
            worktree,
            ["rev-parse", f"{actual_head}^{{tree}}"],
        )
        != _expected_patched_tree(
            session_dir,
            worktree,
            parent,
            payload,
        )
    ):
        try:
            _artifact_apply_head(manifest, workspace)
        except WorkspaceError as exc:
            raise WorkspaceError(
                "Worktree lineage changed during artifact application."
            ) from exc
        workspace["headSha"] = actual_head
        return {"state": "not_applied", "receipt": None}
    _require_clean_tracked_worktree(worktree)
    receipt = {
        "schemaVersion": DECISION_SCHEMA_VERSION,
        "taskId": task.id,
        "artifactSha256": manifest["sha256"],
        "baseCommit": parent,
        "commitSha": actual_head,
        "appliedAt": utc_now(),
        "recoveredAfterCrash": True,
    }
    _exclusive_json(receipt_path, receipt)
    workspace["headSha"] = actual_head
    return {"state": "applied", "receipt": receipt}


def _expected_patched_tree(
    session_dir: Path,
    worktree: Path,
    base_commit: str,
    payload: str,
) -> str:
    """Materialize the sealed patch in a temporary index and return its tree."""
    runtime = session_dir / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    index_path = runtime / f".recovery-index-{uuid.uuid4().hex}"
    try:
        _git(
            worktree,
            ["read-tree", base_commit],
            index_file=index_path,
        )
        _git(
            worktree,
            ["apply", "--cached", "--recount", "-"],
            input_bytes=payload.encode("utf-8"),
            index_file=index_path,
        )
        return _git(
            worktree,
            ["write-tree"],
            index_file=index_path,
        ).stdout.decode("utf-8", "replace").strip()
    finally:
        index_path.unlink(missing_ok=True)


def _commit_applied_artifact(
    session_dir: Path,
    task: TaskDef,
    workspace: dict[str, Any],
    manifest: dict[str, Any],
    *,
    expected_head: str,
    payload: str,
    recovered_after_crash: bool = False,
) -> dict[str, Any]:
    worktree = _execution_path(workspace)
    _require_approved_branch(workspace, worktree)
    if _git_text(worktree, ["rev-parse", "HEAD"]) != expected_head:
        raise WorkspaceError(
            "Workspace HEAD changed before the sealed artifact commit."
        )
    expected_tree = _expected_patched_tree(
        session_dir,
        worktree,
        expected_head,
        payload,
    )
    if _git_text(worktree, ["write-tree"]) != expected_tree:
        raise WorkspaceError(
            "Git index contains changes outside the sealed artifact."
        )
    commit_sha = _git_text(
        worktree,
        [
            "-c",
            "user.name=MLX Swarm",
            "-c",
            "user.email=mlx-swarm@localhost",
            "commit-tree",
            expected_tree,
            "-p",
            expected_head,
            "-m",
            f"mlx-swarm: {task.id} ({task.artifact_type})",
        ],
    )
    _git(
        worktree,
        ["update-ref", "HEAD", commit_sha, expected_head],
    )
    if _git_text(worktree, ["rev-parse", "HEAD"]) != commit_sha:
        raise WorkspaceError("Sealed artifact commit did not become HEAD.")
    receipt = {
        "schemaVersion": DECISION_SCHEMA_VERSION,
        "taskId": task.id,
        "artifactSha256": manifest["sha256"],
        "baseCommit": expected_head,
        "commitSha": commit_sha,
        "appliedAt": utc_now(),
    }
    if recovered_after_crash:
        receipt["recoveredAfterCrash"] = True
    _exclusive_json(
        session_dir / "artifacts" / task.id / "apply-receipt.json",
        receipt,
    )
    workspace["headSha"] = commit_sha
    return receipt


def _validate_apply_receipt(
    value: Any,
    *,
    task_id: str,
    artifact_sha256: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkspaceError("Applied artifact receipt is invalid.")
    required = {
        "schemaVersion",
        "taskId",
        "artifactSha256",
        "baseCommit",
        "commitSha",
        "appliedAt",
    }
    optional = {"recoveredAfterCrash"}
    if set(value) - required - optional or required - set(value):
        raise WorkspaceError("Applied artifact receipt is invalid.")
    if (
        value.get("schemaVersion") != DECISION_SCHEMA_VERSION
        or value.get("taskId") != task_id
        or value.get("artifactSha256") != artifact_sha256
        or _GIT_OID.fullmatch(str(value.get("baseCommit"))) is None
        or _GIT_OID.fullmatch(str(value.get("commitSha"))) is None
        or not isinstance(value.get("appliedAt"), str)
        or not value["appliedAt"].strip()
        or (
            "recoveredAfterCrash" in value
            and value["recoveredAfterCrash"] is not True
        )
    ):
        raise WorkspaceError("Applied artifact receipt is invalid.")
    return value


def run_verifications(
    session_dir: Path,
    task: TaskDef,
    workspace: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run only profiles snapshotted into the approved execution contract."""
    results: list[dict[str, Any]] = []
    for profile_id in task.verification:
        execution_path = _execution_path(workspace)
        _workspace_head(workspace)
        _require_clean_tracked_worktree(execution_path)
        raw_profile = workspace["verificationProfiles"].get(profile_id)
        if not isinstance(raw_profile, dict):
            raise WorkspaceError(
                f"Verification profile is absent from the snapshot: {profile_id}"
            )
        results.append(
            _run_verification_profile(
                session_dir,
                task.id,
                profile_id,
                raw_profile,
                workspace,
            )
        )
        if not results[-1]["passed"]:
            break
    return results


def load_completed_artifact_evidence(
    session_dir: Path,
    task: TaskDef,
    workspace: dict[str, Any],
) -> dict[str, Any]:
    """Reload and bind immutable completed-artifact evidence from disk."""
    manifest, payload = load_artifact(session_dir, task.id)
    _validate_artifact_task_contract(
        manifest,
        payload,
        task=task,
        workspace=workspace,
    )
    artifact_dir = session_dir / "artifacts" / task.id
    apply_path = artifact_dir / "apply-receipt.json"
    if (
        apply_path.is_symlink()
        or not apply_path.is_file()
        or not _is_within(
            apply_path.resolve(),
            session_dir.resolve(),
        )
    ):
        raise WorkspaceError(
            "Completed artifact apply receipt is invalid."
        )
    try:
        apply_raw = json.loads(
            apply_path.read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise WorkspaceError(
            "Completed artifact apply receipt is invalid."
        ) from exc
    apply_receipt = _validate_apply_receipt(
        apply_raw,
        task_id=task.id,
        artifact_sha256=manifest["sha256"],
    )
    verification_receipts: list[dict[str, Any]] = []
    verification_digests: list[str] = []
    for profile_id in task.verification:
        profile = workspace["verificationProfiles"].get(profile_id)
        if not isinstance(profile, dict):
            raise WorkspaceError(
                "Completed artifact verification profile is missing."
            )
        attempt_dir = artifact_dir / "verification" / profile_id
        paths = sorted(attempt_dir.glob("attempt-*.json"))
        if not paths:
            raise WorkspaceError(
                f"Completed artifact has no verification for {profile_id}."
            )
        for path in paths:
            if (
                path.is_symlink()
                or not path.is_file()
                or not _is_within(
                    path.resolve(),
                    session_dir.resolve(),
                )
            ):
                raise WorkspaceError(
                    "Completed artifact verification receipt is invalid."
                )
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise WorkspaceError(
                    "Completed artifact verification receipt is invalid."
                ) from exc
            receipt = _validate_verification_receipt(
                receipt,
                receipt_path=path,
                session_dir=session_dir,
                task_id=task.id,
                profile_id=profile_id,
                profile=profile,
            )
            verification_receipts.append(receipt)
            verification_digests.append(canonical_sha256(receipt))
        if not any(
            receipt.get("profileId") == profile_id
            and receipt.get("passed") is True
            for receipt in verification_receipts
        ):
            raise WorkspaceError(
                f"Completed artifact has no passing verification for "
                f"{profile_id}."
            )
    return {
        "manifest": manifest,
        "payload": payload,
        "payloadSha256": hashlib.sha256(
            payload.encode("utf-8")
        ).hexdigest(),
        "applyReceipt": apply_receipt,
        "applyReceiptSha256": canonical_sha256(apply_receipt),
        "verificationReceipts": verification_receipts,
        "verificationReceiptSha256": verification_digests,
    }


def load_completed_non_mutating_artifact_evidence(
    session_dir: Path,
    task: TaskDef,
    workspace: dict[str, Any],
) -> dict[str, Any]:
    """Reload a completed review/report from its sealed artifact files."""
    if task.mutates_workspace:
        raise WorkspaceError(
            "Mutating tasks require applied-artifact evidence."
        )
    manifest, payload = load_artifact(session_dir, task.id)
    _validate_artifact_task_contract(
        manifest,
        payload,
        task=task,
        workspace=workspace,
    )
    return {
        "manifest": manifest,
        "manifestSha256": canonical_sha256(manifest),
        "artifactSha256": manifest["sha256"],
        "payload": payload,
        "payloadSha256": hashlib.sha256(
            payload.encode("utf-8")
        ).hexdigest(),
    }


def revision_base_evidence(
    session_dir: Path,
    plan: Plan,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Validate one terminal worktree as an incremental revision base."""
    if state.get("status") not in {"completed", "partial", "failed"}:
        raise WorkspaceError(
            "Incremental revisions require a terminal predecessor session."
        )
    workspace = load_workspace_snapshot(session_dir)
    validate_execution_snapshot(
        workspace,
        plan=plan,
        approval=state.get("executionApproval"),
        session_id=str(state.get("sessionId", "")),
    )
    if _workspace_target(workspace) != "worktree":
        raise WorkspaceError(
            "Incremental carry-forward currently requires an isolated "
            "worktree predecessor."
        )
    worktree = _execution_path(workspace)
    if not worktree.is_dir() or workspace.get("cleanedUp"):
        raise WorkspaceError(
            "Incremental carry-forward requires the retained predecessor "
            "worktree."
        )
    head_sha = _workspace_head(workspace)
    _require_clean_tracked_worktree(worktree)
    root = Path(workspace["workspaceRoot"]).resolve()
    branch = str(workspace.get("branch", ""))
    branch_head = _git_text(
        root,
        ["rev-parse", f"refs/heads/{branch}"],
    )
    if branch_head != head_sha:
        raise WorkspaceError(
            "Predecessor branch differs from its validated workspace head."
        )

    carried_tasks: list[dict[str, Any]] = []
    unfinished_subgraph: list[dict[str, Any]] = []
    task_states = state.get("tasks")
    if not isinstance(task_states, dict):
        raise WorkspaceError("Predecessor task state is invalid.")
    for task in plan.tasks:
        raw_state = task_states.get(task.id)
        if not isinstance(raw_state, dict):
            raise WorkspaceError(
                f"Predecessor task state is missing for {task.id}."
            )
        status = raw_state.get("status")
        artifact_dir = session_dir / "artifacts" / task.id
        if status == "completed":
            if task.mutates_workspace:
                evidence = load_completed_artifact_evidence(
                    session_dir,
                    task,
                    workspace,
                )
                commit_sha = evidence["applyReceipt"].get("commitSha")
                if not isinstance(commit_sha, str):
                    raise WorkspaceError(
                        f"Completed task {task.id} has no commit evidence."
                    )
                ancestor = _git(
                    worktree,
                    ["merge-base", "--is-ancestor", commit_sha, head_sha],
                    check=False,
                )
                if ancestor.returncode != 0:
                    raise WorkspaceError(
                        f"Completed task {task.id} is not present in the "
                        "predecessor head."
                    )
                payload = evidence["payload"]
                diff_truncated = (
                    len(payload) > MAX_CARRIED_MUTATING_DIFF_CHARS
                )
                if diff_truncated:
                    half = MAX_CARRIED_MUTATING_DIFF_CHARS // 2
                    diff_excerpt = (
                        payload[:half]
                        + "\n...[bounded carried diff omitted]...\n"
                        + payload[-half:]
                    )
                else:
                    diff_excerpt = payload
                carried_tasks.append({
                    "taskId": task.id,
                    "artifactType": task.artifact_type,
                    "affectedPaths": list(
                        evidence["manifest"]["affectedPaths"]
                    ),
                    "artifactSha256": evidence["manifest"]["sha256"],
                    "commitSha": commit_sha,
                    "applyReceiptSha256": evidence[
                        "applyReceiptSha256"
                    ],
                    "verificationReceiptSha256": evidence[
                        "verificationReceiptSha256"
                    ],
                    "diffSha256": evidence["payloadSha256"],
                    "diffExcerpt": diff_excerpt,
                    "diffTruncated": diff_truncated,
                })
            else:
                evidence = load_completed_non_mutating_artifact_evidence(
                    session_dir,
                    task,
                    workspace,
                )
                payload = evidence["payload"]
                output_truncated = (
                    len(payload) > MAX_CARRIED_NON_MUTATING_CHARS
                )
                if output_truncated:
                    half = MAX_CARRIED_NON_MUTATING_CHARS // 2
                    output_excerpt = (
                        payload[:half]
                        + "\n...[bounded carried output omitted]...\n"
                        + payload[-half:]
                    )
                else:
                    output_excerpt = payload
                carried_tasks.append({
                    "taskId": task.id,
                    "artifactType": task.artifact_type,
                    "affectedPaths": [],
                    "artifactSha256": evidence["artifactSha256"],
                    "manifestSha256": evidence["manifestSha256"],
                    "payloadSha256": evidence["payloadSha256"],
                    "outputExcerpt": output_excerpt,
                    "outputTruncated": output_truncated,
                })
            continue

        if (
            (artifact_dir / "apply-receipt.json").is_file()
            and not (artifact_dir / "revert-receipt.json").is_file()
        ):
            raise WorkspaceError(
                f"Unfinished task {task.id} has an unresolved applied commit."
            )
        gate = raw_state.get("gateResult")
        violations = (
            [
                str(value.get("id"))
                for value in gate.get("violations", [])
                if isinstance(value, dict) and value.get("id") is not None
            ]
            if isinstance(gate, dict)
            else []
        )
        unfinished_subgraph.append({
            "taskId": task.id,
            "status": status,
            "role": task.role,
            "artifactType": task.artifact_type,
            "dependsOn": list(task.depends_on),
            "allowedPaths": list(task.allowed_paths),
            "verification": list(task.verification),
            "violationIds": violations,
            "error": raw_state.get("error"),
        })

    return {
        "workspace": workspace,
        "workspaceSnapshotSha256": canonical_sha256(workspace),
        "baseSha": head_sha,
        "predecessorBranch": branch,
        "inspectionRoot": str(worktree),
        "predecessorExecutionDigest": workspace["executionDigest"],
        "carriedTasks": carried_tasks,
        "unfinishedSubgraph": unfinished_subgraph,
    }


def validate_revision_inspection_root(
    inspection_root: Path,
    *,
    predecessor_branch: str,
    base_sha: str,
) -> Path:
    """Revalidate the exact clean predecessor tree exposed to planning."""
    root = inspection_root.resolve()
    if not root.is_dir():
        raise WorkspaceError(
            "Incremental revision inspection worktree is unavailable."
        )
    if _current_branch(root) != predecessor_branch:
        raise WorkspaceError(
            "Incremental revision inspection branch changed after "
            "evidence was frozen."
        )
    if _git_text(root, ["rev-parse", "HEAD"]) != base_sha:
        raise WorkspaceError(
            "Incremental revision inspection head changed after evidence "
            "was frozen."
        )
    _require_clean_tracked_worktree(root)
    return root


def _validate_verification_receipt(
    value: Any,
    *,
    receipt_path: Path,
    session_dir: Path,
    task_id: str,
    profile_id: str,
    profile: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkspaceError(
            "Completed artifact verification receipt is invalid."
        )
    common_fields = {
        "schemaVersion",
        "taskId",
        "profileId",
        "attempt",
        "argv",
        "cwd",
        "timeoutSeconds",
        "startedAt",
        "finishedAt",
        "elapsedSeconds",
        "exitCode",
        "timedOut",
        "output",
        "outputTruncated",
        "trackedChangesRejected",
        "trackedChangesRestored",
        "workspaceChanges",
        "remainingWorkspaceChanges",
        "lineageError",
        "processGroupCleanupError",
        "passed",
    }
    version = value.get("schemaVersion")
    expected_fields = set(common_fields)
    if version == VERIFICATION_RECEIPT_SCHEMA_VERSION:
        expected_fields.update({"outputSha256", "outputBytes"})
    elif version != 1:
        raise WorkspaceError(
            "Completed artifact verification receipt is invalid."
        )
    if set(value) != expected_fields:
        raise WorkspaceError(
            "Completed artifact verification receipt is invalid."
        )

    attempt_match = re.fullmatch(
        r"attempt-([0-9]{3,})\.json",
        receipt_path.name,
    )
    attempt = value.get("attempt")
    list_fields = (
        "trackedChangesRejected",
        "trackedChangesRestored",
        "workspaceChanges",
        "remainingWorkspaceChanges",
    )
    error_fields = ("lineageError", "processGroupCleanupError")
    elapsed = value.get("elapsedSeconds")
    exit_code = value.get("exitCode")
    if (
        attempt_match is None
        or not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or attempt <= 0
        or int(attempt_match.group(1)) != attempt
        or value.get("taskId") != task_id
        or value.get("profileId") != profile_id
        or value.get("argv") != profile.get("argv")
        or value.get("cwd") != profile.get("cwd")
        or value.get("timeoutSeconds") != profile.get("timeoutSeconds")
        or not isinstance(value.get("startedAt"), str)
        or not value["startedAt"].strip()
        or not isinstance(value.get("finishedAt"), str)
        or not value["finishedAt"].strip()
        or not isinstance(elapsed, (int, float))
        or isinstance(elapsed, bool)
        or elapsed < 0
        or not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not isinstance(value.get("timedOut"), bool)
        or not isinstance(value.get("output"), str)
        or not isinstance(value.get("outputTruncated"), bool)
        or not isinstance(value.get("passed"), bool)
        or any(
            not isinstance(value.get(field), list)
            or not all(
                isinstance(item, str) for item in value[field]
            )
            for field in list_fields
        )
        or any(
            value.get(field) is not None
            and not isinstance(value.get(field), str)
            for field in error_fields
        )
    ):
        raise WorkspaceError(
            "Completed artifact verification receipt is invalid."
        )

    expected_output = str(
        receipt_path.with_suffix(".log").relative_to(session_dir)
    )
    if value["output"] != expected_output:
        raise WorkspaceError(
            "Completed artifact verification receipt is invalid."
        )
    log_path = session_dir / value["output"]
    if (
        log_path.is_symlink()
        or not log_path.is_file()
        or not _is_within(log_path.resolve(), session_dir.resolve())
    ):
        raise WorkspaceError(
            "Completed artifact verification log is invalid."
        )

    expected_passed = (
        exit_code == 0
        and value["timedOut"] is False
        and not value["workspaceChanges"]
        and not value["remainingWorkspaceChanges"]
        and value["lineageError"] is None
        and value["processGroupCleanupError"] is None
    )
    if value["passed"] is not expected_passed:
        raise WorkspaceError(
            "Completed artifact verification result is inconsistent."
        )

    if version == VERIFICATION_RECEIPT_SCHEMA_VERSION:
        output_bytes = value.get("outputBytes")
        output_sha256 = value.get("outputSha256")
        if (
            not isinstance(output_bytes, int)
            or isinstance(output_bytes, bool)
            or not 0 <= output_bytes <= MAX_COMMAND_OUTPUT_BYTES
            or _SHA256.fullmatch(str(output_sha256)) is None
        ):
            raise WorkspaceError(
                "Completed artifact verification log binding is invalid."
            )
        try:
            raw_log = log_path.read_bytes()
        except OSError as exc:
            raise WorkspaceError(
                "Completed artifact verification log is invalid."
            ) from exc
        if (
            len(raw_log) != output_bytes
            or hashlib.sha256(raw_log).hexdigest() != output_sha256
        ):
            raise WorkspaceError(
                "Completed artifact verification log binding is invalid."
            )
    return value


def revert_applied_artifact(
    session_dir: Path,
    task: TaskDef,
    workspace: dict[str, Any],
    *,
    expected_artifact_sha256: str,
) -> dict[str, Any]:
    task_id = task.id
    artifact_dir = session_dir / "artifacts" / task_id
    try:
        apply_receipt = json.loads(
            (artifact_dir / "apply-receipt.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise WorkspaceError("Applied artifact receipt is missing.") from exc
    manifest, payload = load_artifact(session_dir, task_id)
    if manifest["sha256"] != expected_artifact_sha256:
        raise WorkspaceError(
            "Persisted artifact differs from the approved digest."
        )
    _validate_artifact_task_contract(
        manifest,
        payload,
        task=task,
        workspace=workspace,
    )
    apply_receipt = _validate_apply_receipt(
        apply_receipt,
        task_id=task_id,
        artifact_sha256=manifest["sha256"],
    )
    worktree = _execution_path(workspace)
    _require_approved_branch(workspace, worktree)
    commit_sha = apply_receipt.get("commitSha")
    revert_path = artifact_dir / "revert-receipt.json"
    actual_head = _git_text(worktree, ["rev-parse", "HEAD"])
    if revert_path.is_file():
        try:
            receipt = json.loads(revert_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise WorkspaceError("Artifact revert receipt is invalid.") from exc
        receipt = _validate_revert_receipt(
            receipt,
            task_id=task_id,
            reverted_commit=str(commit_sha),
        )
        if receipt.get("revertCommit") != actual_head:
            raise WorkspaceError(
                "Artifact revert receipt does not match the workspace."
            )
        _require_clean_tracked_worktree(worktree)
        workspace["headSha"] = actual_head
        return receipt

    if actual_head == commit_sha:
        _require_clean_tracked_worktree(worktree)
        _git(
            worktree,
            [
                "-c",
                "user.name=MLX Swarm",
                "-c",
                "user.email=mlx-swarm@localhost",
                "revert",
                "--no-edit",
                "--no-gpg-sign",
                str(commit_sha),
            ],
        )
        revert_sha = _git_text(worktree, ["rev-parse", "HEAD"])
        recovered = False
    else:
        parent = _git_text(worktree, ["rev-parse", f"{actual_head}^"])
        base_commit = apply_receipt.get("baseCommit")
        same_as_base = _git(
            worktree,
            ["diff", "--quiet", str(base_commit), actual_head],
            check=False,
        ).returncode == 0
        changed_paths = _nul_paths(
            _git(
                worktree,
                [
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "-z",
                    actual_head,
                ],
            ).stdout
        )
        if (
            parent != commit_sha
            or not same_as_base
            or changed_paths != sorted(manifest.get("affectedPaths", []))
        ):
            raise WorkspaceError(
                "Only the current failed artifact commit can be rejected."
            )
        _require_clean_tracked_worktree(worktree)
        revert_sha = actual_head
        recovered = True
    receipt = {
        "schemaVersion": DECISION_SCHEMA_VERSION,
        "taskId": task_id,
        "revertedCommit": commit_sha,
        "revertCommit": revert_sha,
        "revertedAt": utc_now(),
    }
    if recovered:
        receipt["recoveredAfterCrash"] = True
    _exclusive_json(revert_path, receipt)
    workspace["headSha"] = revert_sha
    return receipt


def archive_artifact_attempt(
    session_dir: Path,
    task_id: str,
    *,
    attempt: int,
    reason: str,
) -> dict[str, Any]:
    """Move one resolved artifact aside before a bounded replacement attempt."""
    if attempt < 1:
        raise WorkspaceError("Artifact archive attempt must be positive.")
    source = session_dir / "artifacts" / task_id
    if source.is_symlink() or not source.is_dir():
        raise WorkspaceError("Artifact archive source is invalid.")
    destination = (
        session_dir
        / "artifact-history"
        / task_id
        / f"attempt-{attempt:03d}"
    )
    if destination.exists():
        raise WorkspaceError("Artifact archive destination already exists.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    digest = hashlib.sha256()
    files: list[str] = []
    for path in sorted(
        item
        for item in destination.rglob("*")
        if item.is_file() and not item.is_symlink()
    ):
        relative = path.relative_to(destination).as_posix()
        files.append(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    record = {
        "schemaVersion": 1,
        "taskId": task_id,
        "attempt": attempt,
        "reason": reason,
        "path": str(destination.relative_to(session_dir)),
        "files": files,
        "sha256": digest.hexdigest(),
        "archivedAt": utc_now(),
    }
    _exclusive_json(destination / "archive-receipt.json", record)
    return record


def _validate_revert_receipt(
    value: Any,
    *,
    task_id: str,
    reverted_commit: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkspaceError("Artifact revert receipt is invalid.")
    required = {
        "schemaVersion",
        "taskId",
        "revertedCommit",
        "revertCommit",
        "revertedAt",
    }
    optional = {"recoveredAfterCrash"}
    if set(value) - required - optional or required - set(value):
        raise WorkspaceError("Artifact revert receipt is invalid.")
    if (
        value.get("schemaVersion") != DECISION_SCHEMA_VERSION
        or value.get("taskId") != task_id
        or value.get("revertedCommit") != reverted_commit
        or _GIT_OID.fullmatch(str(value.get("revertCommit"))) is None
        or not isinstance(value.get("revertedAt"), str)
        or not value["revertedAt"].strip()
        or (
            "recoveredAfterCrash" in value
            and value["recoveredAfterCrash"] is not True
        )
    ):
        raise WorkspaceError("Artifact revert receipt is invalid.")
    return value


def final_workspace_diff(workspace: dict[str, Any]) -> tuple[str, str]:
    worktree = _execution_path(workspace)
    _workspace_head(workspace)
    _require_clean_tracked_worktree(worktree)
    diff = _git_text(
        worktree,
        [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            f"{workspace['baseSha']}..{workspace['headSha']}",
            "--",
        ],
        strip=False,
    )
    return diff, hashlib.sha256(diff.encode("utf-8")).hexdigest()


def cleanup_worktree(
    workspace: dict[str, Any],
    *,
    task_states: dict[str, Any] | None = None,
    pause_reason: str | None = None,
) -> None:
    if _workspace_target(workspace) != "worktree":
        raise WorkspaceError(
            "The main checkout cannot be removed by workspace cleanup."
        )
    recoverable = {
        "awaiting_approval",
        "applying",
        "verifying",
        "verification_failed",
    }
    if task_states is not None and any(
        isinstance(value, dict)
        and value.get("status") in recoverable
        for value in task_states.values()
    ):
        raise WorkspaceError(
            "Resumable workspace work must be resolved before cleanup."
        )
    if pause_reason == "finalization_validation_failed":
        raise WorkspaceError(
            "Final evidence must be repaired and resumed before cleanup."
        )
    root = Path(workspace["workspaceRoot"]).resolve()
    worktree = _execution_path(workspace)
    expected_root = Path(workspace["worktreesRoot"]).resolve()
    if not _is_within(worktree, expected_root):
        raise WorkspaceError("Worktree cleanup target escapes its runtime root.")
    if not worktree.exists():
        registered = _registered_worktrees(root)
        if worktree not in registered:
            workspace["cleanedUp"] = True
            workspace["cleanedUpAt"] = utc_now()
            workspace["cleanupRecoveredAfterCrash"] = True
            return
        raise WorkspaceError(
            "Git still registers the missing session worktree."
        )
    _workspace_head(workspace)
    if _dirty_entries(worktree):
        raise WorkspaceError(
            "Worktree cleanup refuses local staged, unstaged, or untracked "
            "changes."
        )
    _git(root, ["worktree", "remove", str(worktree)])
    workspace["cleanedUp"] = True
    workspace["cleanedUpAt"] = utc_now()


def _registered_worktrees(root: Path) -> set[Path]:
    result = _git(root, ["worktree", "list", "--porcelain"])
    registered: set[Path] = set()
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("worktree "):
            registered.add(Path(line.removeprefix("worktree ")).resolve())
    return registered


def cleanup_session_worktree(
    session_dir: Path,
    workspace: dict[str, Any],
    *,
    task_states: dict[str, Any] | None = None,
    pause_reason: str | None = None,
) -> None:
    """Serialize cleanup against the same durable runner lock as execution."""
    lock_path = session_dir.resolve() / "runner.lock"
    handle = lock_path.open("a+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkspaceError(
                "The session runner is active; workspace cleanup is refused."
            ) from exc
        cleanup_worktree(
            workspace,
            task_states=task_states,
            pause_reason=pause_reason,
        )
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _run_verification_profile(
    session_dir: Path,
    task_id: str,
    profile_id: str,
    profile: dict[str, Any],
    workspace: dict[str, Any],
) -> dict[str, Any]:
    worktree = _execution_path(workspace)
    cwd = (worktree / str(profile["cwd"])).resolve()
    if not _is_within(cwd, worktree) or not cwd.is_dir():
        raise WorkspaceError(
            f"Verification cwd escapes or is missing: {profile['cwd']}"
        )
    _reject_symlink_components(worktree, cwd, str(profile["cwd"]))
    attempt_root = (
        session_dir / "artifacts" / task_id / "verification" / profile_id
    )
    attempt_root.mkdir(parents=True, exist_ok=True)
    attempt = len(list(attempt_root.glob("attempt-*.json"))) + 1
    stem = f"attempt-{attempt:03d}"
    log_path = attempt_root / f"{stem}.log"
    env: dict[str, str] = {}
    for name in profile.get("inheritEnv", []):
        if name in os.environ:
            env[name] = os.environ[name]
    env.update({
        str(key): str(value)
        for key, value in profile.get("environment", {}).items()
    })
    runtime_home = session_dir / "runtime-home"
    runtime_tmp = session_dir / "runtime-tmp"
    runtime_home.mkdir(exist_ok=True)
    runtime_tmp.mkdir(exist_ok=True)
    env["HOME"] = str(runtime_home.resolve())
    env["TMPDIR"] = str(runtime_tmp.resolve())
    env["MLX_SWARM_SESSION_ID"] = session_dir.name
    env["MLX_SWARM_WORKSPACE"] = str(worktree)
    argv = [str(value) for value in profile["argv"]]
    started_at = utc_now()
    started = time.perf_counter()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        start_new_session=True,
        close_fds=True,
    )
    truncated = [False]
    assert process.stdout is not None
    with log_path.open("wb") as log_file:
        reader = threading.Thread(
            target=_bounded_copy,
            args=(process.stdout, log_file, MAX_COMMAND_OUTPUT_BYTES, truncated),
            daemon=True,
        )
        reader.start()
        timed_out = False
        try:
            return_code = process.wait(timeout=int(profile["timeoutSeconds"]))
        except subprocess.TimeoutExpired:
            timed_out = True
            _signal_process_group(process.pid, signal.SIGTERM)
            try:
                return_code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _signal_process_group(process.pid, signal.SIGKILL)
                return_code = process.wait()
        # The leader can exit successfully while background children retain
        # stdout or continue mutating the workspace. End the entire isolated
        # process group before checking Git state or releasing any lease.
        _signal_process_group(process.pid, signal.SIGTERM)
        reader.join(timeout=0.5)
        if _process_group_exists(process.pid):
            _signal_process_group(process.pid, signal.SIGKILL)
        reader.join(timeout=2)
        group_cleanup_error = (
            "Verification subprocess output did not close after process-group "
            "termination."
            if reader.is_alive()
            else None
        )
        if reader.is_alive():
            process.stdout.close()
    workspace_changes = _worktree_status_entries(worktree)
    tracked_changes = _tracked_changes(worktree)
    restored_tracked_changes: list[str] = []
    if tracked_changes and _workspace_target(workspace) == "worktree":
        _git(
            worktree,
            [
                "restore",
                "--source=HEAD",
                "--staged",
                "--worktree",
                "--",
                *tracked_changes,
            ],
        )
        restored_tracked_changes = tracked_changes
    remaining_changes = _worktree_status_entries(worktree)
    lineage_error: str | None = None
    try:
        _workspace_head(workspace)
    except WorkspaceError as exc:
        lineage_error = str(exc)
    raw_log = log_path.read_bytes()
    result = {
        "schemaVersion": VERIFICATION_RECEIPT_SCHEMA_VERSION,
        "taskId": task_id,
        "profileId": profile_id,
        "attempt": attempt,
        "argv": argv,
        "cwd": str(profile["cwd"]),
        "timeoutSeconds": int(profile["timeoutSeconds"]),
        "startedAt": started_at,
        "finishedAt": utc_now(),
        "elapsedSeconds": time.perf_counter() - started,
        "exitCode": return_code,
        "timedOut": timed_out,
        "output": str(log_path.relative_to(session_dir)),
        "outputSha256": hashlib.sha256(raw_log).hexdigest(),
        "outputBytes": len(raw_log),
        "outputTruncated": truncated[0],
        "trackedChangesRejected": tracked_changes,
        "trackedChangesRestored": restored_tracked_changes,
        "workspaceChanges": workspace_changes,
        "remainingWorkspaceChanges": remaining_changes,
        "lineageError": lineage_error,
        "processGroupCleanupError": group_cleanup_error,
        "passed": (
            return_code == 0
            and not timed_out
            and not workspace_changes
            and not remaining_changes
            and lineage_error is None
            and group_cleanup_error is None
        ),
    }
    _exclusive_json(attempt_root / f"{stem}.json", result)
    return result


def _bounded_copy(
    source: BinaryIO,
    destination: BinaryIO,
    maximum: int,
    truncated: list[bool],
) -> None:
    written = 0
    while True:
        chunk = source.read(65_536)
        if not chunk:
            return
        remaining = maximum - written
        if remaining > 0:
            destination.write(chunk[:remaining])
            written += min(len(chunk), remaining)
        if len(chunk) > remaining:
            truncated[0] = True


def _signal_process_group(process_id: int, sig: signal.Signals) -> None:
    try:
        os.killpg(process_id, sig)
    except ProcessLookupError:
        return


def _process_group_exists(process_id: int) -> bool:
    try:
        os.killpg(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tracked_changes(worktree: Path) -> list[str]:
    values: set[str] = set()
    for args in (
        ["diff", "--name-only", "-z"],
        ["diff", "--cached", "--name-only", "-z"],
    ):
        result = _git(worktree, args)
        values.update(
            value.decode("utf-8", "surrogateescape")
            for value in result.stdout.split(b"\0")
            if value
        )
    return sorted(values)


def _nul_paths(value: bytes) -> list[str]:
    return sorted(
        item.decode("utf-8", "surrogateescape")
        for item in value.split(b"\0")
        if item
    )


def _require_clean_tracked_worktree(worktree: Path) -> None:
    changes = _worktree_status_entries(worktree)
    if changes:
        raise WorkspaceError(
            "Session worktree has changes outside the artifact ledger: "
            + ", ".join(changes[:10])
        )


def _worktree_status_entries(worktree: Path) -> list[str]:
    result = _git(
        worktree,
        ["status", "--porcelain=v1", "--untracked-files=all"],
    )
    return [
        line
        for line in result.stdout.decode("utf-8", "replace").splitlines()
        if line
    ]


def _workspace_target(workspace: dict[str, Any]) -> str:
    policy = workspace.get("executionPolicy")
    if isinstance(policy, dict):
        value = policy.get("workspaceTarget")
        if value in WORKSPACE_TARGETS:
            return str(value)
    # Every pre-policy workspace snapshot was an isolated worktree.
    return "worktree"


def _execution_path(workspace: dict[str, Any]) -> Path:
    raw = workspace.get("executionPath", workspace.get("worktreePath"))
    if not isinstance(raw, str):
        raise WorkspaceError("Workspace execution path is missing.")
    path = Path(raw).resolve()
    root = Path(workspace["workspaceRoot"]).resolve()
    if _workspace_target(workspace) == "checkout":
        if path != root:
            raise WorkspaceError(
                "Main-checkout execution path differs from the approved root."
            )
    else:
        runtime_root = Path(workspace["worktreesRoot"]).resolve()
        if not _is_within(path, runtime_root):
            raise WorkspaceError(
                "Session worktree path escapes its approved runtime root."
            )
    return path


def _workspace_head(workspace: dict[str, Any]) -> str:
    worktree = _execution_path(workspace)
    _require_approved_branch(workspace, worktree)
    actual = _git_text(worktree, ["rev-parse", "HEAD"])
    expected = workspace.get("headSha")
    if expected is not None and actual != expected:
        raise WorkspaceError("Session worktree HEAD differs from its snapshot.")
    return actual


def _require_approved_branch(
    workspace: dict[str, Any],
    worktree: Path,
) -> None:
    if _workspace_target(workspace) == "checkout":
        expected_branch = workspace.get("startingBranch")
        if workspace.get("branch") != expected_branch:
            raise WorkspaceError(
                "Main-checkout branch differs from the approved contract."
            )
    else:
        plan_id = workspace.get("planId")
        session_id = workspace.get("sessionId")
        if not isinstance(plan_id, str) or not isinstance(session_id, str):
            raise WorkspaceError(
                "Session worktree identity is missing from its snapshot."
            )
        expected_branch = f"mlx-swarm/{plan_id}/{session_id}"
        if workspace.get("branch") != expected_branch:
            raise WorkspaceError(
                "Session worktree branch identity is invalid."
            )
    if (
        not isinstance(expected_branch, str)
        or _current_branch(worktree) != expected_branch
    ):
        target = (
            "Main checkout"
            if _workspace_target(workspace) == "checkout"
            else "Session worktree"
        )
        raise WorkspaceError(
            f"{target} branch differs from its approved snapshot."
        )


def _safe_patch_path(value: str) -> str:
    if "\x00" in value or "\\" in value:
        raise WorkspaceError("Diff path contains forbidden characters.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise WorkspaceError("Diff path escapes the workspace.")
    normalized = path.as_posix()
    if normalized == ".git" or normalized.startswith(".git/"):
        raise WorkspaceError("Diff path cannot target .git.")
    return normalized


def _path_within(path: str, root: str) -> bool:
    return root == "." or path == root or path.startswith(root.rstrip("/") + "/")


def _reject_symlink_components(root: Path, candidate: Path, label: str) -> None:
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise WorkspaceError(f"Path escapes the worktree: {label}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise WorkspaceError(f"Path traverses a symlink: {label}")


def _reject_external_filters(root: Path) -> None:
    result = _git(
        root,
        [
            "config",
            "--get-regexp",
            (
                r"^(filter\..*\.(clean|smudge|process)"
                r"|diff\..*\.(command|textconv))$"
            ),
        ],
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        raise WorkspaceError(
            "Workspace execution refuses repositories with external Git "
            "filters, diff drivers, or text conversion commands."
        )
    if result.returncode not in {0, 1}:
        raise WorkspaceError("Unable to inspect Git filter configuration.")


def _require_runtime_root_safe(root: Path, runtime_root: Path) -> None:
    if not _is_within(runtime_root, root):
        return
    probe = runtime_root / ".mlx-swarm-ignore-probe"
    result = _git(
        root,
        ["check-ignore", "--no-index", "-q", str(probe)],
        check=False,
    )
    if result.returncode != 0:
        raise WorkspaceError(
            "The configured artifacts/worktree directory is inside the Git "
            "workspace but is not ignored."
        )


def _dirty_entries(root: Path) -> list[str]:
    result = _git(
        root,
        ["status", "--porcelain=v1", "--untracked-files=all"],
    )
    return [
        line
        for line in result.stdout.decode("utf-8", "replace").splitlines()
        if line
    ]


def _current_branch(root: Path, *, required: bool = True) -> str | None:
    result = _git(
        root,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        check=False,
    )
    if result.returncode != 0:
        if not required:
            return None
        raise WorkspaceError(
            "Workspace execution requires a checked-out Git branch."
        )
    branch = result.stdout.decode("utf-8", "replace").strip()
    if not branch:
        if not required:
            return None
        raise WorkspaceError(
            "Workspace execution requires a checked-out Git branch."
        )
    return branch


def _git_text(
    cwd: Path,
    args: list[str],
    *,
    strip: bool = True,
) -> str:
    result = _git(cwd, args)
    value = result.stdout.decode("utf-8", "replace")
    return value.strip() if strip else value


def _git(
    cwd: Path,
    args: list[str],
    *,
    input_bytes: bytes | None = None,
    check: bool = True,
    index_file: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return _run(
        [*_GIT_PREFIX, "-C", str(cwd.resolve()), *args],
        input_bytes=input_bytes,
        check=check,
        git_index_file=index_file,
    )


def _run(
    argv: list[str],
    *,
    input_bytes: bytes | None = None,
    check: bool = True,
    git_index_file: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "check": False,
    }
    if argv and argv[0] == "git":
        git_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        git_env["GIT_CONFIG_NOSYSTEM"] = "1"
        git_env["GIT_CONFIG_GLOBAL"] = os.devnull
        git_env["GIT_TERMINAL_PROMPT"] = "0"
        if git_index_file is not None:
            git_env["GIT_INDEX_FILE"] = str(git_index_file.resolve())
        kwargs["env"] = git_env
    if input_bytes is not None:
        kwargs["input"] = input_bytes
    else:
        kwargs["stdin"] = subprocess.DEVNULL
    result = subprocess.run(argv, **kwargs)
    if check and result.returncode != 0:
        error = result.stderr.decode("utf-8", "replace").strip()
        raise WorkspaceError(
            f"Command failed ({result.returncode}): {' '.join(argv[:2])}: {error}"
        )
    return result


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _exclusive_text(path: Path, value: str) -> None:
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
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise WorkspaceError(
                f"Immutable artifact already exists: {path.name}"
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _exclusive_json(path: Path, value: Any) -> None:
    _exclusive_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
    )
