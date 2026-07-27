"""Localhost-only HTTP API and static dashboard for swarm work."""
# @lat: [[UI]]

from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .backend import _resolve_model_path
from .contracts import (
    ContractError,
    OutputGate,
    Plan,
    SwarmConfig,
    TaskDef,
    load_plan,
)
from .session import Session, _run_id, _utc_now

MAX_REQUEST_BYTES = 16_384
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class APIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class CockpitApp:
    """Read local plans/sessions and launch bounded CLI subprocesses."""

    def __init__(
        self,
        config: SwarmConfig,
        plans_dir: Path,
        *,
        popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
    ):
        self.config = config
        self.plans_dir = plans_dir.resolve()
        self.artifacts_dir = config.artifacts_dir.resolve()
        self.popen_factory = popen_factory
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._lock = threading.Lock()
        if not self.plans_dir.is_dir():
            raise RuntimeError(f"Plans directory not found: {self.plans_dir}")
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def status_payload(self) -> dict[str, Any]:
        try:
            model_path = _resolve_model_path(self.config)
            ready = True
            model_error = None
        except Exception as exc:
            model_path = None
            ready = False
            model_error = str(exc)
        return {
            "ready": ready,
            "model": {
                "repository": self.config.model.repository,
                "path": str(model_path) if model_path else None,
                "error": model_error,
            },
            "batch": {
                "maxWorkers": self.config.batch.max_workers,
                "prefillStepSize": self.config.batch.prefill_step_size,
                "maxPromptCharacters": self.config.batch.max_prompt_characters,
            },
            "plansDir": str(self.plans_dir),
            "artifactsDir": str(self.artifacts_dir),
            "reviewMode": "frontier-final-only",
        }

    def plans_payload(self) -> dict[str, Any]:
        valid, invalid = self._plan_catalog()
        return {
            "plans": [
                _serialize_plan(plan, path)
                for plan, path in sorted(
                    valid.values(),
                    key=lambda item: item[0].plan_id,
                )
            ],
            "invalid": invalid,
        }

    def runs_payload(self) -> dict[str, Any]:
        runs: list[dict[str, Any]] = []
        if self.artifacts_dir.is_dir():
            for candidate in self.artifacts_dir.glob("*/*/session.json"):
                state_path = candidate.resolve()
                if not _is_within(state_path, self.artifacts_dir):
                    continue
                try:
                    state = _read_json_file(state_path)
                except (OSError, ValueError):
                    continue
                runs.append(
                    _run_summary(
                        state,
                        state_path.parent,
                        self._runner_active(state),
                    )
                )
        runs.sort(
            key=lambda run: run.get("startedAt") or "",
            reverse=True,
        )
        return {"runs": runs}

    def run_detail(self, plan_id: str, session_id: str) -> dict[str, Any]:
        session_dir, state = self._load_run_state(plan_id, session_id)
        plan = self._load_historical_plan(session_dir, state)
        if plan is not None:
            plan_payload = _serialize_plan(plan, plan.source)
            levels = [
                [task.id for task in level]
                for level in plan.topological_order()
            ]
        else:
            plan_payload = {
                "planId": state.get("planId"),
                "objective": state.get("objective", ""),
                "source": state.get("planSource"),
                "tasks": [
                    {
                        "id": task_id,
                        "role": task.get("role", "general"),
                        "prompt": "",
                        "dependsOn": task.get("dependsOn", []),
                        "maxRepairAttempts": None,
                        "outputProtocol": "",
                        "gate": None,
                    }
                    for task_id, task in state.get("tasks", {}).items()
                ],
            }
            levels = _levels_from_state(state)

        frontier_result = None
        frontier_path = session_dir / "frontier-result.json"
        if frontier_path.is_file():
            try:
                frontier_result = _read_json_file(frontier_path)
            except (OSError, ValueError):
                frontier_result = None

        active = self._runner_active(state)
        summary = _run_summary(state, session_dir, active)
        return {
            "run": summary,
            "plan": plan_payload,
            "levels": levels,
            "tasks": state.get("tasks", {}),
            "batches": state.get("batches", []),
            "localUsage": (
                frontier_result.get("localUsage", {})
                if frontier_result
                else _local_usage(state.get("batches", []))
            ),
            "frontierResult": frontier_result,
            "actions": {
                "resume": (
                    not active
                    and state.get("status") in {"pending", "running"}
                ),
                "retry": state.get("status") in {"partial", "failed"},
            },
            "runnerLogAvailable": (session_dir / "runner.log").is_file(),
        }

    def launch_run(
        self,
        plan_id: str,
        max_repair: int,
        *,
        retry_of: str | None = None,
        plan_override: tuple[Plan, Path] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(max_repair, int) or isinstance(max_repair, bool):
            raise APIError(HTTPStatus.BAD_REQUEST, "maxRepair must be an integer.")
        if not 0 <= max_repair <= 5:
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "maxRepair must be between 0 and 5.",
            )

        if plan_override is None:
            valid, _ = self._plan_catalog()
            if plan_id not in valid:
                raise APIError(
                    HTTPStatus.NOT_FOUND,
                    f"Unknown or invalid plan: {plan_id}",
                )
            plan, plan_path = valid[plan_id]
        else:
            plan, plan_path = plan_override

        run_id = _run_id()
        session_dir = self.artifacts_dir / plan.plan_id / run_id
        session = Session(
            session_dir,
            plan,
            session_id=run_id,
            retry_of=retry_of,
            launch_source="ui",
        )
        session.set_sources(
            config_source=self.config.source,
            plan_source=plan_path,
        )
        session.state["maxRepair"] = max_repair
        session._save()
        self._spawn(
            session,
            [
                sys.executable,
                "-m",
                "swarm_agents.cli",
                "--config",
                str(self.config.source),
                "run",
                str(plan_path),
                "--session-dir",
                str(session_dir),
                "--max-repair",
                str(max_repair),
            ],
        )
        return self.run_detail(plan.plan_id, run_id)

    def resume_run(self, plan_id: str, session_id: str) -> dict[str, Any]:
        session_dir, state = self._load_run_state(plan_id, session_id)
        if self._runner_active(state):
            raise APIError(HTTPStatus.CONFLICT, "Run is already active.")
        if state.get("status") not in {"pending", "running"}:
            raise APIError(
                HTTPStatus.CONFLICT,
                "Only interrupted or pending runs can be resumed.",
            )
        session = Session.load(session_dir, self.config)
        max_repair = state.get("maxRepair", 2)
        if not isinstance(max_repair, int) or isinstance(max_repair, bool):
            max_repair = 2
        self._spawn(
            session,
            [
                sys.executable,
                "-m",
                "swarm_agents.cli",
                "--config",
                str(self.config.source),
                "resume",
                str(session_dir),
                "--max-repair",
                str(max_repair),
            ],
        )
        return self.run_detail(plan_id, session_id)

    def retry_run(
        self,
        plan_id: str,
        session_id: str,
        max_repair: int,
    ) -> dict[str, Any]:
        session_dir, state = self._load_run_state(plan_id, session_id)
        if state.get("status") not in {"partial", "failed"}:
            raise APIError(
                HTTPStatus.CONFLICT,
                "Only partial or failed runs can be retried.",
            )
        plan = self._load_historical_plan(session_dir, state)
        if plan is None:
            raise APIError(
                HTTPStatus.CONFLICT,
                "The original plan is unavailable for retry.",
            )
        plan_path = plan.source
        return self.launch_run(
            plan.plan_id,
            max_repair,
            retry_of=f"{plan_id}/{session_id}",
            plan_override=(plan, plan_path),
        )

    def _plan_catalog(
        self,
    ) -> tuple[dict[str, tuple[Plan, Path]], list[dict[str, str]]]:
        by_id: dict[str, list[tuple[Plan, Path]]] = {}
        invalid: list[dict[str, str]] = []
        config_source = self.config.source.resolve()
        model_root: Path | None = None
        if self.config.model.local_path:
            model_root = Path(self.config.model.local_path)
            if not model_root.is_absolute():
                model_root = config_source.parent / model_root
            model_root = model_root.resolve()
        for path in sorted(self.plans_dir.rglob("*.json")):
            resolved = path.resolve()
            if (
                resolved == config_source
                or _is_within(resolved, self.artifacts_dir)
                or (
                    model_root is not None
                    and _is_within(resolved, model_root)
                )
                or not _is_within(resolved, self.plans_dir)
            ):
                continue
            try:
                plan = load_plan(resolved, self.config)
            except ContractError as exc:
                invalid.append({
                    "path": str(resolved.relative_to(self.plans_dir)),
                    "error": str(exc),
                })
                continue
            by_id.setdefault(plan.plan_id, []).append((plan, resolved))

        valid: dict[str, tuple[Plan, Path]] = {}
        for plan_id, matches in by_id.items():
            if len(matches) == 1:
                valid[plan_id] = matches[0]
                continue
            paths = ", ".join(
                str(path.relative_to(self.plans_dir))
                for _, path in matches
            )
            invalid.append({
                "path": paths,
                "error": f"Duplicate planId {plan_id!r}.",
            })
        return valid, invalid

    def _load_run_state(
        self,
        plan_id: str,
        session_id: str,
    ) -> tuple[Path, dict[str, Any]]:
        _validate_identifier(plan_id, "planId")
        _validate_identifier(session_id, "sessionId")
        session_dir = (
            self.artifacts_dir / plan_id / session_id
        ).resolve()
        if not _is_within(session_dir, self.artifacts_dir):
            raise APIError(HTTPStatus.BAD_REQUEST, "Invalid run path.")
        state_path = session_dir / "session.json"
        if not state_path.is_file():
            raise APIError(HTTPStatus.NOT_FOUND, "Run not found.")
        try:
            state = _read_json_file(state_path)
        except (OSError, ValueError) as exc:
            raise APIError(
                HTTPStatus.CONFLICT,
                f"Run state is unreadable: {exc}",
            ) from exc
        if (
            state.get("planId") != plan_id
            or state.get("sessionId") != session_id
        ):
            raise APIError(HTTPStatus.CONFLICT, "Run identity mismatch.")
        return session_dir, state

    def _load_historical_plan(
        self,
        session_dir: Path,
        state: dict[str, Any],
    ) -> Plan | None:
        snapshot = state.get("planSnapshot")
        if snapshot:
            snapshot_path = (session_dir / snapshot).resolve()
            if _is_within(snapshot_path, session_dir) and snapshot_path.is_file():
                try:
                    return load_plan(snapshot_path, self.config)
                except ContractError:
                    pass
        source_value = state.get("planSource")
        if not source_value:
            return None
        source = Path(source_value).resolve()
        if not _is_within(source, self.plans_dir) or not source.is_file():
            return None
        try:
            return load_plan(source, self.config)
        except ContractError:
            return None

    def _spawn(self, session: Session, argv: list[str]) -> None:
        log_path = session.dir / "runner.log"
        session.state["runnerStartedAt"] = _utc_now()
        session._save()
        with log_path.open("ab", buffering=0) as log_file:
            process = self.popen_factory(
                argv,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        runner_path = session.dir / "runner.json"
        runner_temp = session.dir / "runner.json.tmp"
        runner_temp.write_text(
            json.dumps({
                "pid": process.pid,
                "startedAt": session.state["runnerStartedAt"],
                "argv": argv,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        runner_temp.replace(runner_path)
        key = f"{session.plan.plan_id}/{session.session_id}"
        with self._lock:
            self._processes[key] = process

    def _runner_active(self, state: dict[str, Any]) -> bool:
        key = f"{state.get('planId')}/{state.get('sessionId')}"
        if state.get("status") in {"completed", "partial", "failed"}:
            with self._lock:
                self._processes.pop(key, None)
            return False
        with self._lock:
            process = self._processes.get(key)
            if process is not None:
                if process.poll() is None:
                    return True
                self._processes.pop(key, None)
        pid = state.get("runnerPid")
        if not isinstance(pid, int):
            plan_id = state.get("planId")
            session_id = state.get("sessionId")
            if (
                isinstance(plan_id, str)
                and isinstance(session_id, str)
                and _SAFE_ID.fullmatch(plan_id)
                and _SAFE_ID.fullmatch(session_id)
            ):
                runner_path = (
                    self.artifacts_dir / plan_id / session_id / "runner.json"
                ).resolve()
                if _is_within(runner_path, self.artifacts_dir):
                    try:
                        pid = _read_json_file(runner_path).get("pid")
                    except (OSError, ValueError):
                        pid = None
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True


def _serialize_plan(plan: Plan, source: Path) -> dict[str, Any]:
    return {
        "planId": plan.plan_id,
        "objective": plan.objective,
        "source": str(source),
        "tasks": [_serialize_task(task) for task in plan.tasks],
    }


def _serialize_task(task: TaskDef) -> dict[str, Any]:
    return {
        "id": task.id,
        "role": task.role,
        "prompt": task.prompt,
        "dependsOn": list(task.depends_on),
        "maxRepairAttempts": task.max_repair_attempts,
        "outputProtocol": task.output_protocol,
        "generationOverride": task.generation_override,
        "gate": _serialize_gate(task.gate),
    }


def _serialize_gate(gate: OutputGate | None) -> dict[str, Any] | None:
    if gate is None:
        return None
    value = asdict(gate)
    return {
        "requiredPatterns": [
            {"id": item["identifier"], "pattern": item["pattern"]}
            for item in value["required_patterns"]
        ],
        "forbiddenPatterns": [
            {"id": item["identifier"], "pattern": item["pattern"]}
            for item in value["forbidden_patterns"]
        ],
        "maxCharacters": value["max_characters"],
        "format": value["output_format"],
        "stripSingleCodeFence": value["strip_single_code_fence"],
        "pythonSyntax": value["python_syntax"],
        "jsonRequiredKeys": list(value["json_required_keys"]),
        "jsonAllowedKeys": list(value["json_allowed_keys"]),
        "jsonFieldEnums": {
            key: list(choices)
            for key, choices in value["json_field_enums"].items()
        },
    }


def _run_summary(
    state: dict[str, Any],
    session_dir: Path,
    active: bool,
) -> dict[str, Any]:
    tasks = state.get("tasks", {})
    counts: dict[str, int] = {}
    for task in tasks.values():
        task_status = task.get("status", "pending")
        counts[task_status] = counts.get(task_status, 0) + 1
    finished_at = state.get("finishedAt")
    started_at = state.get("startedAt")
    elapsed = _elapsed_seconds(started_at, finished_at)
    return {
        "sessionId": state.get("sessionId"),
        "planId": state.get("planId"),
        "objective": state.get("objective", ""),
        "status": state.get("status", "pending"),
        "active": active,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "elapsedSeconds": elapsed,
        "counts": counts,
        "total": len(tasks),
        "completed": counts.get("completed", 0),
        "retryOf": state.get("retryOf"),
        "launchSource": state.get("launchSource", "cli"),
        "maxRepair": state.get("maxRepair"),
        "frontierResult": (
            str(session_dir / "frontier-result.json")
            if (session_dir / "frontier-result.json").is_file()
            else None
        ),
    }


def _elapsed_seconds(
    started_at: str | None,
    finished_at: str | None,
) -> float | None:
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = (
            datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
            if finished_at
            else datetime.now(started.tzinfo)
        )
        return max(0.0, (end - started).total_seconds())
    except (TypeError, ValueError):
        return None


def _local_usage(batches: list[dict[str, Any]]) -> dict[str, int]:
    prompt_tokens = 0
    generation_tokens = 0
    generation_calls = 0
    model_loads = 0
    for batch in batches:
        statistics = [batch.get("statistics", {})]
        statistics.extend(
            repair.get("statistics", {})
            for repair in batch.get("repairs", [])
        )
        for stats in statistics:
            if not stats or stats.get("batchSize", 0) == 0:
                continue
            generation_calls += len(stats.get("groups", [])) or 1
            prompt_tokens += int(stats.get("promptTokens", 0))
            generation_tokens += int(stats.get("generationTokens", 0))
            if float(stats.get("loadSeconds", 0.0)) > 0:
                model_loads += 1
    return {
        "promptTokens": prompt_tokens,
        "generationTokens": generation_tokens,
        "generationCalls": generation_calls,
        "modelLoads": model_loads,
    }


def _levels_from_state(state: dict[str, Any]) -> list[list[str]]:
    tasks = state.get("tasks", {})
    remaining = set(tasks)
    completed: set[str] = set()
    levels: list[list[str]] = []
    while remaining:
        ready = sorted(
            task_id
            for task_id in remaining
            if all(
                dependency in completed
                for dependency in tasks[task_id].get("dependsOn", [])
            )
        )
        if not ready:
            levels.append(sorted(remaining))
            break
        levels.append(ready)
        completed.update(ready)
        remaining.difference_update(ready)
    return levels


def _validate_identifier(value: str, label: str) -> None:
    if _SAFE_ID.fullmatch(value) is None:
        raise APIError(HTTPStatus.BAD_REQUEST, f"Invalid {label}.")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_json_file(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


class CockpitHandler(BaseHTTPRequestHandler):
    app: CockpitApp
    server_version = "SwarmCockpit/0.1"

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/status":
                self._send_json(self.app.status_payload())
                return
            if path == "/api/plans":
                self._send_json(self.app.plans_payload())
                return
            if path == "/api/runs":
                self._send_json(self.app.runs_payload())
                return
            parts = _api_run_parts(path)
            if parts is not None:
                plan_id, session_id, action = parts
                if action is None:
                    self._send_json(
                        self.app.run_detail(plan_id, session_id)
                    )
                    return
            self._serve_static(path)
        except APIError as exc:
            self._send_error_json(exc.status, exc.message)
        except Exception as exc:
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Internal server error: {exc}",
            )

    def do_POST(self) -> None:
        try:
            self._check_origin()
            path = urlparse(self.path).path
            body = self._read_json_body()
            if path == "/api/runs":
                self._send_json(
                    self.app.launch_run(
                        _required_text(body, "planId"),
                        body.get("maxRepair", 2),
                    ),
                    status=HTTPStatus.ACCEPTED,
                )
                return
            parts = _api_run_parts(path)
            if parts is not None:
                plan_id, session_id, action = parts
                if action == "resume":
                    self._send_json(
                        self.app.resume_run(plan_id, session_id),
                        status=HTTPStatus.ACCEPTED,
                    )
                    return
                if action == "retry":
                    self._send_json(
                        self.app.retry_run(
                            plan_id,
                            session_id,
                            body.get("maxRepair", 2),
                        ),
                        status=HTTPStatus.ACCEPTED,
                    )
                    return
            raise APIError(HTTPStatus.NOT_FOUND, "Endpoint not found.")
        except APIError as exc:
            self._send_error_json(exc.status, exc.message)
        except Exception as exc:
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Internal server error: {exc}",
            )

    def _read_json_body(self) -> dict[str, Any]:
        length_value = self.headers.get("Content-Length")
        if length_value is None:
            raise APIError(HTTPStatus.LENGTH_REQUIRED, "Content-Length required.")
        try:
            length = int(length_value)
        except ValueError as exc:
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "Invalid Content-Length.",
            ) from exc
        if not 0 <= length <= MAX_REQUEST_BYTES:
            raise APIError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "Request body is too large.",
            )
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise APIError(HTTPStatus.BAD_REQUEST, "Invalid JSON body.") from exc
        if not isinstance(value, dict):
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "JSON body must be an object.",
            )
        return value

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if not origin:
            return
        parsed = urlparse(origin)
        server_host, server_port = self.server.server_address[:2]
        try:
            origin_port = parsed.port
            request_host = urlparse(
                f"http://{self.headers.get('Host', '')}"
            ).hostname
        except ValueError as exc:
            raise APIError(
                HTTPStatus.FORBIDDEN,
                "Cross-origin request rejected.",
            ) from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname not in LOCAL_HOSTS
            or parsed.hostname != request_host
        ):
            raise APIError(HTTPStatus.FORBIDDEN, "Cross-origin request rejected.")
        if origin_port != server_port:
            raise APIError(HTTPStatus.FORBIDDEN, "Cross-origin request rejected.")
        if server_host not in LOCAL_HOSTS:
            raise APIError(HTTPStatus.FORBIDDEN, "Mutation is not localhost-bound.")

    def _serve_static(self, path: str) -> None:
        asset_name = {
            "/": "index.html",
            "/index.html": "index.html",
            "/app.js": "app.js",
            "/styles.css": "styles.css",
        }.get(path)
        if asset_name is None:
            raise APIError(HTTPStatus.NOT_FOUND, "Not found.")
        asset = files("swarm_agents.ui_static").joinpath(asset_name)
        content = asset.read_bytes()
        content_type = mimetypes.guess_type(asset_name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", _content_security_policy())
        self.end_headers()
        self.wfile.write(content)

    def _send_json(
        self,
        value: Any,
        *,
        status: int = HTTPStatus.OK,
    ) -> None:
        content = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def log_message(self, format: str, *args: Any) -> None:
        return


def _api_run_parts(path: str) -> tuple[str, str, str | None] | None:
    parts = [unquote(part) for part in path.strip("/").split("/")]
    if len(parts) not in {4, 5} or parts[:2] != ["api", "runs"]:
        return None
    action = parts[4] if len(parts) == 5 else None
    if action not in {None, "resume", "retry"}:
        return None
    return parts[2], parts[3], action


def _required_text(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise APIError(HTTPStatus.BAD_REQUEST, f"{key} is required.")
    return result.strip()


def _content_security_policy() -> str:
    return (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    )


def make_handler(app: CockpitApp) -> type[CockpitHandler]:
    class BoundCockpitHandler(CockpitHandler):
        pass

    BoundCockpitHandler.app = app
    return BoundCockpitHandler


def serve_ui(
    config: SwarmConfig,
    plans_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    server_factory: Callable[..., ThreadingHTTPServer] = ThreadingHTTPServer,
) -> None:
    if host not in LOCAL_HOSTS:
        raise RuntimeError(
            "The cockpit is localhost-only; use 127.0.0.1, localhost, or ::1."
        )
    app = CockpitApp(config, plans_dir)
    server = server_factory((host, port), make_handler(app))
    actual_host, actual_port = server.server_address[:2]
    display_host = f"[{actual_host}]" if ":" in actual_host else actual_host
    url = f"http://{display_host}:{actual_port}/"
    print(json.dumps({
        "ready": True,
        "url": url,
        "plansDir": str(app.plans_dir),
        "artifactsDir": str(app.artifacts_dir),
    }, indent=2))
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
