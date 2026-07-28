"""Safe Git worktree execution for typed MLX Swarm artifacts."""
# @lat: [[workspace-execution]]

from __future__ import annotations

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

WORKSPACE_CONTRACT_VERSION = 1
ARTIFACT_SCHEMA_VERSION = 1
DECISION_SCHEMA_VERSION = 1
_DIFF_HEADER = re.compile(r"^diff --git a/([^\t\r\n]+) b/([^\t\r\n]+)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
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


def execution_preview(
    config: SwarmConfig,
    plan: Plan,
) -> dict[str, Any]:
    """Resolve and hash the operator-visible workspace execution contract."""
    if config.workspace is None or not plan.workspace_execution:
        raise WorkspaceError(
            "Workspace execution requires schema-v2 config and plan contracts."
        )
    root = discover_git_root(config.source.parent)
    base_sha = _git_text(root, ["rev-parse", "HEAD"])
    _reject_external_filters(root)
    worktrees_root = (config.artifacts_dir / "_worktrees").resolve()
    _require_runtime_root_safe(root, worktrees_root)
    dirty = _dirty_entries(root)
    referenced = sorted(
        {
            identifier
            for task in plan.tasks
            for identifier in task.verification
        }
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
        "schemaVersion": WORKSPACE_CONTRACT_VERSION,
        "planSha256": canonical_sha256(plan.raw),
        "workspaceRoot": str(root),
        "baseSha": base_sha,
        "writeRoots": list(config.workspace.write_roots),
        "verificationProfiles": profiles,
        "worktreesRoot": str(worktrees_root),
    }
    return {
        **contract,
        "executionDigest": canonical_sha256(contract),
        "dirty": bool(dirty),
        "dirtyEntries": dirty[:256],
        "dirtyEntriesTruncated": len(dirty) > 256,
    }


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
        _require_runtime_root_safe(root, worktrees_root)
        base_sha = _git_text(root, ["rev-parse", "HEAD"])
        dirty = _dirty_entries(root)
        return {
            "enabled": True,
            "ready": True,
            "workspaceRoot": str(root),
            "baseSha": base_sha,
            "worktreesRoot": str(worktrees_root),
            "dirty": bool(dirty),
            "dirtyEntries": dirty[:256],
            "dirtyEntriesTruncated": len(dirty) > 256,
            "writeRoots": list(config.workspace.write_roots),
            "verificationProfiles": sorted(
                config.workspace.verification_profiles
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
    preview = execution_preview(config, plan)
    if expected_execution_digest != preview["executionDigest"]:
        raise WorkspaceError(
            "Execution digest mismatch; refresh the workspace preview."
        )
    root = Path(preview["workspaceRoot"])
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
        if key not in {"dirtyEntries", "dirtyEntriesTruncated"}
    }
    snapshot.update({
        "branch": branch,
        "worktreePath": str(worktree),
        "headSha": preview["baseSha"],
        "dirtyEntries": preview["dirtyEntries"],
        "dirtyEntriesTruncated": preview["dirtyEntriesTruncated"],
        "preparedAt": utc_now(),
        "cleanedUp": False,
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
    artifact_dir.mkdir(parents=True, exist_ok=False)
    suffix = (
        ".diff"
        if task.artifact_type in MUTATING_ARTIFACT_TYPES
        else ".json"
        if task.artifact_type == "review"
        else ".md"
    )
    payload_name = f"payload{suffix}"
    _exclusive_text(artifact_dir / payload_name, payload)
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
    _atomic_json(artifact_dir / "manifest.json", manifest)
    return manifest


def load_artifact(session_dir: Path, task_id: str) -> tuple[dict[str, Any], str]:
    artifact_dir = session_dir / "artifacts" / task_id
    try:
        manifest = json.loads(
            (artifact_dir / "manifest.json").read_text(encoding="utf-8")
        )
        payload_name = manifest["payload"]
        payload_path = (artifact_dir / payload_name).resolve()
        if not _is_within(payload_path, artifact_dir.resolve()):
            raise WorkspaceError("Artifact payload path escapes its directory.")
        payload = payload_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WorkspaceError(f"Artifact not found for task {task_id}.") from exc
    except (KeyError, json.JSONDecodeError, OSError) as exc:
        raise WorkspaceError(f"Artifact for task {task_id} is invalid.") from exc
    if not isinstance(manifest, dict):
        raise WorkspaceError("Artifact manifest must be a JSON object.")
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

    worktree = Path(workspace["worktreePath"]).resolve()
    root = Path(workspace["workspaceRoot"]).resolve()
    runtime_roots = [
        Path(workspace["worktreesRoot"]).resolve(),
    ]
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
        ["apply", "--check", "--index", "-"],
        input_bytes=payload.encode("utf-8"),
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise WorkspaceError(f"git apply --check failed: {message}")
    return paths


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
    if not isinstance(value, dict):
        raise WorkspaceError("Artifact decision is invalid.")
    return value


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
        if not isinstance(value, dict):
            raise WorkspaceError("Artifact rejection decision is invalid.")
        return "rejection.json", value
    for path in sorted(artifact_dir.glob("verify-request-*.json")):
        if path.name in processed_requests:
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise WorkspaceError("Verification request is invalid.") from exc
        if not isinstance(value, dict):
            raise WorkspaceError("Verification request is invalid.")
        return path.name, value
    return None


def apply_artifact(
    session_dir: Path,
    task: TaskDef,
    workspace: dict[str, Any],
) -> dict[str, Any]:
    manifest, payload = load_artifact(session_dir, task.id)
    if manifest.get("decisionStatus") not in {"awaiting_approval", "applying"}:
        raise WorkspaceError("Artifact is not awaiting application.")
    worktree = Path(workspace["worktreePath"]).resolve()
    expected_head = manifest.get("baseCommit")
    if _workspace_head(workspace) != expected_head:
        raise WorkspaceError("Session worktree HEAD changed after artifact generation.")
    _require_clean_tracked_worktree(worktree)
    validate_patch(payload, task=task, workspace=workspace)
    _git(
        worktree,
        ["apply", "--index", "-"],
        input_bytes=payload.encode("utf-8"),
    )
    commit = _git_text(
        worktree,
        [
            "-c",
            "user.name=MLX Swarm",
            "-c",
            "user.email=mlx-swarm@localhost",
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            f"mlx-swarm: {task.id} ({task.artifact_type})",
        ],
    )
    del commit
    commit_sha = _git_text(worktree, ["rev-parse", "HEAD"])
    receipt = {
        "schemaVersion": DECISION_SCHEMA_VERSION,
        "taskId": task.id,
        "artifactSha256": manifest["sha256"],
        "baseCommit": expected_head,
        "commitSha": commit_sha,
        "appliedAt": utc_now(),
    }
    _exclusive_json(
        session_dir / "artifacts" / task.id / "apply-receipt.json",
        receipt,
    )
    workspace["headSha"] = commit_sha
    return receipt


def recover_artifact_application(
    session_dir: Path,
    task: TaskDef,
    workspace: dict[str, Any],
) -> dict[str, Any]:
    """Reconcile a crash around git apply/commit without applying twice."""
    manifest, _ = load_artifact(session_dir, task.id)
    worktree = Path(workspace["worktreePath"]).resolve()
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
        if (
            not isinstance(receipt, dict)
            or receipt.get("artifactSha256") != manifest.get("sha256")
            or receipt.get("commitSha") != actual_head
        ):
            raise WorkspaceError(
                "Applied artifact receipt does not match the worktree."
            )
        _require_clean_tracked_worktree(worktree)
        workspace["headSha"] = actual_head
        return {"state": "applied", "receipt": receipt}

    if actual_head == base_commit:
        _require_clean_tracked_worktree(worktree)
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
        parent != base_commit
        or changed_paths != sorted(manifest.get("affectedPaths", []))
    ):
        raise WorkspaceError(
            "Worktree lineage changed during artifact application."
        )
    _require_clean_tracked_worktree(worktree)
    receipt = {
        "schemaVersion": DECISION_SCHEMA_VERSION,
        "taskId": task.id,
        "artifactSha256": manifest["sha256"],
        "baseCommit": base_commit,
        "commitSha": actual_head,
        "appliedAt": utc_now(),
        "recoveredAfterCrash": True,
    }
    _exclusive_json(receipt_path, receipt)
    workspace["headSha"] = actual_head
    return {"state": "applied", "receipt": receipt}


def run_verifications(
    session_dir: Path,
    task: TaskDef,
    workspace: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run only profiles snapshotted into the approved execution contract."""
    results: list[dict[str, Any]] = []
    for profile_id in task.verification:
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


def revert_applied_artifact(
    session_dir: Path,
    task_id: str,
    workspace: dict[str, Any],
) -> dict[str, Any]:
    artifact_dir = session_dir / "artifacts" / task_id
    try:
        apply_receipt = json.loads(
            (artifact_dir / "apply-receipt.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise WorkspaceError("Applied artifact receipt is missing.") from exc
    worktree = Path(workspace["worktreePath"]).resolve()
    commit_sha = apply_receipt.get("commitSha")
    if _workspace_head(workspace) != commit_sha:
        raise WorkspaceError(
            "Only the current failed artifact commit can be rejected."
        )
    _require_clean_tracked_worktree(worktree)
    _git(
        worktree,
        ["revert", "--no-edit", "--no-gpg-sign", str(commit_sha)],
    )
    revert_sha = _git_text(worktree, ["rev-parse", "HEAD"])
    receipt = {
        "schemaVersion": DECISION_SCHEMA_VERSION,
        "taskId": task_id,
        "revertedCommit": commit_sha,
        "revertCommit": revert_sha,
        "revertedAt": utc_now(),
    }
    _exclusive_json(artifact_dir / "revert-receipt.json", receipt)
    workspace["headSha"] = revert_sha
    return receipt


def final_workspace_diff(workspace: dict[str, Any]) -> tuple[str, str]:
    worktree = Path(workspace["worktreePath"]).resolve()
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


def cleanup_worktree(workspace: dict[str, Any]) -> None:
    root = Path(workspace["workspaceRoot"]).resolve()
    worktree = Path(workspace["worktreePath"]).resolve()
    expected_root = Path(workspace["worktreesRoot"]).resolve()
    if not _is_within(worktree, expected_root):
        raise WorkspaceError("Worktree cleanup target escapes its runtime root.")
    _git(root, ["worktree", "remove", "--force", str(worktree)])
    workspace["cleanedUp"] = True
    workspace["cleanedUpAt"] = utc_now()


def _run_verification_profile(
    session_dir: Path,
    task_id: str,
    profile_id: str,
    profile: dict[str, Any],
    workspace: dict[str, Any],
) -> dict[str, Any]:
    worktree = Path(workspace["worktreePath"]).resolve()
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
            os.killpg(process.pid, signal.SIGTERM)
            try:
                return_code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                return_code = process.wait()
        reader.join(timeout=5)
    workspace_changes = _worktree_status_entries(worktree)
    tracked_changes = _tracked_changes(worktree)
    if tracked_changes:
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
    remaining_changes = _worktree_status_entries(worktree)
    result = {
        "schemaVersion": 1,
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
        "outputTruncated": truncated[0],
        "trackedChangesRejected": tracked_changes,
        "workspaceChanges": workspace_changes,
        "remainingWorkspaceChanges": remaining_changes,
        "passed": (
            return_code == 0
            and not timed_out
            and not workspace_changes
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


def _workspace_head(workspace: dict[str, Any]) -> str:
    worktree = Path(workspace["worktreePath"]).resolve()
    actual = _git_text(worktree, ["rev-parse", "HEAD"])
    expected = workspace.get("headSha")
    if expected is not None and actual != expected:
        raise WorkspaceError("Session worktree HEAD differs from its snapshot.")
    return actual


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
) -> subprocess.CompletedProcess[bytes]:
    return _run(
        [*_GIT_PREFIX, "-C", str(cwd.resolve()), *args],
        input_bytes=input_bytes,
        check=check,
    )


def _run(
    argv: list[str],
    *,
    input_bytes: bytes | None = None,
    check: bool = True,
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
