"""Reproducible paired economics evaluation for MLX Swarm."""
# @lat: [[economics-evaluation]]

from __future__ import annotations

import ast
import builtins
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import random
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .commander import (
    CommanderError,
    CommanderStore,
    canonical_json_sha256,
)
from .contracts import (
    Plan,
    SwarmConfig,
    WorkerConfig,
    load_config,
    load_plan,
    worker_capabilities_payload,
)
from .session import Session, _run_id
from .workspace import (
    WorkspaceError,
    discover_git_root,
    execution_preview,
    final_workspace_diff,
    load_artifact,
    load_workspace_snapshot,
    prepare_worktree,
    submit_artifact_decision,
)


EVALUATION_SCHEMA_VERSION = 1
PROFILE_SCHEMA_VERSION = 3
SUITE_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
FAIR_EVALUATION_PROTOCOL_VERSION = 11
DEFAULT_EVALUATIONS_DIR = ".swarm/evaluations"
DEFAULT_PUBLIC_RESULTS_DIR = "benchmarks/results"
README_START = "<!-- BEGIN MLX-SWARM-ECONOMICS -->"
README_END = "<!-- END MLX-SWARM-ECONOMICS -->"
MAX_LOG_BYTES = 1_000_000
MAX_TASK_PACKET_TREE_CHARS = 10_000
MAX_TASK_PACKET_SOURCE_CHARS = 70_000
MAX_TASK_PACKET_RUNTIME_STATE_CHARS = 20_000
MAX_FRONTIER_DELEGATION_EDITS = 8
BENCHMARK_BUILD_JOBS = 4
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_INFO_VALUE = re.compile(
    r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"(.*)"$'
)
_DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_SHELL_META = ("&&", "||", ">", "<", "|", ";", "$(", "`")
_SAFE_BENCHMARK_COMMANDS = {
    "pip",
    "pip3",
    "py.test",
    "pytest",
    "python",
    "python3",
    "touch",
    "tox",
}
_NON_PRODUCTION_PREFIXES = (
    ".github/",
    "doc/",
    "docs/",
    "pending_tests/",
    "scripts/",
    "test/",
    "tests/",
    "testing/",
)
_NON_PRODUCTION_NAMES = {
    "changelog",
    "changelog.md",
    "license",
    "license.md",
    "manifest.in",
    "pyproject.toml",
    "readme",
    "readme.md",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "tox.ini",
}

_TRACE_LOCALS_RUNNER = r'''import json
import os
import runpy
import sys
import threading
import types

workspace = os.path.realpath(sys.argv[1])
output_path = sys.argv[2]
with open(sys.argv[3], "r", encoding="utf-8") as handle:
    allowed_ranges = json.load(handle)
target = sys.argv[4:]
snapshots = {}
snapshot_count = 0
snapshot_lock = threading.Lock()


def safe_value(value, depth=0):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return {
            "type": "str",
            "length": len(value),
            "value": value[:240],
        }
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value)}
    if isinstance(value, (list, tuple, set, frozenset)):
        result = {"type": type(value).__name__, "length": len(value)}
        if depth == 0:
            result["items"] = [
                safe_value(item, depth=1)
                for item in list(value)[:4]
            ]
        return result
    if isinstance(value, dict):
        return {
            "type": "dict",
            "length": len(value),
            "keys": [
                str(key)[:80]
                for key in list(value.keys())[:8]
            ],
        }
    if isinstance(value, type):
        return {
            "type": "type",
            "name": (
                value.__module__
                + "."
                + getattr(value, "__qualname__", value.__name__)
            ),
        }
    if isinstance(value, types.ModuleType) or callable(value):
        return {"type": type(value).__name__}
    value_type = type(value)
    result = {
        "type": (
            value_type.__module__
            + "."
            + getattr(value_type, "__qualname__", value_type.__name__)
        ),
    }
    if depth == 0:
        try:
            fields = vars(value)
        except TypeError:
            fields = {}
        fields = dict(fields)
        for owner in getattr(value_type, "__mro__", (value_type,)):
            raw_slots = owner.__dict__.get("__slots__", ())
            if isinstance(raw_slots, str):
                raw_slots = (raw_slots,)
            for name in raw_slots:
                if (
                    not isinstance(name, str)
                    or name.startswith("_")
                    or name in fields
                ):
                    continue
                descriptor = owner.__dict__.get(name)
                if not isinstance(descriptor, types.MemberDescriptorType):
                    continue
                try:
                    fields[name] = object.__getattribute__(value, name)
                except (AttributeError, TypeError, ValueError):
                    continue
        selected = {}
        for name in sorted(fields):
            if name.startswith("_"):
                continue
            selected[name] = safe_value(fields[name], depth=1)
            if len(selected) >= 16:
                break
        if selected:
            result["fields"] = selected
    return result


def trace_selected_frame(frame, event, _arg):
    global snapshot_count
    if event != "line" or snapshot_count >= 4000:
        return trace_selected_frame
    filename = os.path.realpath(frame.f_code.co_filename)
    try:
        if os.path.commonpath([workspace, filename]) != workspace:
            return None
    except ValueError:
        return None
    relative = os.path.relpath(filename, workspace).replace(os.sep, "/")
    ranges = allowed_ranges.get(relative)
    if not ranges or not any(
        start <= frame.f_lineno <= end
        for start, end in ranges
    ):
        return trace_selected_frame
    key = "%s:%d:%s" % (
        relative,
        frame.f_lineno,
        frame.f_code.co_name,
    )
    values = {}
    for name in sorted(frame.f_locals):
        if name.startswith("__"):
            continue
        values[name] = safe_value(frame.f_locals[name])
        if len(values) >= 24:
            break
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))
    with snapshot_lock:
        prior = snapshots.setdefault(key, [])
        if encoded not in prior and len(prior) < 6:
            prior.append(encoded)
            snapshot_count += 1
    return trace_selected_frame


def trace_frame(frame, event, _arg):
    if event != "call" or snapshot_count >= 4000:
        return None
    filename = os.path.realpath(frame.f_code.co_filename)
    try:
        if os.path.commonpath([workspace, filename]) != workspace:
            return None
    except ValueError:
        return None
    relative = os.path.relpath(filename, workspace).replace(os.sep, "/")
    ranges = allowed_ranges.get(relative)
    first_line = frame.f_code.co_firstlineno
    if not ranges or not any(
        start <= first_line <= end
        for start, end in ranges
    ):
        return None
    return trace_selected_frame


def write_evidence():
    records = []
    for key in sorted(snapshots):
        path, line, function = key.rsplit(":", 2)
        for sample, encoded in enumerate(snapshots[key], start=1):
            records.append({
                "path": path,
                "line": int(line),
                "function": function,
                "sample": sample,
                "locals": json.loads(encoded),
            })
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(
            {"schemaVersion": 1, "records": records},
            handle,
            sort_keys=True,
            separators=(",", ":"),
        )


exit_code = 0
error = None
sys.settrace(trace_frame)
threading.settrace(trace_frame)
try:
    if len(target) >= 2 and target[0] == "-m":
        sys.argv = [target[1]] + target[2:]
        runpy.run_module(target[1], run_name="__main__", alter_sys=True)
    elif target:
        sys.argv = target
        runpy.run_path(target[0], run_name="__main__")
except SystemExit as exc:
    if exc.code is None:
        exit_code = 0
    elif isinstance(exc.code, int):
        exit_code = exc.code
    else:
        exit_code = 1
except BaseException:
    error = sys.exc_info()
finally:
    sys.settrace(None)
    threading.settrace(None)
    write_evidence()
if error is not None:
    raise error[1].with_traceback(error[2])
raise SystemExit(exit_code)
'''
_CONTEXT_STOP_WORDS = {
    "actual",
    "assert",
    "class",
    "command",
    "error",
    "expected",
    "false",
    "file",
    "from",
    "function",
    "import",
    "line",
    "none",
    "object",
    "python",
    "return",
    "self",
    "string",
    "test",
    "tests",
    "that",
    "this",
    "true",
    "with",
}


class EvaluationError(RuntimeError):
    """Raised when evaluation evidence or execution is invalid."""


_FRONTIER_ADAPTERS = (
    "codex-cli",
    "hermes-oneshot",
    "hermes-completion",
)
_FRONTIER_TOOLSETS = ("todo",)
_MAX_FRONTIER_RESPONSE_BYTES = 1_000_000


@dataclass(frozen=True)
class FrontierSettings:
    command: str
    codex_version: str
    model: str
    reasoning_effort: str
    arm_timeout_seconds: int
    planning_timeout_seconds: int
    local_timeout_seconds: int
    review_timeout_seconds: int
    adapter: str = "codex-cli"
    provider: str = "openai-codex"
    command_version: str = ""
    toolsets: tuple[str, ...] = ()
    context_window: int = 0
    max_completion_tokens: int = 0


@dataclass(frozen=True)
class SelectionSettings:
    pilot_size: int
    measured_size: int
    min_projects: int
    max_per_project: int
    max_changed_files: int
    max_changed_lines: int
    max_context_characters: int
    minimum_python: tuple[int, int]
    projects: tuple[str, ...]


@dataclass(frozen=True)
class StorageSettings:
    max_bytes: int
    min_free_bytes: int


@dataclass(frozen=True)
class ContainerSettings:
    image: str
    digest: str
    platform: str


@dataclass(frozen=True)
class EvaluationProfile:
    source: Path
    schema_version: int
    profile_id: str
    benchmark_repository: str
    benchmark_revision: str
    seed: int
    selection: SelectionSettings
    storage: StorageSettings
    container: ContainerSettings
    frontier: FrontierSettings
    python_bootstrap: tuple[str, ...]
    dependency_roots: dict[str, tuple[str, ...]]
    dependency_pins: dict[str, tuple[str, ...]]
    max_repair: int


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float
    timed_out: bool = False
    infrastructure_error: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_evaluation_profile(path: Path) -> EvaluationProfile:
    """Load a strict, pinned economics evaluation profile."""
    raw = _read_json(path)
    _exact_keys(
        raw,
        "profile",
        {
            "schemaVersion",
            "profileId",
            "benchmark",
            "seed",
            "selection",
            "storage",
            "container",
            "frontier",
            "pythonBootstrap",
            "dependencyRoots",
            "dependencyPins",
            "local",
        },
    )
    schema_version = _integer(
        raw["schemaVersion"],
        "profile.schemaVersion",
        1,
        100,
    )
    if schema_version not in {1, 2, 3}:
        raise EvaluationError("Unsupported evaluation profile schema version.")
    benchmark = _object(raw["benchmark"], "profile.benchmark")
    _exact_keys(
        benchmark,
        "profile.benchmark",
        {"repository", "revision"},
    )
    revision = _text(benchmark["revision"], "profile.benchmark.revision")
    if _GIT_SHA.fullmatch(revision) is None:
        raise EvaluationError(
            "profile.benchmark.revision must be a pinned 40-character Git SHA."
        )
    selection_raw = _object(raw["selection"], "profile.selection")
    _exact_keys(
        selection_raw,
        "profile.selection",
        {
            "pilotSize",
            "measuredSize",
            "minProjects",
            "maxPerProject",
            "maxChangedFiles",
            "maxChangedLines",
            "maxContextCharacters",
            "minimumPython",
            "projects",
        },
    )
    minimum_python_text = _text(
        selection_raw["minimumPython"],
        "profile.selection.minimumPython",
    )
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)", minimum_python_text)
    if match is None:
        raise EvaluationError(
            "profile.selection.minimumPython must be major.minor."
        )
    projects = _unique_text_array(
        selection_raw["projects"],
        "profile.selection.projects",
        minimum=1,
        maximum=32,
    )
    selection = SelectionSettings(
        pilot_size=_integer(
            selection_raw["pilotSize"],
            "profile.selection.pilotSize",
            1,
            100,
        ),
        measured_size=_integer(
            selection_raw["measuredSize"],
            "profile.selection.measuredSize",
            1,
            500,
        ),
        min_projects=_integer(
            selection_raw["minProjects"],
            "profile.selection.minProjects",
            1,
            32,
        ),
        max_per_project=_integer(
            selection_raw["maxPerProject"],
            "profile.selection.maxPerProject",
            1,
            100,
        ),
        max_changed_files=_integer(
            selection_raw["maxChangedFiles"],
            "profile.selection.maxChangedFiles",
            1,
            100,
        ),
        max_changed_lines=_integer(
            selection_raw["maxChangedLines"],
            "profile.selection.maxChangedLines",
            1,
            100_000,
        ),
        max_context_characters=_integer(
            selection_raw["maxContextCharacters"],
            "profile.selection.maxContextCharacters",
            1_024,
            500_000,
        ),
        minimum_python=(int(match.group(1)), int(match.group(2))),
        projects=projects,
    )
    if selection.min_projects > len(selection.projects):
        raise EvaluationError(
            "profile.selection.minProjects exceeds the project allowlist."
        )
    if (
        selection.measured_size
        > selection.max_per_project * len(selection.projects)
    ):
        raise EvaluationError(
            "Measured size cannot fit within maxPerProject and projects."
        )
    if (
        selection.measured_size
        > selection.max_per_project * selection.min_projects
        and selection.min_projects == len(selection.projects)
    ):
        raise EvaluationError(
            "Measured selection cannot satisfy its project ceiling."
        )
    storage_raw = _object(raw["storage"], "profile.storage")
    _exact_keys(
        storage_raw,
        "profile.storage",
        {"maxBytes", "minFreeBytes"},
    )
    storage = StorageSettings(
        max_bytes=_integer(
            storage_raw["maxBytes"],
            "profile.storage.maxBytes",
            1,
            10**15,
        ),
        min_free_bytes=_integer(
            storage_raw["minFreeBytes"],
            "profile.storage.minFreeBytes",
            1,
            10**15,
        ),
    )
    container_raw = _object(raw["container"], "profile.container")
    _exact_keys(
        container_raw,
        "profile.container",
        {"image", "digest", "platform"},
    )
    container_digest = _text(
        container_raw["digest"],
        "profile.container.digest",
    )
    if _IMAGE_DIGEST.fullmatch(container_digest) is None:
        raise EvaluationError(
            "profile.container.digest must be a sha256 image digest."
        )
    container = ContainerSettings(
        image=_text(container_raw["image"], "profile.container.image"),
        digest=container_digest,
        platform=_enum(
            container_raw["platform"],
            "profile.container.platform",
            {"linux/amd64", "linux/arm64"},
        ),
    )
    frontier_raw = _object(raw["frontier"], "profile.frontier")
    timeout_fields = {
        "armTimeoutSeconds",
        "planningTimeoutSeconds",
        "localTimeoutSeconds",
        "reviewTimeoutSeconds",
    }
    if schema_version == 1:
        _exact_keys(
            frontier_raw,
            "profile.frontier",
            {
                "command",
                "codexVersion",
                "model",
                "reasoningEffort",
                *timeout_fields,
            },
        )
        adapter = "codex-cli"
        provider = "openai-codex"
        command_version = _text(
            frontier_raw["codexVersion"],
            "profile.frontier.codexVersion",
        )
        codex_version = command_version
        reasoning_effort = _enum(
            frontier_raw["reasoningEffort"],
            "profile.frontier.reasoningEffort",
            {"low", "medium", "high", "xhigh", "max", "ultra"},
        )
        toolsets: tuple[str, ...] = ()
        context_window = 0
        max_completion_tokens = 0
    elif schema_version == 2:
        _exact_keys(
            frontier_raw,
            "profile.frontier",
            {
                "adapter",
                "command",
                "commandVersion",
                "provider",
                "model",
                "contextWindowTokens",
                "toolsets",
                *timeout_fields,
            },
        )
        adapter = _text(
            frontier_raw["adapter"],
            "profile.frontier.adapter",
        )
        provider = _text(
            frontier_raw["provider"],
            "profile.frontier.provider",
        )
        command_version = _text(
            frontier_raw["commandVersion"],
            "profile.frontier.commandVersion",
        )
        codex_version = ""
        reasoning_effort = ""
        toolsets = _unique_text_array(
            frontier_raw["toolsets"],
            "profile.frontier.toolsets",
            minimum=1,
            maximum=16,
        )
        context_window = _integer(
            frontier_raw["contextWindowTokens"],
            "profile.frontier.contextWindowTokens",
            1,
            2**20,
        )
        max_completion_tokens = 0
    else:
        _exact_keys(
            frontier_raw,
            "profile.frontier",
            {
                "adapter",
                "command",
                "commandVersion",
                "provider",
                "model",
                "contextWindowTokens",
                "maxCompletionTokens",
                "reasoningEffort",
                "toolsets",
                *timeout_fields,
            },
        )
        adapter = _text(
            frontier_raw["adapter"],
            "profile.frontier.adapter",
        )
        provider = _text(
            frontier_raw["provider"],
            "profile.frontier.provider",
        )
        command_version = _text(
            frontier_raw["commandVersion"],
            "profile.frontier.commandVersion",
        )
        codex_version = ""
        reasoning_effort = _enum(
            frontier_raw["reasoningEffort"],
            "profile.frontier.reasoningEffort",
            {"none", "low", "medium", "high"},
        )
        toolsets = _unique_text_array(
            frontier_raw["toolsets"],
            "profile.frontier.toolsets",
            minimum=0,
            maximum=16,
        )
        context_window = _integer(
            frontier_raw["contextWindowTokens"],
            "profile.frontier.contextWindowTokens",
            1,
            2**20,
        )
        max_completion_tokens = _integer(
            frontier_raw["maxCompletionTokens"],
            "profile.frontier.maxCompletionTokens",
            1,
            131_072,
        )
    if adapter not in _FRONTIER_ADAPTERS:
        raise EvaluationError(
            f"profile.frontier.adapter must be one of: "
            f"{', '.join(_FRONTIER_ADAPTERS)}."
        )
    for toolset in toolsets:
        if toolset not in _FRONTIER_TOOLSETS:
            raise EvaluationError(
                f"profile.frontier.toolsets must be one of: "
                f"{', '.join(_FRONTIER_TOOLSETS)}."
            )
    if adapter == "hermes-oneshot":
        if schema_version != 2:
            raise EvaluationError(
                "hermes-oneshot requires evaluation profile schema version 2."
            )
        if toolsets != ("todo",):
            raise EvaluationError(
                "hermes-oneshot adapter requires exactly the todo toolset."
            )
    elif adapter == "hermes-completion":
        if schema_version != 3:
            raise EvaluationError(
                "hermes-completion requires evaluation profile schema version 3."
            )
        if toolsets:
            raise EvaluationError(
                "hermes-completion adapter does not permit toolsets."
            )
    elif schema_version != 1:
        raise EvaluationError(
            "codex-cli evaluation profiles must use schema version 1."
        )
    frontier = FrontierSettings(
        command=_text(frontier_raw["command"], "profile.frontier.command"),
        codex_version=codex_version,
        model=_text(frontier_raw["model"], "profile.frontier.model"),
        reasoning_effort=reasoning_effort,
        arm_timeout_seconds=_integer(
            frontier_raw["armTimeoutSeconds"],
            "profile.frontier.armTimeoutSeconds",
            60,
            86_400,
        ),
        planning_timeout_seconds=_integer(
            frontier_raw["planningTimeoutSeconds"],
            "profile.frontier.planningTimeoutSeconds",
            30,
            86_400,
        ),
        local_timeout_seconds=_integer(
            frontier_raw["localTimeoutSeconds"],
            "profile.frontier.localTimeoutSeconds",
            30,
            86_400,
        ),
        review_timeout_seconds=_integer(
            frontier_raw["reviewTimeoutSeconds"],
            "profile.frontier.reviewTimeoutSeconds",
            30,
            86_400,
        ),
        adapter=adapter,
        provider=provider,
        command_version=command_version,
        toolsets=toolsets,
        context_window=context_window,
        max_completion_tokens=max_completion_tokens,
    )
    if (
        frontier.planning_timeout_seconds
        + frontier.local_timeout_seconds
        + frontier.review_timeout_seconds
        != frontier.arm_timeout_seconds
    ):
        raise EvaluationError(
            "Swarm phase timeouts must sum to armTimeoutSeconds."
        )
    local_raw = _object(raw["local"], "profile.local")
    _exact_keys(local_raw, "profile.local", {"maxRepair"})
    python_bootstrap = _unique_text_array(
        raw["pythonBootstrap"],
        "profile.pythonBootstrap",
        minimum=3,
        maximum=16,
    )
    for requirement in python_bootstrap:
        if not is_exact_requirement(requirement):
            raise EvaluationError(
                "profile.pythonBootstrap entries must use exact == pins."
            )
    dependency_roots_raw = _object(
        raw["dependencyRoots"],
        "profile.dependencyRoots",
    )
    if set(dependency_roots_raw) != set(selection.projects):
        raise EvaluationError(
            "profile.dependencyRoots keys must exactly match selection.projects."
        )
    dependency_roots: dict[str, tuple[str, ...]] = {}
    for project in selection.projects:
        roots = _unique_text_array(
            dependency_roots_raw[project],
            f"profile.dependencyRoots.{project}",
            minimum=0,
            maximum=64,
        )
        if any(
            re.fullmatch(r"[A-Za-z0-9_.-]+", root) is None
            for root in roots
        ):
            raise EvaluationError(
                f"profile.dependencyRoots.{project} contains an invalid package."
            )
        dependency_roots[project] = roots
    dependency_pins_raw = _object(
        raw["dependencyPins"],
        "profile.dependencyPins",
    )
    if set(dependency_pins_raw) != set(selection.projects):
        raise EvaluationError(
            "profile.dependencyPins keys must exactly match selection.projects."
        )
    dependency_pins: dict[str, tuple[str, ...]] = {}
    for project in selection.projects:
        pins = _unique_text_array(
            dependency_pins_raw[project],
            f"profile.dependencyPins.{project}",
            maximum=128,
        )
        if any(not is_exact_requirement(pin) for pin in pins):
            raise EvaluationError(
                f"profile.dependencyPins.{project} entries must use exact == pins."
            )
        dependency_pins[project] = pins
    profile_id = _identifier(raw["profileId"], "profile.profileId")
    return EvaluationProfile(
        source=path.resolve(),
        schema_version=schema_version,
        profile_id=profile_id,
        benchmark_repository=_text(
            benchmark["repository"],
            "profile.benchmark.repository",
        ),
        benchmark_revision=revision,
        seed=_integer(raw["seed"], "profile.seed", 0, 2**31 - 1),
        selection=selection,
        storage=storage,
        container=container,
        frontier=frontier,
        python_bootstrap=python_bootstrap,
        dependency_roots=dependency_roots,
        dependency_pins=dependency_pins,
        max_repair=_integer(
            local_raw["maxRepair"],
            "profile.local.maxRepair",
            0,
            5,
        ),
    )


def profile_payload(profile: EvaluationProfile) -> dict[str, Any]:
    payload = {
        "schemaVersion": profile.schema_version,
        "profileId": profile.profile_id,
        "benchmark": {
            "repository": profile.benchmark_repository,
            "revision": profile.benchmark_revision,
        },
        "seed": profile.seed,
        "selection": {
            "pilotSize": profile.selection.pilot_size,
            "measuredSize": profile.selection.measured_size,
            "minProjects": profile.selection.min_projects,
            "maxPerProject": profile.selection.max_per_project,
            "maxChangedFiles": profile.selection.max_changed_files,
            "maxChangedLines": profile.selection.max_changed_lines,
            "maxContextCharacters": profile.selection.max_context_characters,
            "minimumPython": (
                f"{profile.selection.minimum_python[0]}."
                f"{profile.selection.minimum_python[1]}"
            ),
            "projects": list(profile.selection.projects),
        },
        "storage": {
            "maxBytes": profile.storage.max_bytes,
            "minFreeBytes": profile.storage.min_free_bytes,
        },
        "container": {
            "image": profile.container.image,
            "digest": profile.container.digest,
            "platform": profile.container.platform,
        },
        "pythonBootstrap": list(profile.python_bootstrap),
        "dependencyRoots": {
            project: list(profile.dependency_roots[project])
            for project in profile.selection.projects
        },
        "dependencyPins": {
            project: list(profile.dependency_pins[project])
            for project in profile.selection.projects
        },
        "local": {"maxRepair": profile.max_repair},
    }
    timeouts = {
        "armTimeoutSeconds": profile.frontier.arm_timeout_seconds,
        "planningTimeoutSeconds": profile.frontier.planning_timeout_seconds,
        "localTimeoutSeconds": profile.frontier.local_timeout_seconds,
        "reviewTimeoutSeconds": profile.frontier.review_timeout_seconds,
    }
    if profile.schema_version == 1:
        payload["frontier"] = {
            "command": profile.frontier.command,
            "codexVersion": profile.frontier.codex_version,
            "model": profile.frontier.model,
            "reasoningEffort": profile.frontier.reasoning_effort,
            **timeouts,
        }
    elif profile.schema_version == 2:
        payload["frontier"] = {
            "adapter": profile.frontier.adapter,
            "command": profile.frontier.command,
            "commandVersion": profile.frontier.command_version,
            "provider": profile.frontier.provider,
            "model": profile.frontier.model,
            "contextWindowTokens": profile.frontier.context_window,
            "toolsets": list(profile.frontier.toolsets),
            **timeouts,
        }
    else:
        payload["frontier"] = {
            "adapter": profile.frontier.adapter,
            "command": profile.frontier.command,
            "commandVersion": profile.frontier.command_version,
            "provider": profile.frontier.provider,
            "model": profile.frontier.model,
            "contextWindowTokens": profile.frontier.context_window,
            "maxCompletionTokens": (
                profile.frontier.max_completion_tokens
            ),
            "reasoningEffort": profile.frontier.reasoning_effort,
            "toolsets": list(profile.frontier.toolsets),
            **timeouts,
        }
    return payload


def preliminary_evaluation_profile(
    profile: EvaluationProfile,
) -> EvaluationProfile:
    """Derive the fixed 2-calibration / 6-measured decision-gate profile."""
    if len(profile.selection.projects) < 6:
        raise EvaluationError(
            "Preliminary evaluation requires at least six allowed projects."
        )
    return replace(
        profile,
        profile_id=f"{profile.profile_id}-preliminary",
        selection=replace(
            profile.selection,
            pilot_size=2,
            measured_size=6,
            min_projects=6,
            max_per_project=1,
        ),
    )


def read_text_portable(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise EvaluationError(f"Benchmark metadata is not supported text: {path}")


def parse_info_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in read_text_portable(path).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _INFO_VALUE.fullmatch(line)
        if match is None:
            raise EvaluationError(f"Unsupported benchmark info line: {line}")
        if match.group(1) in result:
            raise EvaluationError(
                f"Duplicate benchmark info field: {match.group(1)}"
            )
        result[match.group(1)] = match.group(2)
    return result


def parse_benchmark_commands(path: Path) -> list[list[str]]:
    """Parse a deliberately narrow command subset without invoking a shell."""
    commands: list[list[str]] = []
    if not path.is_file():
        return commands
    for raw_line in read_text_portable(path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if any(marker in line for marker in _SHELL_META):
            raise EvaluationError(
                f"Benchmark command requires unsupported shell syntax: {line}"
            )
        try:
            argv = shlex.split(line)
        except ValueError as exc:
            raise EvaluationError(
                f"Benchmark command is malformed: {line}"
            ) from exc
        if not argv or argv[0] not in _SAFE_BENCHMARK_COMMANDS:
            raise EvaluationError(
                f"Benchmark command is not allowlisted: {line}"
            )
        if len(argv) > 128 or any(len(value) > 8_192 for value in argv):
            raise EvaluationError("Benchmark command exceeds safety limits.")
        commands.append(argv)
    if path.name == "run_test.sh" and not commands:
        raise EvaluationError("Benchmark case has no verification commands.")
    return commands


def patch_metadata(patch: str) -> dict[str, Any]:
    if any(
        marker in patch
        for marker in (
            "GIT binary patch",
            "Binary files ",
            "Subproject commit ",
            "new file mode 160000",
            "old file mode 160000",
        )
    ):
        raise EvaluationError(
            "Reference patch contains binary or submodule content."
        )
    if any(
        line.startswith(("rename from ", "rename to ", "copy from ", "copy to "))
        for line in patch.splitlines()
    ):
        raise EvaluationError("Reference patch contains rename/copy metadata.")
    paths: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        match = _DIFF_HEADER.fullmatch(line)
        if match is None or match.group(1) != match.group(2):
            raise EvaluationError("Reference patch contains rename/copy metadata.")
        path = match.group(1)
        if path.startswith("/") or ".." in Path(path).parts:
            raise EvaluationError("Reference patch path escapes the project.")
        paths.append(path)
    if not paths or len(paths) != len(set(paths)):
        raise EvaluationError(
            "Reference patch must contain unique unified diff paths."
        )
    changed_lines = sum(
        1
        for line in patch.splitlines()
        if (
            (line.startswith("+") or line.startswith("-"))
            and not line.startswith(("+++", "---"))
        )
    )
    return {
        "paths": paths,
        "changedFiles": len(paths),
        "changedLines": changed_lines,
        "sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
    }


def enumerate_bugsinpy_candidates(
    metadata_root: Path,
    profile: EvaluationProfile,
) -> list[dict[str, Any]]:
    """Enumerate statically eligible candidates without exposing patch text."""
    candidates: list[dict[str, Any]] = []
    projects_root = metadata_root / "projects"
    for project in profile.selection.projects:
        project_dir = projects_root / project
        project_info = parse_info_file(project_dir / "project.info")
        repository = project_info.get("github_url")
        if not repository or project_info.get("status") != "OK":
            continue
        bug_dirs = sorted(
            (project_dir / "bugs").glob("*"),
            key=lambda value: (
                int(value.name) if value.name.isdigit() else 2**31,
                value.name,
            ),
        )
        for bug_dir in bug_dirs:
            if not bug_dir.is_dir() or not bug_dir.name.isdigit():
                continue
            try:
                info = parse_info_file(bug_dir / "bug.info")
                patch = read_text_portable(bug_dir / "bug_patch.txt")
                patch_info = patch_metadata(patch)
                setup = parse_benchmark_commands(bug_dir / "setup.sh")
                verification = parse_benchmark_commands(
                    bug_dir / "run_test.sh"
                )
                requirements = read_text_portable(
                    bug_dir / "requirements.txt"
                )
                python_version = _python_version(info.get("python_version"))
                buggy_commit = _commitish(
                    info.get("buggy_commit_id"),
                    "buggy_commit_id",
                )
                fixed_commit = _commitish(
                    info.get("fixed_commit_id"),
                    "fixed_commit_id",
                )
                test_files = tuple(
                    value
                    for value in info.get("test_file", "").split(";")
                    if value
                )
                if not test_files:
                    raise EvaluationError("Benchmark case has no test files.")
                _safe_relative_paths(test_files, "test_file")
                if python_version < profile.selection.minimum_python:
                    continue
                if (
                    patch_info["changedFiles"]
                    > profile.selection.max_changed_files
                    or patch_info["changedLines"]
                    > profile.selection.max_changed_lines
                ):
                    continue
                if any(
                    _is_non_production_path(value)
                    for value in patch_info["paths"]
                ):
                    continue
                if any(
                    Path(value).suffix.lower()
                    in {".c", ".cc", ".cpp", ".h", ".hpp", ".pxd", ".pyx"}
                    for value in patch_info["paths"]
                ):
                    continue
                requirement_lines = _requirement_lines(
                    requirements,
                    project=project,
                )
                dependency_requirements(
                    requirement_lines,
                    project,
                    profile,
                )
            except (EvaluationError, OSError):
                continue
            candidates.append({
                "caseId": f"{project.lower()}-{bug_dir.name}",
                "project": project,
                "bugId": int(bug_dir.name),
                "repository": repository,
                "buggyCommit": buggy_commit,
                "fixedCommit": fixed_commit,
                "pythonVersion": (
                    f"{python_version[0]}.{python_version[1]}"
                ),
                "testFiles": list(test_files),
                "setupArgv": setup,
                "verificationArgv": verification,
                "requirements": requirement_lines,
                "reference": patch_info,
            })
    if not candidates:
        raise EvaluationError("No statically eligible BugsInPy cases found.")
    candidates.sort(key=lambda value: value["caseId"])
    _assign_patch_strata(candidates)
    return candidates


def select_cases(
    candidates: Sequence[dict[str, Any]],
    profile: EvaluationProfile,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select disjoint pilot/measured sets with deterministic quotas."""
    rng = random.Random(profile.seed)
    by_project: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_project.setdefault(candidate["project"], []).append(candidate)
    projects = [
        project
        for project in profile.selection.projects
        if by_project.get(project)
    ]
    if len(projects) < profile.selection.min_projects:
        raise EvaluationError(
            "Not enough projects have eligible benchmark cases."
        )
    capacities = {
        project: min(
            len(by_project[project]),
            profile.selection.max_per_project,
        )
        for project in projects
    }
    if sum(capacities.values()) < profile.selection.measured_size:
        raise EvaluationError(
            "Measured selection cannot fit within the available project caps."
        )
    if profile.selection.measured_size < profile.selection.min_projects:
        raise EvaluationError(
            "Measured size cannot cover the minimum project count."
        )
    included = projects[: min(len(projects), profile.selection.measured_size)]
    quotas = {project: (1 if project in included else 0) for project in projects}
    remaining = profile.selection.measured_size - sum(quotas.values())
    for project in projects:
        if remaining <= 0:
            break
        available = capacities[project] - quotas[project]
        granted = min(available, remaining)
        quotas[project] += granted
        remaining -= granted
    if remaining:
        raise EvaluationError("Measured selection exhausted its quotas.")
    if sum(value > 0 for value in quotas.values()) < profile.selection.min_projects:
        raise EvaluationError("Measured suite does not cover enough projects.")
    shuffled: dict[str, list[dict[str, Any]]] = {}
    for project in projects:
        values = list(by_project[project])
        rng.shuffle(values)
        values.sort(
            key=lambda value: (
                value["reference"]["stratum"],
                hashlib.sha256(
                    f"{profile.seed}:{value['caseId']}".encode()
                ).hexdigest(),
            )
        )
        shuffled[project] = values
    pilot: list[dict[str, Any]] = []
    pilot_projects = [
        project
        for project in projects
        if len(shuffled[project]) > quotas[project]
    ]
    pilot_projects.sort(
        key=lambda project: (
            any(
                any("build_ext" in arg for arg in command)
                for value in shuffled[project]
                for command in value["setupArgv"]
            ),
            projects.index(project),
        )
    )
    inexpensive = [
        project
        for project in pilot_projects
        if not any(
            any("build_ext" in arg for arg in command)
            for value in shuffled[project]
            for command in value["setupArgv"]
        )
    ]
    if sum(
        len(shuffled[project]) - quotas[project]
        for project in inexpensive
    ) >= profile.selection.pilot_size:
        pilot_projects = inexpensive
    project_index = 0
    while len(pilot) < profile.selection.pilot_size:
        if not pilot_projects:
            raise EvaluationError(
                "Pilot selection has no cases beyond measured reservations."
            )
        project = pilot_projects[project_index % len(pilot_projects)]
        project_index += 1
        values = shuffled[project]
        if len(values) <= quotas[project]:
            pilot_projects.remove(project)
            project_index = 0
            continue
        counts = {
            stratum: sum(
                value["reference"]["stratum"] == stratum
                for value in pilot
            )
            for stratum in ("small", "medium", "large")
        }
        preferred = min(
            ("small", "medium", "large"),
            key=lambda key: (counts[key], key),
        )
        selected_index = next(
            (
                index
                for index in range(len(values))
                if values[index]["reference"]["stratum"] == preferred
            ),
            0,
        )
        pilot.append(values.pop(selected_index))
    measured: list[dict[str, Any]] = []
    per_project: dict[str, int] = {project: 0 for project in projects}
    while len(measured) < profile.selection.measured_size:
        progress = False
        for project in projects:
            if len(measured) >= profile.selection.measured_size:
                break
            if (
                per_project[project] >= quotas[project]
            ):
                continue
            values = shuffled[project]
            if not values:
                continue
            # Prefer the currently least represented patch-size stratum.
            counts = {
                stratum: sum(
                    value["reference"]["stratum"] == stratum
                    for value in measured
                )
                for stratum in ("small", "medium", "large")
            }
            preferred = min(counts, key=lambda key: (counts[key], key))
            selected_index = next(
                (
                    index
                    for index, value in enumerate(values)
                    if value["reference"]["stratum"] == preferred
                ),
                0,
            )
            measured.append(values.pop(selected_index))
            per_project[project] += 1
            progress = True
        if not progress:
            raise EvaluationError("Measured selection exhausted its quotas.")
    if len({value["project"] for value in measured}) < profile.selection.min_projects:
        raise EvaluationError("Measured suite does not cover enough projects.")
    if len({value["caseId"] for value in pilot + measured}) != len(
        pilot + measured
    ):
        raise EvaluationError("Pilot and measured selections overlap.")
    _rebalance_strata(measured, candidates, pilot, projects, profile)
    return pilot, measured


def evaluation_case(
    candidate: dict[str, Any],
    phase: str,
) -> dict[str, Any]:
    if phase not in {"pilot", "measured"}:
        raise EvaluationError("Evaluation case phase is invalid.")
    return {
        **candidate,
        "phase": phase,
        "objective": (
            f"Repair BugsInPy case {candidate['caseId']} so every "
            "frozen verification command passes without modifying "
            "tests or benchmark evidence."
        ),
    }


def parse_codex_usage_jsonl(text: str) -> dict[str, Any]:
    """Aggregate exact usage from every Codex turn.completed JSONL event."""
    prompt_tokens = 0
    cached_input_tokens = 0
    completion_tokens = 0
    reasoning_tokens = 0
    total_tokens = 0
    turns = 0
    malformed = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        input_value = _usage_integer(
            usage,
            ("input_tokens", "prompt_tokens"),
        )
        output_value = _usage_integer(
            usage,
            ("output_tokens", "completion_tokens"),
        )
        cached_value = _usage_integer(
            usage,
            ("cached_input_tokens",),
            default=0,
        )
        reasoning_value = _usage_integer(
            usage,
            ("reasoning_output_tokens", "reasoning_tokens"),
            default=0,
        )
        total_value = _usage_integer(
            usage,
            ("total_tokens",),
            default=input_value + output_value,
        )
        prompt_tokens += input_value
        cached_input_tokens += cached_value
        completion_tokens += output_value
        reasoning_tokens += reasoning_value
        total_tokens += total_value
        turns += 1
    if turns == 0:
        return {
            "usageStatus": "unavailable",
            "turns": 0,
            "promptTokens": None,
            "cachedInputTokens": None,
            "completionTokens": None,
            "reasoningTokens": None,
            "totalTokens": None,
            "malformedLines": malformed,
        }
    return {
        "usageStatus": "reported",
        "turns": turns,
        "promptTokens": prompt_tokens,
        "cachedInputTokens": cached_input_tokens,
        "completionTokens": completion_tokens,
        "reasoningTokens": reasoning_tokens,
        "totalTokens": total_tokens,
        "malformedLines": malformed,
    }


def parse_hermes_usage_json(
    text: str,
    *,
    expected_provider: str | None = None,
    expected_model: str | None = None,
) -> dict[str, Any]:
    """Parse one Hermes ``--usage-file`` JSON receipt into frontier usage.

    The Hermes usage schema is a single JSON object (not JSONL).  Every field
    is strictly validated: non-negative integers, exact provider/model
    identity, ``completed == true``, ``failed == false``,
    ``api_calls == 1``, and ``total_tokens == input_tokens + output_tokens``.
    Any violation makes the measurement invalid rather than score zero.

    When *expected_provider* and *expected_model* are supplied the receipt
    must carry matching ``provider`` and ``model`` strings; otherwise the
    result is unavailable.  This keeps the identity check inside the parser
    so callers never see a "reported" receipt that failed identity validation.
    """
    unavailable = {
        "usageStatus": "unavailable",
        "turns": 0,
        "promptTokens": None,
        "cachedInputTokens": None,
        "completionTokens": None,
        "reasoningTokens": None,
        "totalTokens": None,
        "malformedLines": 1,
    }
    text = text.strip()
    if not text:
        return {**unavailable}
    try:
        receipt = json.loads(text)
    except json.JSONDecodeError:
        return {**unavailable}
    if not isinstance(receipt, dict):
        return {**unavailable}
    try:
        input_tokens = _strict_usage_int(receipt, "input_tokens")
        output_tokens = _strict_usage_int(receipt, "output_tokens")
        cache_read = _strict_usage_int(receipt, "cache_read_tokens")
        cache_write = _strict_usage_int(receipt, "cache_write_tokens")
        reasoning = _strict_usage_int(receipt, "reasoning_tokens")
        total = _strict_usage_int(receipt, "total_tokens")
        api_calls = _strict_usage_int(receipt, "api_calls")
    except EvaluationError:
        return {**unavailable}
    if api_calls != 1:
        return {**unavailable}
    if total != input_tokens + output_tokens:
        return {**unavailable}
    completed = receipt.get("completed")
    failed = receipt.get("failed")
    if not isinstance(completed, bool) or not isinstance(failed, bool):
        return {**unavailable}
    if not completed or failed:
        return {**unavailable}
    if expected_provider is not None:
        provider = receipt.get("provider")
        if not isinstance(provider, str) or provider != expected_provider:
            return {**unavailable}
    if expected_model is not None:
        model = receipt.get("model")
        if not isinstance(model, str) or model != expected_model:
            return {**unavailable}
    return {
        "usageStatus": "reported",
        "turns": api_calls,
        "promptTokens": input_tokens,
        "cachedInputTokens": cache_read,
        "completionTokens": output_tokens,
        "reasoningTokens": reasoning,
        "totalTokens": total,
        "malformedLines": 0,
        "cacheWriteTokens": cache_write,
    }


def _strict_usage_int(
    receipt: dict[str, Any],
    key: str,
) -> int:
    value = receipt.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EvaluationError(
            f"Hermes usage field {key} must be a non-negative integer."
        )
    return value


def validate_arm_result(value: Any) -> dict[str, Any]:
    """Validate one immutable per-arm measurement."""
    result = _object(value, "armResult")
    _exact_keys(
        result,
        "armResult",
        {
            "schemaVersion",
            "caseId",
            "phase",
            "arm",
            "status",
            "completed",
            "score",
            "elapsedSeconds",
            "phaseSeconds",
            "frontierUsage",
            "localUsage",
            "repairs",
            "modelLoads",
            "reviewVerdict",
            "patch",
            "oracle",
            "recordedAt",
        },
    )
    if _integer(result["schemaVersion"], "armResult.schemaVersion", 1, 100) != 1:
        raise EvaluationError("Unsupported arm result schema version.")
    _identifier(result["caseId"], "armResult.caseId")
    _enum(result["phase"], "armResult.phase", {"pilot", "measured"})
    _enum(result["arm"], "armResult.arm", {"frontier-alone", "mlx-swarm"})
    _enum(
        result["status"],
        "armResult.status",
        {"completed", "failed", "timed_out", "invalid"},
    )
    if not isinstance(result["completed"], bool):
        raise EvaluationError("armResult.completed must be boolean.")
    _integer(result["score"], "armResult.score", 0, 1)
    _number(
        result["elapsedSeconds"],
        "armResult.elapsedSeconds",
        0,
        10**9,
    )
    phase_seconds = _object(
        result["phaseSeconds"],
        "armResult.phaseSeconds",
    )
    allowed_phase_names = (
        {"frontier", "oracle"}
        if result["arm"] == "frontier-alone"
        else {"planning", "local", "review", "oracle"}
    )
    unknown_phases = set(phase_seconds) - allowed_phase_names
    if unknown_phases:
        raise EvaluationError(
            "armResult.phaseSeconds contains unknown phases: "
            + ", ".join(sorted(unknown_phases))
        )
    for name, seconds in phase_seconds.items():
        _number(
            seconds,
            f"armResult.phaseSeconds.{name}",
            0,
            10**9,
        )
    _validate_frontier_usage(
        result["frontierUsage"],
        "armResult.frontierUsage",
    )
    _validate_local_usage(result["localUsage"], "armResult.localUsage")
    _integer(result["repairs"], "armResult.repairs", 0, 10**9)
    _integer(result["modelLoads"], "armResult.modelLoads", 0, 10**9)
    review = result["reviewVerdict"]
    if review is not None:
        _enum(
            review,
            "armResult.reviewVerdict",
            {"approved", "changes_requested", "rejected"},
        )
    patch = _object(result["patch"], "armResult.patch")
    _exact_keys(
        patch,
        "armResult.patch",
        {"sha256", "changedFiles"},
    )
    if patch["sha256"] is not None and (
        not isinstance(patch["sha256"], str)
        or _SHA256.fullmatch(patch["sha256"]) is None
    ):
        raise EvaluationError("armResult.patch.sha256 must be null or SHA-256.")
    _integer(patch["changedFiles"], "armResult.patch.changedFiles", 0, 100_000)
    oracle = _object(result["oracle"], "armResult.oracle")
    _exact_keys(
        oracle,
        "armResult.oracle",
        {"passed", "exitCode", "evidence"},
    )
    if not isinstance(oracle["passed"], bool):
        raise EvaluationError("armResult.oracle.passed must be boolean.")
    if int(result["score"]) != int(oracle["passed"]):
        raise EvaluationError(
            "armResult.score must equal the executable oracle result."
        )
    if oracle["exitCode"] is not None:
        _integer(
            oracle["exitCode"],
            "armResult.oracle.exitCode",
            -2**31,
            2**31 - 1,
        )
    _text(
        oracle["evidence"],
        "armResult.oracle.evidence",
        allow_empty=True,
        maximum=MAX_LOG_BYTES,
    )
    _text(result["recordedAt"], "armResult.recordedAt")
    if result["completed"] != (result["status"] == "completed"):
        raise EvaluationError(
            "armResult.completed must match status=completed."
        )
    if result["arm"] == "frontier-alone":
        if (
            any(result["localUsage"].values())
            or result["repairs"] != 0
            or result["modelLoads"] != 0
            or result["reviewVerdict"] is not None
        ):
            raise EvaluationError(
                "Frontier-alone results cannot contain local or review metrics."
            )
    return result


def aggregate_results(
    suite: dict[str, Any],
    arm_results: Sequence[dict[str, Any]],
    *,
    bootstrap_samples: int = 10_000,
) -> dict[str, Any]:
    """Build paired metrics without mixing frontier and local tokens."""
    measured_ids = [
        case["caseId"]
        for case in suite["cases"]
        if case["phase"] == "measured"
    ]
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_result in arm_results:
        result = validate_arm_result(raw_result)
        key = (result["caseId"], result["arm"])
        if key in indexed:
            raise EvaluationError(
                f"Duplicate arm result for {result['caseId']} {result['arm']}."
            )
        indexed[key] = result
    rows: list[dict[str, Any]] = []
    for case_id in measured_ids:
        frontier = indexed.get((case_id, "frontier-alone"))
        swarm = indexed.get((case_id, "mlx-swarm"))
        if frontier is None or swarm is None:
            continue
        frontier_tokens = _reported_total(frontier["frontierUsage"])
        swarm_tokens = _reported_total(swarm["frontierUsage"])
        rows.append({
            "caseId": case_id,
            "project": next(
                case["project"]
                for case in suite["cases"]
                if case["caseId"] == case_id
            ),
            "frontier": frontier,
            "swarm": swarm,
            "frontierTokenDelta": (
                frontier_tokens - swarm_tokens
                if frontier_tokens is not None and swarm_tokens is not None
                else None
            ),
            "timeDeltaSeconds": (
                float(frontier["elapsedSeconds"])
                - float(swarm["elapsedSeconds"])
            ),
        })
    complete_pairs = len(rows)
    all_usage_valid = (
        complete_pairs == len(measured_ids)
        and all(row["frontierTokenDelta"] is not None for row in rows)
    )
    token_deltas = [
        int(row["frontierTokenDelta"])
        for row in rows
        if row["frontierTokenDelta"] is not None
    ]
    lower = None
    upper = None
    if token_deltas:
        lower, upper = bootstrap_mean_interval(
            token_deltas,
            seed=int(suite["seed"]),
            samples=bootstrap_samples,
        )
    frontier_metrics = _arm_aggregate(
        [row["frontier"] for row in rows],
        include_local=False,
    )
    swarm_metrics = _arm_aggregate(
        [row["swarm"] for row in rows],
        include_local=True,
    )
    accepted_both = [
        row
        for row in rows
        if row["frontier"]["score"] == 1 and row["swarm"]["score"] == 1
    ]
    accepted_both_deltas = [
        int(row["frontierTokenDelta"])
        for row in accepted_both
        if row["frontierTokenDelta"] is not None
    ]
    claim_established = bool(
        len(measured_ids) == 30
        and all_usage_valid
        and frontier_metrics["completed"] <= swarm_metrics["completed"]
        and frontier_metrics["score"] <= swarm_metrics["score"]
        and lower is not None
        and lower > 0
    )
    summary = {
        "schemaVersion": RESULT_SCHEMA_VERSION,
        "suiteId": suite["suiteId"],
        "measuredCases": len(measured_ids),
        "completePairs": complete_pairs,
        "frontierAlone": frontier_metrics,
        "mlxSwarm": swarm_metrics,
        "paired": {
            "frontierTokensSaved": sum(token_deltas) if token_deltas else None,
            "frontierTokensSavedMedian": (
                statistics.median(token_deltas) if token_deltas else None
            ),
            "frontierTokensSavedPercent": _percentage(
                sum(token_deltas),
                frontier_metrics["frontierTokens"],
            )
            if token_deltas
            else None,
            "bootstrap95": {
                "lower": lower,
                "upper": upper,
                "samples": bootstrap_samples,
            },
            "acceptedByBothCases": len(accepted_both),
            "acceptedByBothTokensSaved": (
                sum(accepted_both_deltas)
                if accepted_both_deltas
                else None
            ),
            "allUsageValid": all_usage_valid,
        },
        "claim": {
            "status": (
                "established"
                if claim_established
                else "tradeoff_measured"
                if complete_pairs == len(measured_ids)
                else "incomplete"
            ),
            "text": (
                "MLX Swarm saves frontier tokens without reducing completion "
                "or executable acceptance."
                if claim_established
                else "Token savings at completion and acceptance parity are "
                "not established by this study."
            ),
        },
        "rows": rows,
        "generatedAt": utc_now(),
    }
    return summary


def mark_preliminary_summary(
    summary: dict[str, Any],
    *,
    source_evaluation_id: str,
) -> dict[str, Any]:
    """Disable the product claim and add the six-pair decision gate."""
    summary["studyType"] = "preliminary_6_pair"
    summary["sourceEvaluationId"] = source_evaluation_id
    summary["claim"] = {
        "status": "preliminary",
        "text": (
            "Measured scores, time, and tokens are directional. "
            "The 30-pair product claim gate was not evaluated."
        ),
    }
    frontier_score = summary["frontierAlone"]["score"]
    swarm_score = summary["mlxSwarm"]["score"]
    measured_cases = int(summary["measuredCases"])
    summary["decisionGate"] = {
        "status": (
            "continue_to_full_study"
            if swarm_score >= frontier_score
            else "stop_and_improve_workers"
        ),
        "text": (
            "Acceptance is materially behind "
            f"({swarm_score}/{measured_cases} vs "
            f"{frontier_score}/{measured_cases}). Improve local worker patch "
            "quality before running the 30-pair study."
            if swarm_score < frontier_score
            else "Acceptance is not behind in this preliminary set; "
            "a full 30-pair study may be considered."
        ),
    }
    return summary


def apply_protocol_audit(
    summary: dict[str, Any],
    environment: dict[str, Any],
) -> dict[str, Any]:
    """Prevent pre-fix or asymmetric studies from being reported as paired evidence."""
    observed = environment.get("evaluationProtocolVersion")
    valid = observed == FAIR_EVALUATION_PROTOCOL_VERSION
    summary["protocolAudit"] = {
        "status": "valid" if valid else "invalid",
        "observedVersion": observed,
        "requiredVersion": FAIR_EVALUATION_PROTOCOL_VERSION,
        "invariants": [
            "one immutable task packet and write-root contract for both arms",
            "exact contiguous workspace source excerpts",
            "Git recount support for semantically valid diff hunks",
            "immutable prompt and output evidence for every local attempt",
            "Docker context is frozen into verifier profiles",
            "verifier infrastructure failures are invalid measurements",
            "measured work requires a two-case zero-frontier local replay gate",
        ],
        "issues": (
            []
            if valid
            else [
                (
                    "This study predates the symmetric write-root, source-"
                    "packet, source-fidelity, patch-recount, and per-attempt "
                    "evidence contract."
                )
            ]
        ),
    }
    if not valid:
        summary["claim"] = {
            "status": "protocol_invalid",
            "text": (
                "The recorded rows are diagnostic history, not a fair paired "
                "economics comparison. A new evaluation must be prepared and "
                "run under the current protocol."
            ),
        }
        summary["decisionGate"] = {
            "status": "rerun_fair_protocol",
            "text": (
                "Do not use this study to judge worker acceptance or token "
                "economics; rerun the preliminary suite under the current "
                "symmetric protocol."
            ),
        }
    return summary


def bootstrap_mean_interval(
    values: Sequence[int | float],
    *,
    seed: int,
    samples: int = 10_000,
) -> tuple[float, float]:
    if not values:
        raise EvaluationError("Bootstrap requires at least one value.")
    if samples < 100:
        raise EvaluationError("Bootstrap requires at least 100 samples.")
    rng = random.Random(seed)
    size = len(values)
    means = sorted(
        sum(values[rng.randrange(size)] for _ in range(size)) / size
        for _ in range(samples)
    )
    lower_index = max(0, math.floor(0.025 * (samples - 1)))
    upper_index = min(samples - 1, math.ceil(0.975 * (samples - 1)))
    return float(means[lower_index]), float(means[upper_index])


def render_readme_economics(summary: dict[str, Any]) -> str:
    """Render deterministic aggregate and per-case Markdown tables."""
    frontier = summary["frontierAlone"]
    swarm = summary["mlxSwarm"]
    paired = summary["paired"]
    preliminary = summary.get("studyType") == "preliminary_6_pair"
    lines = [
        (
            "## Preliminary measured economics"
            if preliminary
            else "## Measured economics"
        ),
        "",
        f"**Study status:** `{summary['claim']['status']}` — "
        f"{summary['claim']['text']}",
        "",
    ]
    protocol_audit = summary.get("protocolAudit")
    if isinstance(protocol_audit, dict):
        protocol_status = protocol_audit.get("status", "unknown")
        if protocol_status == "invalid":
            lines.extend([
                (
                    "**Protocol audit:** `invalid` — The tables below are "
                    "retained to diagnose the old run, but they are not a "
                    "valid paired comparison."
                ),
                "",
            ])
        elif protocol_status == "valid":
            lines.extend([
                (
                    "**Protocol audit:** `valid` — Both arms used identical "
                    "write roots; local file context was verified verbatim; "
                    "all local generation attempts were retained."
                ),
                "",
            ])
    if preliminary:
        lines.extend([
            (
                "**Preliminary 6-pair study.** This is a directional decision "
                "gate, not the planned 30-pair claim study. The strong "
                "“saves frontier tokens without reducing acceptance” claim "
                "is disabled regardless of the observed deltas."
            ),
            "",
        ])
        decision = summary.get("decisionGate")
        if isinstance(decision, dict):
            lines.extend([
                (
                    f"**Decision gate:** `{decision.get('status', 'unknown')}` "
                    f"— {decision.get('text', '')}"
                ),
                "",
            ])
    study = summary.get("study")
    if isinstance(study, dict):
        local_model = study.get("localModel") or {}
        hardware = study.get("hardware") or {}
        reasoning = study.get("reasoningEffort")
        reasoning_label = f" ({reasoning})" if reasoning else ""
        adapter = study.get("frontierAdapter", "unknown")
        provider = study.get("frontierProvider", "unknown")
        frontier_version = (
            study.get("frontierCommandVersion")
            or study.get("codexVersion")
            or "unknown"
        )
        lines.extend([
            (
                f"Pinned protocol: `{study.get('benchmark', 'BugsInPy')}@"
                f"{study.get('benchmarkRevision', 'unknown')}` · "
                f"`{study.get('frontierModel', 'unknown')}`"
                f"{reasoning_label} via `{adapter}` / `{provider}` · local "
                f"`{local_model.get('repository', 'unknown')}@"
                f"{local_model.get('fingerprint', 'unknown')}` · "
                f"seed `{study.get('seed', 'unknown')}`."
            ),
            "",
            (
                f"Recorded `{study.get('recordedAt', 'unknown')}` on "
                f"`{hardware.get('machine', 'unknown')}` / "
                f"`{hardware.get('processor', 'unknown')}` with "
                f"{format_bytes(hardware.get('memoryBytes'))}. "
                f"MLX Swarm commit `{study.get('mlxSwarmCommit', 'unknown')}`; "
                f"frontier command `{frontier_version}`."
            ),
            "",
        ])
    preparation = summary.get("preparation")
    if isinstance(preparation, dict):
        lines.extend([
            (
                "One-time case preparation (excluded from task timing): "
                f"{format_duration(preparation.get('totalSeconds'))} across "
                f"{preparation.get('caseCount', 0)} cases."
            ),
            "",
        ])
    lines.extend([
        "Scores are binary executable-oracle results. Times are end-to-end "
        "wall time and exclude one-time benchmark preparation. Frontier and "
        "local tokens are intentionally separate. This pass@1 study is one "
        "suite on one machine; it does not establish monetary savings or "
        "generalize beyond the pinned protocol.",
        "",
        "| Metric | Frontier Alone | MLX Swarm | Delta |",
        "|---|---:|---:|---:|",
        (
            f"| Completed | {frontier['completed']}/{summary['measuredCases']} "
            f"({frontier['completionRate']:.1f}%) "
            f"| {swarm['completed']}/{summary['measuredCases']} "
            f"({swarm['completionRate']:.1f}%) "
            f"| {_signed(swarm['completed'] - frontier['completed'])} |"
        ),
        (
            f"| Score | {frontier['score']}/{summary['measuredCases']} "
            f"| {swarm['score']}/{summary['measuredCases']} "
            f"| {_signed(swarm['score'] - frontier['score'])} |"
        ),
        (
            f"| Median end-to-end time | "
            f"{format_duration(frontier['medianElapsedSeconds'])} "
            f"| {format_duration(swarm['medianElapsedSeconds'])} "
            f"| {_format_time_percentage(frontier['medianElapsedSeconds'], swarm['medianElapsedSeconds'])} |"
        ),
        (
            f"| Frontier tokens (total / median) "
            f"| {format_integer(frontier['frontierTokens'])} / "
            f"{format_integer(frontier['medianFrontierTokens'])} "
            f"| {format_integer(swarm['frontierTokens'])} / "
            f"{format_integer(swarm['medianFrontierTokens'])} "
            f"| {format_integer(paired['frontierTokensSaved'])} "
            f"{'fewer' if preliminary else 'saved'} "
            f"({format_percentage(paired['frontierTokensSavedPercent'])}) |"
        ),
        (
            f"| Local tokens (total / median) | — "
            f"| {format_integer(swarm['localTokens'])} / "
            f"{format_integer(swarm['medianLocalTokens'])} "
            "| separate |"
        ),
        (
            f"| Repairs (total / median) | — "
            f"| {format_integer(swarm['repairs'])} / "
            f"{format_integer(swarm['medianRepairs'])} | — |"
        ),
        f"| Model loads | — | {format_integer(swarm['modelLoads'])} | — |",
        "",
        "| Task | Project | Frontier score | Frontier time | Frontier tokens "
        "| Swarm score | Swarm time | Swarm frontier tokens | Local tokens "
        "| Repairs | Loads | Review | Token delta | Time delta |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|",
    ])
    for row in summary["rows"]:
        frontier_result = row["frontier"]
        swarm_result = row["swarm"]
        lines.append(
            f"| [{row['caseId']}](benchmarks/results/{summary['suiteId']}/"
            f"cases/{row['caseId']}.json) | {row['project']} "
            f"| {frontier_result['score']} "
            f"| {format_duration(frontier_result['elapsedSeconds'])} "
            f"| {format_integer(_reported_total(frontier_result['frontierUsage']))} "
            f"| {swarm_result['score']} "
            f"| {format_duration(swarm_result['elapsedSeconds'])} "
            f"| {format_integer(_reported_total(swarm_result['frontierUsage']))} "
            f"| {format_integer(_local_total(swarm_result['localUsage']))} "
            f"| {swarm_result['repairs']} "
            f"| {swarm_result['modelLoads']} "
            f"| {swarm_result['reviewVerdict'] or 'not eligible'} "
            f"| {format_integer(row['frontierTokenDelta'])} "
            f"| {_signed_duration(row['timeDeltaSeconds'])} |"
        )
    lines.extend([
        "",
        f"Study: `{summary['suiteId']}` · paired cases: "
        f"{summary['completePairs']}/{summary['measuredCases']} · "
        f"95% bootstrap token-saving interval: "
        f"{_format_interval(paired['bootstrap95']['lower'], paired['bootstrap95']['upper'])}. "
        f"Accepted-by-both savings: "
        f"{format_integer(paired['acceptedByBothTokensSaved'])} tokens across "
        f"{paired['acceptedByBothCases']} cases.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def preliminary_study_subset(
    suite: dict[str, Any],
    results: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Select a non-cherry-picked 2-calibration / 6-measured subset."""
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_result in results:
        result = validate_arm_result(raw_result)
        key = (result["caseId"], result["arm"])
        if key in indexed:
            raise EvaluationError(
                f"Duplicate arm result for {result['caseId']} {result['arm']}."
            )
        indexed[key] = result

    def eligible(case: dict[str, Any]) -> bool:
        paired = [
            indexed.get((case["caseId"], arm))
            for arm in ("frontier-alone", "mlx-swarm")
        ]
        if any(result is None for result in paired):
            return False
        assert all(result is not None for result in paired)
        return all(
            result["status"] != "invalid"
            and result["frontierUsage"]["usageStatus"] == "reported"
            and oracle_infrastructure_failure(result["oracle"]) is None
            for result in paired
        )

    pilots = [
        case
        for case in suite["cases"]
        if case["phase"] == "pilot" and eligible(case)
    ][:2]
    if len(pilots) != 2:
        raise EvaluationError(
            "Preliminary report requires two valid calibration pairs."
        )
    measured_candidates = [
        case
        for case in suite["cases"]
        if case["phase"] == "measured" and eligible(case)
    ]
    measured: list[dict[str, Any]] | None = None
    for candidate_group in combinations(measured_candidates, 6):
        projects = {case["project"] for case in candidate_group}
        strata = {
            name: sum(
                case["reference"]["stratum"] == name
                for case in candidate_group
            )
            for name in ("small", "medium", "large")
        }
        if len(projects) == 6 and strata == {
            "small": 2,
            "medium": 2,
            "large": 2,
        }:
            measured = list(candidate_group)
            break
    if measured is None:
        raise EvaluationError(
            "Preliminary report requires six valid projects with 2/2/2 strata."
        )
    selected = [*pilots, *measured]
    selected_ids = {case["caseId"] for case in selected}
    selected_results = [
        indexed[(case["caseId"], arm)]
        for case in selected
        for arm in ("frontier-alone", "mlx-swarm")
    ]
    calibration_results = [
        result
        for result in selected_results
        if result["caseId"] in {case["caseId"] for case in pilots}
    ]
    report_id = f"{suite['suiteId']}-preliminary-6"
    subset = {
        **suite,
        "suiteId": report_id,
        "cases": [
            case for case in suite["cases"] if case["caseId"] in selected_ids
        ],
    }
    return subset, selected_results, calibration_results


def render_calibration_results(results: Sequence[dict[str, Any]]) -> str:
    lines = [
        "## Calibration evidence",
        "",
        "Calibration validates the harness and does not enter headline scores.",
        "",
        "| Task | Arm | Status | Score | Time | Frontier tokens |",
        "|---|---|---|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result['caseId']} | {result['arm']} | {result['status']} "
            f"| {result['score']} | {format_duration(result['elapsedSeconds'])} "
            f"| {format_integer(_reported_total(result['frontierUsage']))} |"
        )
    if not results:
        lines.append("| — | — | not available | — | — | — |")
    return "\n".join(lines).rstrip() + "\n"


def update_readme_economics(
    readme_path: Path,
    rendered: str,
    *,
    check: bool = False,
) -> bool:
    """Replace one generated README block, or verify it without writing."""
    text = readme_path.read_text(encoding="utf-8")
    block = f"{README_START}\n{rendered.rstrip()}\n{README_END}"
    if README_START in text or README_END in text:
        if text.count(README_START) != 1 or text.count(README_END) != 1:
            raise EvaluationError("README economics markers are malformed.")
        start = text.index(README_START)
        end = text.index(README_END) + len(README_END)
        updated = text[:start] + block + text[end:]
    else:
        updated = text.rstrip() + "\n\n" + block + "\n"
    changed = updated != text
    if check:
        if changed:
            raise EvaluationError("README economics table is out of date.")
        return False
    if changed:
        readme_path.write_text(updated, encoding="utf-8")
    return changed


class EvaluationStore:
    """Filesystem-backed immutable evaluation ledger."""

    def __init__(
        self,
        config: SwarmConfig,
        *,
        root: Path | None = None,
    ):
        self.config = config
        try:
            self.workspace_root = discover_git_root(config.source.parent)
        except WorkspaceError:
            self.workspace_root = config.source.parent.resolve()
        self.root = (
            root.resolve()
            if root is not None
            else (
                self.workspace_root / DEFAULT_EVALUATIONS_DIR
            ).resolve()
        )
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare(
        self,
        profile: EvaluationProfile,
        *,
        clone: Callable[[EvaluationProfile, Path], Path] | None = None,
        resume_evaluation_id: str | None = None,
    ) -> dict[str, Any]:
        if profile.frontier.adapter == "hermes-oneshot":
            raise EvaluationError(
                "hermes-oneshot is retained for historical evidence only; "
                "new evaluations must use hermes-completion."
            )
        source = mlx_swarm_source_revision()
        if source["dirty"]:
            raise EvaluationError(
                "MLX Swarm source is dirty; commit the benchmark harness "
                "before freezing an evaluation."
            )
        inspect_frontier_version(profile)
        container = inspect_container(profile)
        self._check_storage(profile)
        excluded: list[dict[str, Any]] = []
        if resume_evaluation_id is None:
            evaluation_id = (
                f"{profile.profile_id}-"
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            ).lower()
            evaluation_dir = self.root / evaluation_id
            try:
                evaluation_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError as exc:
                raise EvaluationError(
                    f"Evaluation already exists: {evaluation_id}"
                ) from exc
            _atomic_json(
                evaluation_dir / "profile.snapshot.json",
                profile_payload(profile),
            )
            clone_fn = clone or clone_benchmark_metadata
            metadata_root = clone_fn(
                profile,
                evaluation_dir / "benchmark",
            )
        else:
            evaluation_id = _identifier(
                resume_evaluation_id,
                "evaluationId",
            )
            evaluation_dir = self._dir(evaluation_id)
            sealed = [
                name
                for name in (
                    "evaluation.json",
                    "environment.json",
                )
                if (evaluation_dir / name).exists()
            ]
            if sealed:
                raise EvaluationError(
                    "Prepared evaluations are immutable and cannot resume "
                    f"preparation ({', '.join(sealed)} already exists)."
                )
            snapshot_path = evaluation_dir / "profile.snapshot.json"
            if not snapshot_path.is_file():
                raise EvaluationError(
                    "Interrupted evaluation has no profile snapshot."
                )
            expected_profile = canonical_json_sha256(
                profile_payload(profile)
            )
            actual_profile = canonical_json_sha256(
                _read_json(snapshot_path)
            )
            if actual_profile != expected_profile:
                raise EvaluationError(
                    "Evaluation profile differs from the interrupted snapshot."
                )
            suite_path = evaluation_dir / "suite.json"
            if suite_path.is_file():
                suite = _read_json(suite_path)
                validate_suite(suite, profile)
                for case in suite["cases"]:
                    runtime_path = (
                        evaluation_dir
                        / "cases"
                        / case["caseId"]
                        / "runtime.json"
                    )
                    if not runtime_path.is_file():
                        raise EvaluationError(
                            "Interrupted finalization is missing a selected "
                            f"case runtime: {case['caseId']}."
                        )
                remove_sensitive_preparation_sources(evaluation_dir)
                state = {
                    "schemaVersion": EVALUATION_SCHEMA_VERSION,
                    "evaluationId": evaluation_id,
                    "status": "prepared",
                    "pilotStatus": "pending",
                    "measuredStatus": "locked",
                    "createdAt": utc_now(),
                    "updatedAt": utc_now(),
                    "results": {},
                }
                _atomic_json(evaluation_dir / "evaluation.json", state)
                _atomic_json(
                    evaluation_dir / "environment.json",
                    environment_fingerprint(
                        self.config,
                        profile,
                        container=container,
                    ),
                )
                return self.detail(evaluation_id)
            metadata_root = evaluation_dir / "benchmark"
            if not metadata_root.is_dir():
                raise EvaluationError(
                    "Interrupted evaluation no longer has preparation metadata."
                )
            exclusions_path = (
                evaluation_dir / "preparation-exclusions.json"
            )
            if exclusions_path.is_file():
                excluded = list(
                    _read_json(exclusions_path).get("cases", [])
                )
        candidates = enumerate_bugsinpy_candidates(metadata_root, profile)
        resolve_case_commits(
            candidates,
            evaluation_dir / "cache" / "repositories",
        )
        excluded_ids = {
            value.get("caseId")
            for value in excluded
            if isinstance(value, dict)
            and isinstance(value.get("caseId"), str)
        }
        candidates = [
            value
            for value in candidates
            if value["caseId"] not in excluded_ids
        ]
        runner = EvaluationRunner(self.config, self, profile)
        while True:
            pilot, measured = select_cases(candidates, profile)
            cases = [
                evaluation_case(value, phase)
                for phase, values in (
                    ("pilot", pilot),
                    ("measured", measured),
                )
                for value in values
            ]
            failures: list[dict[str, Any]] = []
            for case in cases:
                try:
                    runtime_path = (
                        evaluation_dir
                        / "cases"
                        / case["caseId"]
                        / "runtime.json"
                    )
                    was_prepared = runtime_path.is_file()
                    runner.prepare_case(
                        evaluation_dir,
                        case,
                        retain_mirror=True,
                    )
                    if not was_prepared:
                        self._check_storage(profile)
                except Exception as exc:
                    failures.append({
                        "caseId": case["caseId"],
                        "reason": str(exc)[:8_000],
                    })
                    shutil.rmtree(
                        evaluation_dir / "cases" / case["caseId"],
                        ignore_errors=True,
                    )
            if not failures:
                break
            failed_ids = {value["caseId"] for value in failures}
            excluded.extend(failures)
            _atomic_json(
                evaluation_dir / "preparation-exclusions.json",
                {
                    "schemaVersion": 1,
                    "cases": excluded,
                    "recordedAt": utc_now(),
                },
            )
            candidates = [
                value
                for value in candidates
                if value["caseId"] not in failed_ids
            ]
        selected_ids = {case["caseId"] for case in cases}
        cases_root = evaluation_dir / "cases"
        if cases_root.is_dir():
            for case_dir in cases_root.iterdir():
                if (
                    case_dir.is_dir()
                    and case_dir.name not in selected_ids
                ):
                    shutil.rmtree(case_dir)
        self._check_storage(profile)
        _atomic_json(
            evaluation_dir / "preparation-exclusions.json",
            {
                "schemaVersion": 1,
                "cases": excluded,
                "recordedAt": utc_now(),
            },
        )
        source_after = mlx_swarm_source_revision()
        if source_after != source:
            raise EvaluationError(
                "MLX Swarm source changed while preparing the evaluation."
            )
        suite = {
            "schemaVersion": SUITE_SCHEMA_VERSION,
            "suiteId": evaluation_id,
            "profileId": profile.profile_id,
            "benchmark": {
                "name": "BugsInPy",
                "repository": profile.benchmark_repository,
                "revision": profile.benchmark_revision,
            },
            "seed": profile.seed,
            "createdAt": utc_now(),
            "cases": cases,
        }
        validate_suite(suite, profile)
        _atomic_json(evaluation_dir / "suite.json", suite)
        # Metadata can contain reference patches and mirrors contain future
        # commits. The frozen suite is sufficient to reproduce selection, so
        # remove both before any model process can inspect an arm workspace.
        remove_sensitive_preparation_sources(evaluation_dir)
        state = {
            "schemaVersion": EVALUATION_SCHEMA_VERSION,
            "evaluationId": evaluation_id,
            "status": "prepared",
            "pilotStatus": "pending",
            "measuredStatus": "locked",
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
            "results": {},
        }
        _atomic_json(evaluation_dir / "evaluation.json", state)
        _atomic_json(
            evaluation_dir / "environment.json",
            environment_fingerprint(
                self.config,
                profile,
                container=container,
            ),
        )
        return self.detail(evaluation_id)

    def list(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return values
        for path in self.root.glob("*/evaluation.json"):
            try:
                state = _read_json(path)
            except (EvaluationError, OSError):
                continue
            values.append({
                "evaluationId": state.get("evaluationId"),
                "status": state.get("status"),
                "pilotStatus": state.get("pilotStatus"),
                "measuredStatus": state.get("measuredStatus"),
                "updatedAt": state.get("updatedAt"),
                "dir": str(path.parent),
            })
        values.sort(key=lambda value: value.get("updatedAt") or "", reverse=True)
        return values

    def detail(self, evaluation_id: str) -> dict[str, Any]:
        evaluation_dir = self._dir(evaluation_id)
        state = _read_json(evaluation_dir / "evaluation.json")
        suite = _read_json(evaluation_dir / "suite.json")
        summary = None
        if (evaluation_dir / "summary.json").is_file():
            summary = _read_json(evaluation_dir / "summary.json")
        results = self.load_results(evaluation_id)
        return {
            "evaluation": state,
            "suite": suite,
            "summary": summary,
            "results": results,
            "environment": _read_json(
                evaluation_dir / "environment.json"
            ),
        }

    def load_results(self, evaluation_id: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        results_root = self._dir(evaluation_id) / "results"
        if not results_root.is_dir():
            return results
        for path in sorted(results_root.glob("*/*.json")):
            try:
                results.append(validate_arm_result(_read_json(path)))
            except (EvaluationError, OSError):
                continue
        return results

    def record_result(
        self,
        evaluation_id: str,
        result: dict[str, Any],
    ) -> None:
        result = validate_arm_result(result)
        evaluation_dir = self._dir(evaluation_id)
        suite = _read_json(evaluation_dir / "suite.json")
        case_ids = {case["caseId"] for case in suite["cases"]}
        if result["caseId"] not in case_ids:
            raise EvaluationError("Arm result case is absent from the suite.")
        path = (
            evaluation_dir
            / "results"
            / result["caseId"]
            / f"{result['arm']}.json"
        )
        _exclusive_json(path, result)
        state_path = evaluation_dir / "evaluation.json"
        state = _read_json(state_path)
        state["updatedAt"] = utc_now()
        state.setdefault("results", {})[
            f"{result['caseId']}:{result['arm']}"
        ] = str(path.relative_to(evaluation_dir))
        _atomic_json(state_path, state)

    def finalize_phase(
        self,
        evaluation_id: str,
        phase: str,
    ) -> dict[str, Any]:
        if phase not in {"pilot", "measured"}:
            raise EvaluationError("Evaluation phase must be pilot or measured.")
        evaluation_dir = self._dir(evaluation_id)
        state_path = evaluation_dir / "evaluation.json"
        state = _read_json(state_path)
        suite = _read_json(evaluation_dir / "suite.json")
        expected = {
            (case["caseId"], arm)
            for case in suite["cases"]
            if case["phase"] == phase
            for arm in ("frontier-alone", "mlx-swarm")
        }
        observed = {
            (result["caseId"], result["arm"])
            for result in self.load_results(evaluation_id)
            if result["phase"] == phase
        }
        missing = sorted(expected - observed)
        if missing:
            raise EvaluationError(
                f"Evaluation phase is incomplete; missing {len(missing)} arm results."
            )
        if phase == "pilot":
            # Calibration unlocks measured work only when evidence itself is
            # valid. A separate local replay gate must then prove every local
            # worker case before any measured frontier spend is allowed.
            state["pilotStatus"] = "completed"
            state["measuredStatus"] = "locked_local_replay"
            state["status"] = "pilot_completed_local_replay_required"
        else:
            if state.get("pilotStatus") != "completed":
                raise EvaluationError(
                    "Measured phase remains locked until pilot completion."
                )
            environment = _read_json(evaluation_dir / "environment.json")
            if (
                environment.get("evaluationProtocolVersion")
                != FAIR_EVALUATION_PROTOCOL_VERSION
            ):
                raise EvaluationError(
                    "Measured phase remains locked because the frozen study "
                    "predates the current evaluation protocol."
                )
            replay_gate = state.get("localReplayGate")
            if (
                not isinstance(replay_gate, dict)
                or replay_gate.get("status") != "passed"
            ):
                raise EvaluationError(
                    "Measured phase remains locked until the local replay "
                    "calibration gate passes."
                )
            state["measuredStatus"] = "completed"
            state["status"] = "completed"
        state["updatedAt"] = utc_now()
        _atomic_json(state_path, state)
        if phase == "measured":
            summary = aggregate_results(
                suite,
                self.load_results(evaluation_id),
            )
            if str(suite.get("profileId", "")).endswith("-preliminary"):
                mark_preliminary_summary(
                    summary,
                    source_evaluation_id=evaluation_id,
                )
            summary["preparation"] = preparation_summary(
                evaluation_dir,
                suite,
            )
            summary["study"] = study_context(
                suite,
                _read_json(evaluation_dir / "environment.json"),
            )
            apply_protocol_audit(
                summary,
                _read_json(evaluation_dir / "environment.json"),
            )
            _atomic_json(evaluation_dir / "summary.json", summary)
        return self.detail(evaluation_id)

    def report(
        self,
        evaluation_id: str,
        export_dir: Path,
        *,
        check: bool = False,
        preliminary: bool = False,
    ) -> dict[str, Any]:
        detail = self.detail(evaluation_id)
        report_suite = detail["suite"]
        report_results = detail["results"]
        calibration_source: Sequence[dict[str, Any]] = report_results
        if preliminary:
            (
                report_suite,
                report_results,
                calibration_source,
            ) = preliminary_study_subset(
                detail["suite"],
                detail["results"],
            )
            summary = aggregate_results(report_suite, report_results)
            summary["generatedAt"] = max(
                result["recordedAt"] for result in report_results
            )
            mark_preliminary_summary(
                summary,
                source_evaluation_id=evaluation_id,
            )
            summary["preparation"] = preparation_summary(
                self._dir(evaluation_id),
                report_suite,
            )
            summary["study"] = study_context(
                report_suite,
                detail["environment"],
            )
        else:
            summary = detail["summary"]
            if summary is None:
                raise EvaluationError(
                    "Measured phase must be complete before exporting a report."
                )
        apply_protocol_audit(summary, detail["environment"])
        export_dir = export_dir.resolve()
        if not check and export_dir.exists() and any(export_dir.iterdir()):
            raise EvaluationError("Report export directory must be empty.")
        sanitized_rows: list[dict[str, Any]] = []
        for row in summary["rows"]:
            sanitized = sanitize_public_row(row)
            sanitized_rows.append(sanitized)
        public_summary = {
            **summary,
            "rows": sanitized_rows,
        }
        public_study = {
            "schemaVersion": RESULT_SCHEMA_VERSION,
            "suite": sanitize_suite(report_suite),
            "environment": sanitize_environment(detail["environment"]),
        }
        calibration = [
            _sanitize_arm(result)
            for result in calibration_source
            if result["phase"] == "pilot"
        ]
        calibration.sort(key=lambda value: (value["caseId"], value["arm"]))
        readme_report = render_readme_economics(public_summary)
        detailed_report = (
            readme_report.rstrip()
            + "\n\n"
            + render_calibration_results(calibration)
        )
        if check:
            expected_paths = {
                Path("summary.json"),
                Path("study.json"),
                Path("calibration.json"),
                Path("report.md"),
                *{
                    Path("cases") / f"{row['caseId']}.json"
                    for row in sanitized_rows
                },
            }
            actual_paths = {
                path.relative_to(export_dir)
                for path in export_dir.rglob("*")
                if path.is_file()
            } if export_dir.is_dir() else set()
            if actual_paths != expected_paths:
                raise EvaluationError(
                    "Sanitized evaluation export is missing or out of date."
                )
            if (
                _read_json(export_dir / "summary.json") != public_summary
                or _read_json(export_dir / "study.json") != public_study
                or _read_json(export_dir / "calibration.json").get("results")
                != calibration
                or (export_dir / "report.md").read_text(encoding="utf-8")
                != detailed_report
            ):
                raise EvaluationError(
                    "Sanitized evaluation export is out of date."
                )
            for row in sanitized_rows:
                if _read_json(
                    export_dir / "cases" / f"{row['caseId']}.json"
                ) != row:
                    raise EvaluationError(
                        "Sanitized case evidence is out of date."
                    )
        else:
            export_dir.mkdir(parents=True, exist_ok=True)
            cases_dir = export_dir / "cases"
            cases_dir.mkdir()
            for row in sanitized_rows:
                _atomic_json(
                    cases_dir / f"{row['caseId']}.json",
                    row,
                )
            _atomic_json(export_dir / "summary.json", public_summary)
            _atomic_json(export_dir / "study.json", public_study)
            _atomic_json(
                export_dir / "calibration.json",
                {
                    "schemaVersion": RESULT_SCHEMA_VERSION,
                    "results": calibration,
                },
            )
            (export_dir / "report.md").write_text(
                detailed_report,
                encoding="utf-8",
            )
        return {
            "evaluationId": evaluation_id,
            "reportId": public_summary["suiteId"],
            "exportDir": str(export_dir),
            "summary": public_summary,
            "readmeMarkdown": readme_report,
        }

    def _dir(self, evaluation_id: str) -> Path:
        _identifier(evaluation_id, "evaluationId")
        path = (self.root / evaluation_id).resolve()
        if not _is_within(path, self.root) or not path.is_dir():
            raise EvaluationError(f"Evaluation not found: {evaluation_id}")
        return path

    def _check_storage(self, profile: EvaluationProfile) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self.root)
        if usage.free < profile.storage.min_free_bytes:
            raise EvaluationError(
                "Evaluation storage reserve is not available: "
                f"{usage.free} free, {profile.storage.min_free_bytes} required."
            )
        current = directory_size(self.root)
        if current > profile.storage.max_bytes:
            raise EvaluationError(
                "Evaluation root already exceeds its configured storage ceiling."
            )


class EvaluationRunner:
    """Execute paired frontier-alone and MLX Swarm arms sequentially."""

    def __init__(
        self,
        config: SwarmConfig,
        store: EvaluationStore,
        profile: EvaluationProfile,
    ):
        self.config = config
        self.store = store
        self.profile = profile

    def run_phase(
        self,
        evaluation_id: str,
        phase: str,
    ) -> dict[str, Any]:
        if phase not in {"pilot", "measured"}:
            raise EvaluationError("Evaluation phase must be pilot or measured.")
        detail = self.store.detail(evaluation_id)
        current_source = mlx_swarm_source_revision()
        frozen_commit = detail["environment"].get("mlxSwarmCommit")
        if (
            current_source["dirty"]
            or current_source["commit"] != frozen_commit
        ):
            raise EvaluationError(
                "MLX Swarm source differs from the prepared evaluation."
            )
        profile_digest = canonical_json_sha256(profile_payload(self.profile))
        if profile_digest != detail["environment"].get("profileSha256"):
            raise EvaluationError(
                "Evaluation profile differs from the prepared evaluation."
            )
        current_frontier = inspect_frontier_version(self.profile)
        frozen_frontier = (
            detail["environment"].get("frontierCommandVersion")
            or detail["environment"].get("codexVersion")
        )
        if current_frontier != frozen_frontier:
            raise EvaluationError(
                "Frontier command differs from the prepared evaluation."
            )
        current_container = inspect_container(self.profile)
        frozen_container = (
            detail["environment"].get("runtime", {}).get("container")
        )
        if current_container != frozen_container:
            raise EvaluationError(
                "Benchmark container differs from the prepared environment."
            )
        current_runtime = python_runtime_identity()
        frozen_runtime = detail["environment"].get("runtime", {})
        frozen_python = {
            key: frozen_runtime.get(key)
            for key in ("python", "pythonExecutable", "packages")
        }
        if current_runtime != frozen_python:
            raise EvaluationError(
                "Python/MLX runtime differs from the prepared environment."
            )
        state = detail["evaluation"]
        if phase == "measured":
            if state.get("pilotStatus") != "completed":
                raise EvaluationError(
                    "Measured work is locked until calibration completes."
                )
            if (
                detail["environment"].get("evaluationProtocolVersion")
                != FAIR_EVALUATION_PROTOCOL_VERSION
            ):
                raise EvaluationError(
                    "Measured work is locked because the frozen study predates "
                    "the current evaluation protocol."
                )
            replay_gate = state.get("localReplayGate")
            if (
                not isinstance(replay_gate, dict)
                or replay_gate.get("status") != "passed"
            ):
                raise EvaluationError(
                    "Measured work is locked until every frozen local "
                    "calibration replay passes its independent oracle."
                )
        evaluation_dir = self.store._dir(evaluation_id)
        suite = validate_suite(detail["suite"], self.profile)
        existing = {
            (result["caseId"], result["arm"])
            for result in detail["results"]
        }
        cases = [case for case in suite["cases"] if case["phase"] == phase]
        for case in cases:
            missing_arms = [
                arm
                for arm in ("frontier-alone", "mlx-swarm")
                if (case["caseId"], arm) not in existing
            ]
            if not missing_arms:
                continue
            with exclusive_case_lock(evaluation_dir, case["caseId"]):
                self.store._check_storage(self.profile)
                try:
                    runtime = self.prepare_case(evaluation_dir, case)
                except Exception as exc:
                    for arm in missing_arms:
                        self.store.record_result(
                            evaluation_id,
                            invalid_arm_result(
                                case,
                                arm,
                                f"Case preparation failed: {exc}",
                            ),
                        )
                    continue
                order = list(missing_arms)
                random.Random(
                    f"{suite['seed']}:{case['caseId']}"
                ).shuffle(order)
                for arm in order:
                    try:
                        result = (
                            self.run_frontier_alone(case, runtime)
                            if arm == "frontier-alone"
                            else self.run_mlx_swarm(case, runtime)
                        )
                        self.store._check_storage(self.profile)
                    except Exception as exc:
                        result = invalid_arm_result(
                            case,
                            arm,
                            f"Arm execution failed: {exc}",
                        )
                    self.store.record_result(evaluation_id, result)
        if phase == "pilot":
            pilot_results = [
                result
                for result in self.store.load_results(evaluation_id)
                if result["phase"] == "pilot"
            ]
            expected = len(cases) * 2
            if len(pilot_results) == expected and any(
                result["status"] == "invalid"
                or result["frontierUsage"]["usageStatus"] != "reported"
                for result in pilot_results
            ):
                state_path = evaluation_dir / "evaluation.json"
                invalid_state = _read_json(state_path)
                invalid_state["pilotStatus"] = "invalid"
                invalid_state["measuredStatus"] = "locked"
                invalid_state["status"] = "pilot_invalid"
                invalid_state["updatedAt"] = utc_now()
                _atomic_json(state_path, invalid_state)
                raise EvaluationError(
                    "Pilot evidence is invalid; measured work remains locked."
                )
            validate_pilot_evidence(
                evaluation_dir,
                cases,
                pilot_results,
                self.profile,
                self.store,
            )
        return self.store.finalize_phase(evaluation_id, phase)

    def prepare_case(
        self,
        evaluation_dir: Path,
        case: dict[str, Any],
        *,
        retain_mirror: bool = False,
    ) -> dict[str, Any]:
        """Create a history-free case snapshot and prove its oracle."""
        started = time.perf_counter()
        case_root = evaluation_dir / "cases" / case["caseId"]
        runtime_path = case_root / "runtime.json"
        if runtime_path.is_file():
            runtime = _read_json(runtime_path)
            base = Path(runtime["baseSnapshot"])
            environment = Path(runtime["environment"])
            expected_dependency = dependency_environment_receipt(
                case,
                self.profile,
            )
            ready_path = environment / ".mlx-swarm-ready.json"
            ready = (
                _read_json(ready_path)
                if ready_path.is_file()
                else {}
            )
            if (
                base.is_dir()
                and environment.is_dir()
                and runtime.get("containerDigest")
                == self.profile.container.digest
                and runtime.get("dependencyContractSha256")
                == expected_dependency["sha256"]
                and ready.get("contract") == expected_dependency
            ):
                return runtime
            raise EvaluationError(
                "Prepared case runtime differs from the frozen profile."
            )
        case_root.mkdir(parents=True, exist_ok=True)
        for partial in ("base", "fixed-validation", "preflight"):
            partial_path = case_root / partial
            if partial_path.is_dir() and not partial_path.is_symlink():
                shutil.rmtree(partial_path)
            elif partial_path.exists() or partial_path.is_symlink():
                partial_path.unlink()
        (case_root / "verifier.json").unlink(missing_ok=True)
        mirror = (
            evaluation_dir
            / "cache"
            / "repositories"
            / f"{case['project']}.git"
        )
        if not mirror.is_dir():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            _run_checked(
                [
                    "git",
                    "clone",
                    "--mirror",
                    case["repository"],
                    str(mirror),
                ],
                cwd=mirror.parent,
                timeout=900,
            )
        else:
            _run_checked(
                ["git", "--git-dir", str(mirror), "fetch", "--prune", "origin"],
                cwd=mirror.parent,
                timeout=600,
            )
        for commit in (case["buggyCommit"], case["fixedCommit"]):
            result = run_command(
                ["git", "--git-dir", str(mirror), "cat-file", "-e", commit],
                cwd=mirror.parent,
                timeout=30,
            )
            if result.returncode != 0:
                raise EvaluationError(
                    f"Project mirror does not contain pinned commit {commit}."
                )
        work_root = case_root / "preflight"
        buggy_checkout = work_root / "buggy"
        fixed_checkout = work_root / "fixed"
        if work_root.exists():
            shutil.rmtree(work_root)
        work_root.mkdir(parents=True)
        _git_worktree_add(mirror, buggy_checkout, case["buggyCommit"])
        _git_worktree_add(mirror, fixed_checkout, case["fixedCommit"])
        try:
            copy_fixed_test_support(
                fixed_checkout,
                buggy_checkout,
                case["testFiles"],
            )
            base_snapshot = case_root / "base"
            if base_snapshot.exists():
                raise EvaluationError("Case base snapshot already exists.")
            validate_repository_symlinks(buggy_checkout)
            shutil.copytree(
                buggy_checkout,
                base_snapshot,
                ignore=shutil.ignore_patterns(".git"),
                symlinks=True,
            )
            environment = dependency_environment_path(
                evaluation_dir,
                case,
                self.profile,
            )
            self._prepare_environment(
                case,
                environment,
                evaluation_dir,
            )
            self._run_setup(
                case,
                base_snapshot,
                environment,
                evaluation_dir,
            )
            _remove_generated_state(base_snapshot)
            _init_snapshot_repository(base_snapshot)
            verifier_manifest = {
                "schemaVersion": 1,
                "caseId": case["caseId"],
                "evaluationRoot": str(evaluation_dir),
                "containerImage": self.profile.container.image,
                "containerDigest": self.profile.container.digest,
                "containerPlatform": self.profile.container.platform,
                "environment": str(environment),
                "commands": case["verificationArgv"],
                "timeoutSeconds": min(
                    self.profile.frontier.local_timeout_seconds,
                    1_800,
                ),
            }
            _atomic_json(case_root / "verifier.json", verifier_manifest)
            buggy_result = run_case_verifier(
                case_root / "verifier.json",
                base_snapshot,
            )
            unsupported_failure = oracle_infrastructure_failure(
                buggy_result
            )
            if unsupported_failure is not None:
                raise EvaluationError(unsupported_failure)
            fixed_runtime = case_root / "fixed-validation"
            validate_repository_symlinks(fixed_checkout)
            shutil.copytree(
                fixed_checkout,
                fixed_runtime,
                ignore=shutil.ignore_patterns(".git"),
                symlinks=True,
            )
            try:
                self._run_setup(
                    case,
                    fixed_runtime,
                    environment,
                    evaluation_dir,
                    cache_writes=False,
                )
                _remove_generated_state(fixed_runtime)
                fixed_result = run_case_verifier(
                    case_root / "verifier.json",
                    fixed_runtime,
                )
            finally:
                shutil.rmtree(fixed_runtime, ignore_errors=True)
            stable_buggy_result = run_case_verifier(
                case_root / "verifier.json",
                base_snapshot,
            )
            unsupported_failure = oracle_infrastructure_failure(
                stable_buggy_result
            )
            if unsupported_failure is not None:
                raise EvaluationError(unsupported_failure)
            _atomic_json(
                case_root / "preflight-oracle.json",
                {
                    "schemaVersion": 1,
                    "caseId": case["caseId"],
                    "buggy": {
                        **buggy_result,
                        "evidence": buggy_result["evidence"][:MAX_LOG_BYTES],
                    },
                    "fixed": {
                        **fixed_result,
                        "evidence": fixed_result["evidence"][:MAX_LOG_BYTES],
                    },
                    "buggyAfterFixed": {
                        **stable_buggy_result,
                        "evidence": (
                            stable_buggy_result["evidence"][:MAX_LOG_BYTES]
                        ),
                    },
                    "recordedAt": utc_now(),
                },
            )
            if buggy_result["passed"]:
                raise EvaluationError(
                    "Buggy snapshot unexpectedly passes the frozen oracle."
                )
            if not fixed_result["passed"]:
                raise EvaluationError(
                    "Fixed snapshot does not pass the frozen oracle."
                )
            if stable_buggy_result["passed"]:
                raise EvaluationError(
                    "Buggy snapshot passed after fixed-oracle validation."
                )
            failure_evidence = stable_buggy_result["evidence"][:40_000]
            executed_source_lines = collect_buggy_execution_trace(
                case,
                evaluation_root=evaluation_dir,
                workspace=base_snapshot,
                environment=environment,
                profile=self.profile,
            )
            provisional_runtime = {
                "baseSnapshot": str(base_snapshot),
                "failureEvidence": failure_evidence,
                "executedSourceLines": executed_source_lines,
            }
            _tree, source_context = deterministic_case_context(
                case,
                provisional_runtime,
            )
            runtime_local_evidence = collect_buggy_runtime_locals(
                case,
                evaluation_root=evaluation_dir,
                workspace=base_snapshot,
                environment=environment,
                profile=self.profile,
                source_context=source_context,
            )
            _remove_generated_state(base_snapshot)
            runtime = {
                "schemaVersion": 1,
                "caseId": case["caseId"],
                "evaluationRoot": str(evaluation_dir),
                "baseSnapshot": str(base_snapshot),
                "baseSha": _git_text(base_snapshot, ["rev-parse", "HEAD"]),
                "environment": str(environment),
                "containerDigest": self.profile.container.digest,
                "dependencyContractSha256": (
                    dependency_environment_receipt(
                        case,
                        self.profile,
                    )["sha256"]
                ),
                "verifierManifest": str(case_root / "verifier.json"),
                "failureEvidence": failure_evidence,
                "executedSourceLines": executed_source_lines,
                "runtimeLocalEvidence": runtime_local_evidence,
                "preparationSeconds": time.perf_counter() - started,
                "buggyOracleSeconds": (
                    buggy_result["elapsedSeconds"]
                    + stable_buggy_result["elapsedSeconds"]
                ),
                "fixedOracleSeconds": fixed_result["elapsedSeconds"],
                "preparedAt": utc_now(),
            }
            _atomic_json(runtime_path, runtime)
            return runtime
        finally:
            _git_worktree_remove(mirror, buggy_checkout)
            _git_worktree_remove(mirror, fixed_checkout)
            if not retain_mirror:
                shutil.rmtree(mirror, ignore_errors=True)
            shutil.rmtree(work_root, ignore_errors=True)

    def _prepare_environment(
        self,
        case: dict[str, Any],
        environment: Path,
        evaluation_dir: Path,
    ) -> None:
        ready_path = environment / ".mlx-swarm-ready.json"
        expected = dependency_environment_receipt(case, self.profile)
        if ready_path.is_file():
            ready = _read_json(ready_path)
            if (
                ready.get("contract") == expected
                and isinstance(ready.get("resolvedLock"), list)
                and ready["resolvedLock"]
            ):
                return
        if environment.exists():
            shutil.rmtree(environment)
        environment.parent.mkdir(parents=True, exist_ok=True)
        container_environment = container_path(
            environment,
            evaluation_dir,
        )
        create_argv = docker_case_argv(
            self.profile,
            evaluation_dir,
            evaluation_dir,
            [
                "uv",
                "venv",
                "--python",
                case["pythonVersion"],
                container_environment,
            ],
            network="bridge",
        )
        _run_checked(
            create_argv,
            cwd=evaluation_dir,
            timeout=900,
        )
        _run_checked(
            docker_case_argv(
                self.profile,
                evaluation_dir,
                evaluation_dir,
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    f"{container_environment}/bin/python",
                    *self.profile.python_bootstrap,
                ],
                network="bridge",
            ),
            cwd=evaluation_dir,
            timeout=900,
        )
        requirements = case_dependency_constraints(case, self.profile)
        roots = dependency_requirements(
            requirements,
            case["project"],
            self.profile,
        )
        constraints_path = (
            environment.parent / f"{environment.name}.constraints.txt"
        )
        constraints_text = "\n".join(requirements) + "\n"
        constraints_path.write_text(constraints_text, encoding="utf-8")
        if roots:
            _run_checked(
                docker_case_argv(
                    self.profile,
                    evaluation_dir,
                    evaluation_dir,
                    [
                        "uv",
                        "pip",
                        "install",
                        "--python",
                        f"{container_environment}/bin/python",
                        "--constraint",
                        container_path(constraints_path, evaluation_dir),
                        *roots,
                    ],
                    network="bridge",
                ),
                cwd=evaluation_dir,
                timeout=2_700,
            )
        freeze = _run_checked(
            docker_case_argv(
                self.profile,
                evaluation_dir,
                evaluation_dir,
                [
                    "uv",
                    "pip",
                    "freeze",
                    "--python",
                    f"{container_environment}/bin/python",
                ],
                network="none",
            ),
            cwd=evaluation_dir,
            timeout=300,
        )
        resolved_lock = validate_resolved_dependencies(
            freeze.stdout,
            requirements,
            self.profile.python_bootstrap,
        )
        _atomic_json(
            ready_path,
            {
                "contract": expected,
                "resolvedLock": resolved_lock,
            },
        )

    def _run_setup(
        self,
        case: dict[str, Any],
        workspace: Path,
        environment: Path,
        evaluation_dir: Path,
        *,
        deadline: float | None = None,
        cache_writes: bool = True,
    ) -> None:
        for raw_argv in case["setupArgv"]:
            if deadline is not None and time.monotonic() >= deadline:
                raise EvaluationError("Arm deadline expired during setup.")
            if is_dependency_or_project_install(raw_argv):
                # Dependencies are frozen in the shared, digest-addressed
                # environment. Project imports resolve from the disposable
                # workspace and never mutate that environment.
                continue
            container_environment = Path(
                container_path(environment, evaluation_dir)
            )
            argv = rewrite_benchmark_argv(
                raw_argv,
                container_environment,
                for_setup=True,
                require_environment=False,
            )
            argv = normalize_setup_parallelism(argv)
            _run_checked(
                docker_case_argv(
                    self.profile,
                    evaluation_dir,
                    workspace,
                    argv,
                    network="none",
                    extra_env=(
                        ()
                        if cache_writes
                        else ("CCACHE_DISABLE=true",)
                    ),
                ),
                cwd=evaluation_dir,
                timeout=(
                    1_800
                    if deadline is None
                    else max(
                        1,
                        min(1_800, math.floor(deadline - time.monotonic())),
                    )
                ),
            )

    def _uv_environment(self, root: Path) -> dict[str, str]:
        # Retained for compatibility with callers that only need a sanitized
        # host environment. Benchmark dependencies never execute on the host.
        return {
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "UV_CACHE_DIR": str(root / "uv-cache"),
            "UV_PYTHON_INSTALL_DIR": str(root / "uv-python"),
        }

    def run_frontier_alone(
        self,
        case: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        arm_root = (
            Path(runtime["baseSnapshot"]).parent
            / "arms"
            / "frontier-alone"
        )
        repository = fresh_arm_repository(
            Path(runtime["baseSnapshot"]),
            arm_root / "repo",
        )
        pair_contract = ensure_pair_contract(
            case,
            runtime,
            maximum_characters=(
                self.profile.selection.max_context_characters
            ),
        )
        approved_write_roots = list(
            pair_contract["approvedWriteRoots"]
        )
        if evaluation_write_roots(repository) != approved_write_roots:
            raise EvaluationError(
                "Frontier arm repository differs from the frozen pair contract."
            )
        base_sha = _git_text(repository, ["rev-parse", "HEAD"])
        task_packet = pair_contract["taskPacket"]
        evidence_root = arm_root / "evidence"
        evidence_root.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        is_hermes = self.profile.frontier.adapter == "hermes-completion"
        if is_hermes:
            usage_file = evidence_root / "usage.json"
            response_file = evidence_root / "response.txt"
            prompt_file = evidence_root / "prompt.txt"
            prompt = frontier_alone_response_prompt(task_packet)
            prompt_file.write_text(prompt, encoding="utf-8")
            command = frontier_command(
                self.profile,
                cwd=repository,
                sandbox="",
                output_last_message=response_file,
                usage_file=usage_file,
                prompt_file=prompt_file,
                request_timeout_seconds=(
                    self.profile.frontier.arm_timeout_seconds
                ),
            )
            frontier_result = run_command(
                command,
                cwd=repository,
                timeout=self.profile.frontier.arm_timeout_seconds,
                env=frontier_environment(),
            )
            (evidence_root / "stdout.log").write_text(
                frontier_result.stdout,
                encoding="utf-8",
            )
            (evidence_root / "stderr.log").write_text(
                frontier_result.stderr,
                encoding="utf-8",
            )
            raw_usage_text = ""
            if usage_file.is_file():
                raw_usage_text = usage_file.read_text(encoding="utf-8")
            (evidence_root / "usage-raw.json").write_text(
                raw_usage_text,
                encoding="utf-8",
            )
            phase_usage = parse_hermes_usage_json(
                raw_usage_text,
                expected_provider=self.profile.frontier.provider,
                expected_model=self.profile.frontier.model,
            )
            usage = usage_with_phases([
                ("frontier-alone", phase_usage)
            ])
            response_text = frontier_result.stdout[
                :_MAX_FRONTIER_RESPONSE_BYTES
            ]
            response_file.write_text(response_text, encoding="utf-8")
            diff = ""
            materialize_error: str | None = None
            if (
                not frontier_result.timed_out
                and frontier_result.returncode == 0
                and response_text.strip()
                and phase_usage["usageStatus"] == "reported"
            ):
                try:
                    diff = materialize_frontier_edit_manifest(
                        response_text,
                        repository=repository,
                        approved_write_roots=approved_write_roots,
                    )
                except EvaluationError as exc:
                    materialize_error = str(exc)
            else:
                materialize_error = (
                    "Frontier timed out"
                    if frontier_result.timed_out
                    else "Frontier returned no response."
                )
        else:
            last_message = evidence_root / "last-message.txt"
            command = codex_command(
                self.profile,
                cwd=repository,
                sandbox="workspace-write",
                output_last_message=last_message,
            )
            codex_result = run_command(
                command,
                cwd=repository,
                timeout=self.profile.frontier.arm_timeout_seconds,
                env=frontier_environment(),
                input_text=frontier_alone_prompt(task_packet),
            )
            (evidence_root / "events.jsonl").write_text(
                codex_result.stdout,
                encoding="utf-8",
            )
            (evidence_root / "stderr.log").write_text(
                codex_result.stderr,
                encoding="utf-8",
            )
            phase_usage = parse_codex_usage_jsonl(codex_result.stdout)
            usage = usage_with_phases([
                ("frontier-alone", phase_usage)
            ])
            diff = _git_diff(repository, base_sha)
            materialize_error = None
            frontier_result = codex_result
        patch = persist_candidate_patch(evidence_root, diff)
        structural_error = validate_candidate_diff(
            diff,
            case,
            repository,
            allowed_paths=approved_write_roots,
        )
        if materialize_error is not None:
            structural_error = materialize_error
        remaining = self.profile.frontier.arm_timeout_seconds - (
            time.perf_counter() - started
        )
        if structural_error is None and diff and remaining > 0:
            oracle = self._score_candidate(
                case,
                runtime,
                diff,
                arm_root,
                timeout_seconds=remaining,
            )
        else:
            oracle = {
                "passed": False,
                "exitCode": None,
                "evidence": (
                    "Arm deadline expired before independent oracle."
                    if remaining <= 0
                    else structural_error or "No candidate patch produced."
                ),
                "timedOut": remaining <= 0,
            }
        elapsed = time.perf_counter() - started
        deadline_expired = bool(
            elapsed >= self.profile.frontier.arm_timeout_seconds
            or oracle.get("timedOut")
        )
        infrastructure_error = oracle_infrastructure_failure(oracle)
        usage_invalid = usage.get("usageStatus") != "reported"
        completed = bool(
            diff
            and structural_error is None
            and not frontier_result.timed_out
            and frontier_result.returncode == 0
            and not deadline_expired
            and infrastructure_error is None
            and not usage_invalid
        )
        status = (
            "invalid"
            if infrastructure_error is not None or usage_invalid
            else "timed_out"
            if frontier_result.timed_out or deadline_expired
            else "completed"
            if completed
            else "failed"
        )
        return make_arm_result(
            case=case,
            arm="frontier-alone",
            status=status,
            completed=completed,
            score=1 if oracle["passed"] else 0,
            elapsed_seconds=elapsed,
            phase_seconds={
                "frontier": frontier_result.elapsed_seconds,
                "oracle": oracle.get("elapsedSeconds", 0.0),
            },
            frontier_usage=usage,
            local_usage=empty_local_usage(),
            repairs=0,
            model_loads=0,
            review_verdict=None,
            patch=patch,
            oracle=oracle,
        )

    def run_mlx_swarm(
        self,
        case: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        arm_root = (
            Path(runtime["baseSnapshot"]).parent
            / "arms"
            / "mlx-swarm"
        )
        repository = fresh_arm_repository(
            Path(runtime["baseSnapshot"]),
            arm_root / "repo",
        )
        pair_contract = ensure_pair_contract(
            case,
            runtime,
            maximum_characters=(
                self.profile.selection.max_context_characters
            ),
        )
        approved_write_roots = list(
            pair_contract["approvedWriteRoots"]
        )
        if evaluation_write_roots(repository) != approved_write_roots:
            raise EvaluationError(
                "MLX Swarm arm repository differs from the frozen pair contract."
            )
        base_sha = _git_text(repository, ["rev-parse", "HEAD"])
        config_path = repository / ".mlx-swarm-eval.json"
        artifacts_root = arm_root / "artifacts"
        write_evaluation_config(
            self.config,
            config_path,
            artifacts_root,
            Path(runtime["verifierManifest"]),
            repository,
            write_roots=approved_write_roots,
        )
        eval_config = load_config(config_path)
        store = CommanderStore(eval_config)
        task_packet = pair_contract["taskPacket"]
        roots_text = ", ".join(approved_write_roots)
        request = store.create_request(
            case["objective"],
            [
                "Modify production code only; never modify tests or benchmark evidence.",
                "Use the bugsinpy-acceptance verification profile for every mutating artifact.",
                "Return a schema-v2 typed workspace plan.",
                (
                    "For paired-arm symmetry, every mutating task must set "
                    f"allowedPaths to exactly these roots: {roots_text}."
                ),
                (
                    "Any context copied from a workspace file must be one "
                    "exact contiguous excerpt. Put its repository-relative "
                    "path in the source label; never summarize, rewrite, or "
                    "silently omit lines inside an excerpt."
                ),
                (
                    "Every mutating task must use workerOutputProtocol "
                    "edit-manifest-v1 with a JSON gate whose required and "
                    "allowed top-level keys are exactly edits. Ask the worker "
                    "for the smallest exact old/new anchors, not a Git diff."
                ),
                (
                    "Every mutating task must explicitly set deterministic "
                    "generationOverride temperature 0, top_p 1, "
                    "enable_thinking false, and max_tokens no greater than 800."
                ),
                *split_constraint_text(task_packet),
            ],
            request_id=f"eval-{case['caseId']}",
        )
        claim = store.claim_plan(
            request["request"]["requestId"],
            adapter=self.profile.frontier.adapter,
        )
        evidence_root = arm_root / "evidence"
        evidence_root.mkdir(parents=True, exist_ok=True)
        plan_response = evidence_root / "plan-response.json"
        plan_blueprint_response = evidence_root / "plan-blueprint.raw.json"
        started = time.perf_counter()
        deadline = started + self.profile.frontier.arm_timeout_seconds
        is_hermes = self.profile.frontier.adapter == "hermes-completion"
        plan_prompt_text = Path(claim["promptPath"]).read_text(
            encoding="utf-8"
        )
        if is_hermes:
            plan_prompt_text = frontier_delegation_blueprint_prompt(
                task_packet,
                worker_capabilities=worker_capabilities_payload(
                    eval_config.worker.capabilities
                ),
            )
            Path(claim["promptPath"]).write_text(
                plan_prompt_text,
                encoding="utf-8",
            )
            (evidence_root / "plan-prompt.txt").write_text(
                plan_prompt_text,
                encoding="utf-8",
            )
            plan_usage_file = evidence_root / "plan-usage.json"
            plan_command = frontier_command(
                self.profile,
                cwd=repository,
                sandbox="",
                output_last_message=plan_blueprint_response,
                usage_file=plan_usage_file,
                prompt_file=Path(claim["promptPath"]),
                request_timeout_seconds=(
                    self.profile.frontier.planning_timeout_seconds
                ),
            )
            plan_frontier_result = run_command(
                plan_command,
                cwd=repository,
                timeout=self.profile.frontier.planning_timeout_seconds,
                env=frontier_environment(),
            )
            (evidence_root / "plan-stdout.log").write_text(
                plan_frontier_result.stdout,
                encoding="utf-8",
            )
            (evidence_root / "plan-stderr.log").write_text(
                plan_frontier_result.stderr,
                encoding="utf-8",
            )
            raw_plan_usage = ""
            if plan_usage_file.is_file():
                raw_plan_usage = plan_usage_file.read_text(encoding="utf-8")
            (evidence_root / "plan-usage-raw.json").write_text(
                raw_plan_usage,
                encoding="utf-8",
            )
            plan_usage = parse_hermes_usage_json(
                raw_plan_usage,
                expected_provider=self.profile.frontier.provider,
                expected_model=self.profile.frontier.model,
            )
            if (
                not plan_frontier_result.timed_out
                and plan_frontier_result.returncode == 0
                and plan_frontier_result.stdout.strip()
            ):
                plan_blueprint_response.write_text(
                    strip_one_json_fence(plan_frontier_result.stdout),
                    encoding="utf-8",
                )
            plan_timed_out = plan_frontier_result.timed_out
            plan_returncode = plan_frontier_result.returncode
            plan_elapsed = plan_frontier_result.elapsed_seconds
        else:
            plan_frontier_result = run_command(
                codex_command(
                    self.profile,
                    cwd=repository,
                    sandbox="read-only",
                    output_last_message=plan_response,
                ),
                cwd=repository,
                timeout=self.profile.frontier.planning_timeout_seconds,
                env=frontier_environment(),
                input_text=plan_prompt_text,
            )
            (evidence_root / "plan-events.jsonl").write_text(
                plan_frontier_result.stdout,
                encoding="utf-8",
            )
            plan_usage = parse_codex_usage_jsonl(
                plan_frontier_result.stdout
            )
            plan_timed_out = plan_frontier_result.timed_out
            plan_returncode = plan_frontier_result.returncode
            plan_elapsed = plan_frontier_result.elapsed_seconds
        if is_hermes:
            plan_usage_valid = plan_usage["usageStatus"] == "reported"
        else:
            plan_usage_valid = True
        if (
            plan_timed_out
            or plan_returncode != 0
            or not (
                plan_blueprint_response.is_file()
                if is_hermes
                else plan_response.is_file()
            )
            or not plan_usage_valid
        ):
            return make_arm_result(
                case=case,
                arm="mlx-swarm",
                status=(
                    "timed_out"
                    if plan_timed_out
                    else "invalid"
                    if not plan_usage_valid
                    else "failed"
                ),
                completed=False,
                score=0,
                elapsed_seconds=time.perf_counter() - started,
                phase_seconds={"planning": plan_elapsed},
                frontier_usage=usage_with_phases([("planning", plan_usage)]),
                local_usage=empty_local_usage(),
                repairs=0,
                model_loads=0,
                review_verdict=None,
                patch={"sha256": None, "changedFiles": 0},
                oracle={
                    "passed": False,
                    "exitCode": None,
                    "evidence": (
                        "Frontier planning usage is invalid."
                        if not plan_usage_valid
                        else "Frontier planning did not produce a valid response."
                    ),
                },
            )
        if is_hermes:
            try:
                raw_blueprint = plan_blueprint_response.read_text(
                    encoding="utf-8"
                )
                blueprint = parse_frontier_delegation_blueprint(
                    raw_blueprint,
                    objective=case["objective"],
                    task_packet=task_packet,
                    repository=repository,
                    approved_write_roots=approved_write_roots,
                    maximum_manifest_characters=(
                        eval_config.worker.capabilities.max_generation_tokens
                        * 4
                    ),
                )
                materialized_plan = materialize_frontier_delegation_plan(
                    blueprint,
                    task_packet=task_packet,
                    repository=repository,
                    approved_write_roots=approved_write_roots,
                    max_repair=self.profile.max_repair,
                    max_generation_tokens=(
                        eval_config.worker.capabilities.max_generation_tokens
                    ),
                )
                plan_response.write_text(
                    json.dumps(
                        materialized_plan,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (evidence_root / "plan-materialization.json").write_text(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "blueprintSha256": hashlib.sha256(
                                raw_blueprint.encode("utf-8")
                            ).hexdigest(),
                            "materializedPlanSha256": canonical_json_sha256(
                                materialized_plan
                            ),
                            "workerDelegation": (
                                eval_config.worker.capabilities.delegation_level
                            ),
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            except (EvaluationError, OSError, ValueError) as exc:
                (evidence_root / "plan-materialization.error.txt").write_text(
                    str(exc) + "\n",
                    encoding="utf-8",
                )
                return make_arm_result(
                    case=case,
                    arm="mlx-swarm",
                    status="failed",
                    completed=False,
                    score=0,
                    elapsed_seconds=time.perf_counter() - started,
                    phase_seconds={"planning": plan_elapsed},
                    frontier_usage=usage_with_phases([
                        ("planning", plan_usage),
                    ]),
                    local_usage=empty_local_usage(),
                    repairs=0,
                    model_loads=0,
                    review_verdict=None,
                    patch={"sha256": None, "changedFiles": 0},
                    oracle={
                        "passed": False,
                        "exitCode": None,
                        "evidence": (
                            "Frontier delegation blueprint was rejected: "
                            f"{exc}"
                        ),
                    },
                )
        try:
            imported = store.import_plan(
                request["request"]["requestId"],
                plan_response,
                claim_id=claim["claimId"],
                adapter=self.profile.frontier.adapter,
                provider=self.profile.frontier.provider,
                model=self.profile.frontier.model,
                prompt_tokens=plan_usage.get("promptTokens"),
                completion_tokens=plan_usage.get("completionTokens"),
                total_tokens=plan_usage.get("totalTokens"),
            )
            candidate_plan = load_plan(
                Path(imported["plan"]["source"]),
                eval_config,
            )
            validate_evaluation_plan(
                candidate_plan,
                repository,
                approved_write_roots,
            )
        except (CommanderError, EvaluationError) as exc:
            return make_arm_result(
                case=case,
                arm="mlx-swarm",
                status="failed",
                completed=False,
                score=0,
                elapsed_seconds=time.perf_counter() - started,
                phase_seconds={"planning": plan_elapsed},
                frontier_usage=usage_with_phases([
                    ("planning", plan_usage),
                ]),
                local_usage=empty_local_usage(),
                repairs=0,
                model_loads=0,
                review_verdict=None,
                patch={"sha256": None, "changedFiles": 0},
                oracle={
                    "passed": False,
                    "exitCode": None,
                    "evidence": f"Frontier plan was rejected: {exc}",
                },
            )
        plan_digest = imported["plan"]["digest"]
        execution = imported["executionPreview"]
        if not isinstance(execution, dict):
            raise EvaluationError("Frontier plan has no execution preview.")
        plan, plan_path, approval, receipt, request_state = store.approved_plan(
            request["request"]["requestId"],
            plan_digest,
            source="evaluation-harness",
            execution_digest=execution["executionDigest"],
        )
        run_id = _run_id()
        session_dir = artifacts_root / plan.plan_id / run_id
        snapshot = prepare_worktree(
            eval_config,
            plan,
            session_id=run_id,
            expected_execution_digest=execution["executionDigest"],
        )
        session = Session(
            session_dir,
            plan,
            session_id=run_id,
            launch_source="evaluation",
        )
        session.set_sources(
            config_source=config_path,
            plan_source=plan_path,
        )
        session.state["maxRepair"] = self.profile.max_repair
        session._save()
        session.attach_workspace(
            snapshot,
            execution_approval={
                "schemaVersion": 1,
                "planSha256": plan_digest,
                "executionDigest": execution["executionDigest"],
                "workspaceRoot": execution["workspaceRoot"],
                "baseSha": execution["baseSha"],
                "approvedAt": utc_now(),
                "source": "evaluation-harness",
            },
        )
        session.attach_commander(
            request_id=request["request"]["requestId"],
            approval=approval.to_json(),
            planning_receipt=receipt,
            revision_of=request_state.get("revisionOf"),
        )
        store.mark_launched(
            request["request"]["requestId"],
            approval,
            plan_id=plan.plan_id,
            session_id=run_id,
        )
        local_started = time.perf_counter()
        local_timeout = min(
            self.profile.frontier.local_timeout_seconds,
            max(1, math.floor(deadline - time.perf_counter())),
        )
        local_result = run_swarm_with_synthetic_operator(
            eval_config,
            plan_path,
            session_dir,
            self.profile.max_repair,
            timeout=local_timeout,
        )
        local_seconds = time.perf_counter() - local_started
        session = Session.load(session_dir, eval_config)
        local_usage = session.local_usage()
        repairs = sum(
            int(task.get("repairAttempts", 0))
            for task in session.state.get("tasks", {}).values()
        )
        workspace = load_workspace_snapshot(session_dir)
        diff, _ = final_workspace_diff(workspace)
        candidate_diff = diff or retained_session_candidate_diff(session)
        patch = persist_candidate_patch(evidence_root, candidate_diff)
        structural_error = validate_candidate_diff(
            candidate_diff,
            case,
            repository,
            allowed_paths=approved_write_roots,
        )
        if local_result.infrastructure_error is not None:
            return make_arm_result(
                case=case,
                arm="mlx-swarm",
                status="invalid",
                completed=False,
                score=0,
                elapsed_seconds=time.perf_counter() - started,
                phase_seconds={
                    "planning": plan_elapsed,
                    "local": local_seconds,
                    "review": 0.0,
                    "oracle": 0.0,
                },
                frontier_usage=usage_with_phases([
                    ("planning", plan_usage),
                ]),
                local_usage=local_usage,
                repairs=repairs,
                model_loads=int(local_usage.get("modelLoads", 0)),
                review_verdict=None,
                patch=patch,
                oracle={
                    "passed": False,
                    "exitCode": None,
                    "evidence": (
                        "Verifier infrastructure failed: "
                        + local_result.infrastructure_error
                    ),
                },
            )
        review_usage = None
        review_verdict = None
        review_seconds = 0.0
        review_timed_out = False
        if (
            session.state.get("status") == "completed"
            and time.perf_counter() < deadline
        ):
            review_claim = store.claim_review(
                session_dir,
                adapter=self.profile.frontier.adapter,
            )
            review_prompt_text = Path(
                review_claim["promptPath"]
            ).read_text(encoding="utf-8")
            if is_hermes:
                review_usage_file = evidence_root / "review-usage.json"
                review_response = evidence_root / "review-response.json"
                review_command = frontier_command(
                    self.profile,
                    cwd=repository,
                    sandbox="",
                    output_last_message=review_response,
                    usage_file=review_usage_file,
                    prompt_file=Path(review_claim["promptPath"]),
                    request_timeout_seconds=min(
                        self.profile.frontier.review_timeout_seconds,
                        max(1, math.floor(deadline - time.perf_counter())),
                    ),
                )
                review_result = run_command(
                    review_command,
                    cwd=repository,
                    timeout=min(
                        self.profile.frontier.review_timeout_seconds,
                        max(1, math.floor(deadline - time.perf_counter())),
                    ),
                    env=frontier_environment(),
                )
                review_seconds = review_result.elapsed_seconds
                review_timed_out = review_result.timed_out
                (evidence_root / "review-stdout.log").write_text(
                    review_result.stdout,
                    encoding="utf-8",
                )
                (evidence_root / "review-stderr.log").write_text(
                    review_result.stderr,
                    encoding="utf-8",
                )
                raw_review_usage = ""
                if review_usage_file.is_file():
                    raw_review_usage = review_usage_file.read_text(
                        encoding="utf-8"
                    )
                (evidence_root / "review-usage-raw.json").write_text(
                    raw_review_usage,
                    encoding="utf-8",
                )
                review_usage = parse_hermes_usage_json(
                    raw_review_usage,
                    expected_provider=self.profile.frontier.provider,
                    expected_model=self.profile.frontier.model,
                )
                if (
                    not review_result.timed_out
                    and review_result.returncode == 0
                    and review_usage["usageStatus"] == "reported"
                    and review_result.stdout.strip()
                ):
                    review_response.write_text(
                        strip_one_json_fence(review_result.stdout),
                        encoding="utf-8",
                    )
                    imported_review = store.import_review(
                        session_dir,
                        review_response,
                        claim_id=review_claim["claimId"],
                        adapter=self.profile.frontier.adapter,
                        provider=self.profile.frontier.provider,
                        model=self.profile.frontier.model,
                        prompt_tokens=review_usage.get("promptTokens"),
                        completion_tokens=review_usage.get(
                            "completionTokens"
                        ),
                        total_tokens=review_usage.get("totalTokens"),
                    )
                    review_verdict = imported_review["review"]["verdict"]
            else:
                review_response = evidence_root / "review-response.json"
                review_result = run_command(
                    codex_command(
                        self.profile,
                        cwd=repository,
                        sandbox="read-only",
                        output_last_message=review_response,
                    ),
                    cwd=repository,
                    timeout=min(
                        self.profile.frontier.review_timeout_seconds,
                        max(1, math.floor(deadline - time.perf_counter())),
                    ),
                    env=frontier_environment(),
                    input_text=review_prompt_text,
                )
                review_seconds = review_result.elapsed_seconds
                review_timed_out = review_result.timed_out
                (evidence_root / "review-events.jsonl").write_text(
                    review_result.stdout,
                    encoding="utf-8",
                )
                review_usage = parse_codex_usage_jsonl(review_result.stdout)
                if (
                    not review_result.timed_out
                    and review_result.returncode == 0
                    and review_response.is_file()
                ):
                    imported_review = store.import_review(
                        session_dir,
                        review_response,
                        claim_id=review_claim["claimId"],
                        adapter="codex-cli-evaluation",
                        provider="openai-codex",
                        model=self.profile.frontier.model,
                        prompt_tokens=review_usage.get("promptTokens"),
                        completion_tokens=review_usage.get(
                            "completionTokens"
                        ),
                        total_tokens=review_usage.get("totalTokens"),
                    )
                    review_verdict = imported_review["review"]["verdict"]
        remaining = deadline - time.perf_counter()
        if structural_error is None and candidate_diff and remaining > 0:
            oracle = self._score_candidate(
                case,
                runtime,
                candidate_diff,
                arm_root,
                timeout_seconds=remaining,
            )
        else:
            oracle = {
                "passed": False,
                "exitCode": None,
                "evidence": (
                    "Arm deadline expired before independent oracle."
                    if remaining <= 0
                    else structural_error or "No candidate patch produced."
                ),
                "timedOut": remaining <= 0,
            }
        phases = [("planning", plan_usage)]
        if review_usage is not None:
            phases.append(("review", review_usage))
        usage = usage_with_phases(phases)
        usage_invalid = usage.get("usageStatus") != "reported"
        completed = bool(
            session.state.get("status") == "completed"
            and review_verdict is not None
            and not local_result.timed_out
            and not oracle.get("timedOut")
            and not usage_invalid
        )
        infrastructure_error = oracle_infrastructure_failure(oracle)
        if infrastructure_error is not None:
            completed = False
        deadline_expired = (
            time.perf_counter() >= deadline or bool(oracle.get("timedOut"))
        )
        status = (
            "invalid"
            if infrastructure_error is not None or usage_invalid
            else "timed_out"
            if local_result.timed_out or review_timed_out or deadline_expired
            else "completed"
            if completed
            else "failed"
        )
        elapsed = time.perf_counter() - started
        return make_arm_result(
            case=case,
            arm="mlx-swarm",
            status=status,
            completed=completed,
            score=1 if oracle["passed"] else 0,
            elapsed_seconds=elapsed,
            phase_seconds={
                "planning": plan_elapsed,
                "local": local_seconds,
                "review": review_seconds,
                "oracle": oracle.get("elapsedSeconds", 0.0),
            },
            frontier_usage=usage,
            local_usage=local_usage,
            repairs=repairs,
            model_loads=int(local_usage.get("modelLoads", 0)),
            review_verdict=review_verdict,
            patch=patch,
            oracle=oracle,
        )

    def _score_candidate(
        self,
        case: dict[str, Any],
        runtime: dict[str, Any],
        diff: str,
        arm_root: Path,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        oracle_root = arm_root / "oracle"
        if oracle_root.exists():
            shutil.rmtree(oracle_root)
        repository = fresh_arm_repository(
            Path(runtime["baseSnapshot"]),
            oracle_root,
        )
        apply_result = run_command(
            ["git", "apply", "--index", "-"],
            cwd=repository,
            timeout=max(1, min(60, math.floor(deadline - time.monotonic()))),
            input_text=diff,
        )
        if apply_result.returncode != 0:
            raise EvaluationError(
                "Independent oracle could not apply candidate patch: "
                + (apply_result.stderr or apply_result.stdout)
            )
        try:
            return run_case_verifier(
                Path(runtime["verifierManifest"]),
                repository,
                timeout_seconds=max(0.0, deadline - time.monotonic()),
            )
        except EvaluationError as exc:
            return {
                "passed": False,
                "exitCode": None,
                "evidence": f"INFRASTRUCTURE_ERROR: {exc}",
                "elapsedSeconds": 0.0,
                "timedOut": False,
            }


def run_local_replay_calibration(
    config: SwarmConfig,
    store: EvaluationStore,
    evaluation_id: str,
    *,
    worker_mode: str,
    reasoning_max_tokens: int,
    adapted_plan_dir: Path | None = None,
) -> dict[str, Any]:
    """Replay pilot plans locally without invoking a frontier adapter.

    Frozen-prompt replays remain the promotion authority. Capability-adapted
    plans are explicitly diagnostic and never mutate the measured-work gate.
    """
    if worker_mode not in {"direct", "reasoning-edit"}:
        raise EvaluationError("Unsupported local replay worker mode.")
    if not 64 <= reasoning_max_tokens <= 8192:
        raise EvaluationError(
            "Local replay reasoningMaxTokens must be between 64 and 8192."
        )
    detail = store.detail(evaluation_id)
    state = detail["evaluation"]
    pilot_status = state.get("pilotStatus")
    if not replayable_pilot_status(pilot_status):
        raise EvaluationError(
            "Local replay requires a sealed frozen calibration phase."
        )
    evaluation_dir = store._dir(evaluation_id)
    diagnostic_only = adapted_plan_dir is not None
    if adapted_plan_dir is not None:
        approved_root = config.source.parent.resolve()
        adapted_plan_dir = adapted_plan_dir.resolve()
        try:
            adapted_plan_dir.relative_to(approved_root)
        except ValueError as exc:
            raise EvaluationError(
                "Adapted plan directory must stay below the config directory."
            ) from exc
        if not adapted_plan_dir.is_dir():
            raise EvaluationError("Adapted plan directory does not exist.")
    profile = load_evaluation_profile(
        evaluation_dir / "profile.snapshot.json"
    )
    suite = validate_suite(detail["suite"], profile)
    pilot_cases = [
        case for case in suite["cases"] if case["phase"] == "pilot"
    ]
    if not pilot_cases:
        raise EvaluationError("Frozen evaluation has no calibration cases.")
    source_revision = mlx_swarm_source_revision()
    if source_revision["dirty"]:
        raise EvaluationError(
            "Local replay requires a clean MLX Swarm source revision."
        )

    replay_id = _run_id().lower()
    replay_root = evaluation_dir / "local-replays" / replay_id
    replay_root.mkdir(parents=True, exist_ok=False)
    replay_config_source = replace(
        config,
        worker=WorkerConfig(
            mode=worker_mode,
            reasoning_max_tokens=reasoning_max_tokens,
            capabilities=config.worker.capabilities,
        ),
    )
    runner = EvaluationRunner(replay_config_source, store, profile)
    results: list[dict[str, Any]] = []

    for case in pilot_cases:
        case_id = case["caseId"]
        case_root = evaluation_dir / "cases" / case_id
        runtime = _read_json(case_root / "runtime.json")
        source_plan = (
            adapted_plan_dir / f"{case_id}.json"
            if adapted_plan_dir is not None
            else (
                case_root
                / "arms"
                / "mlx-swarm"
                / "artifacts"
                / "_commander"
                / "requests"
                / f"eval-{case_id}"
                / "plan.validated.json"
            )
        )
        if not source_plan.is_file():
            case_replay_root = replay_root / case_id
            case_replay_root.mkdir(parents=True, exist_ok=False)
            result = {
                "schemaVersion": 1,
                "replayId": replay_id,
                "evaluationId": evaluation_id,
                "caseId": case_id,
                "status": "failed",
                "score": 0,
                "workerMode": worker_mode,
                "reasoningMaxTokens": reasoning_max_tokens,
                "frontierCalls": 0,
                "planSourceType": (
                    "capability-adapted"
                    if diagnostic_only
                    else "frozen-frontier"
                ),
                "diagnosticOnly": diagnostic_only,
                "sourcePlan": str(source_plan),
                "sourcePlanSha256": None,
                "sourcePromptEvidence": [],
                "sessionDir": None,
                "elapsedSeconds": 0.0,
                "localUsage": empty_local_usage(),
                "taskEvidence": {},
                "oracle": {
                    "passed": False,
                    "exitCode": None,
                    "evidence": (
                        f"{'Adapted' if diagnostic_only else 'Frozen commander'} "
                        f"plan is missing for {case_id}."
                    ),
                },
                "recordedAt": utc_now(),
            }
            _exclusive_json(case_replay_root / "result.json", result)
            results.append(result)
            continue
        case_replay_root = replay_root / case_id
        repository = fresh_arm_repository(
            Path(runtime["baseSnapshot"]),
            case_replay_root / "repo",
        )
        approved_write_roots = evaluation_write_roots(repository)
        config_path = repository / ".mlx-swarm-local-replay.json"
        artifacts_root = case_replay_root / "artifacts"
        write_evaluation_config(
            replay_config_source,
            config_path,
            artifacts_root,
            Path(runtime["verifierManifest"]),
            repository,
            write_roots=approved_write_roots,
        )
        replay_config = load_config(config_path)
        plan = load_plan(source_plan, replay_config)
        validate_evaluation_plan(
            plan,
            repository,
            approved_write_roots,
        )
        _atomic_json(case_replay_root / "plan.snapshot.json", plan.raw)

        source_prompts = (
            []
            if diagnostic_only
            else frozen_prompt_evidence(case_root)
        )
        if not diagnostic_only:
            required_prompt_tasks = sorted(task.id for task in plan.tasks)
            if sorted(item["taskId"] for item in source_prompts) != (
                required_prompt_tasks
            ):
                raise EvaluationError(
                    "Frozen initial prompt evidence is incomplete for "
                    f"{case_id}."
                )
        preview = execution_preview(replay_config, plan)
        run_id = _run_id()
        session_dir = artifacts_root / plan.plan_id / run_id
        snapshot = prepare_worktree(
            replay_config,
            plan,
            session_id=run_id,
            expected_execution_digest=preview["executionDigest"],
        )
        session = Session(
            session_dir,
            plan,
            session_id=run_id,
            launch_source="evaluation-local-replay",
        )
        session.set_sources(
            config_source=config_path,
            plan_source=source_plan,
        )
        session.state["maxRepair"] = profile.max_repair
        session.state["localReplay"] = {
            "replayId": replay_id,
            "sourceEvaluationId": evaluation_id,
            "sourcePlan": str(source_plan),
            "planSourceType": (
                "capability-adapted"
                if diagnostic_only
                else "frozen-frontier"
            ),
            "diagnosticOnly": diagnostic_only,
            "sourcePromptEvidence": source_prompts,
            "frontierCalls": 0,
        }
        if not diagnostic_only:
            install_frozen_prompt_replay(session, source_prompts)
        session.attach_workspace(
            snapshot,
            execution_approval={
                "schemaVersion": 1,
                "planSha256": canonical_json_sha256(plan.raw),
                "executionDigest": preview["executionDigest"],
                "workspaceRoot": preview["workspaceRoot"],
                "baseSha": preview["baseSha"],
                "approvedAt": utc_now(),
                "source": "evaluation-local-replay",
            },
        )

        started = time.perf_counter()
        local_result = run_swarm_with_synthetic_operator(
            replay_config,
            source_plan,
            session_dir,
            profile.max_repair,
            timeout=profile.frontier.local_timeout_seconds,
        )
        elapsed = time.perf_counter() - started
        session = Session.load(session_dir, replay_config)
        usage = session.local_usage()
        workspace = load_workspace_snapshot(session_dir)
        diff, _ = final_workspace_diff(workspace)
        candidate_diff = diff or retained_session_candidate_diff(session)
        structural_error = validate_candidate_diff(
            candidate_diff,
            case,
            repository,
            allowed_paths=approved_write_roots,
        )
        if local_result.infrastructure_error is not None:
            status = "invalid"
            oracle = {
                "passed": False,
                "exitCode": None,
                "evidence": local_result.infrastructure_error,
            }
        elif structural_error is None and candidate_diff:
            oracle = runner._score_candidate(
                case,
                runtime,
                candidate_diff,
                case_replay_root,
                timeout_seconds=max(
                    1,
                    profile.frontier.local_timeout_seconds - elapsed,
                ),
            )
            infrastructure = oracle_infrastructure_failure(oracle)
            status = (
                "invalid"
                if infrastructure is not None
                else "completed"
                if oracle["passed"]
                else "failed"
            )
            if infrastructure is not None:
                oracle["evidence"] = infrastructure
        else:
            status = "failed"
            oracle = {
                "passed": False,
                "exitCode": None,
                "evidence": (
                    replay_failure_evidence(session)
                    or structural_error
                    or "No candidate patch produced."
                ),
            }
        result = {
            "schemaVersion": 1,
            "replayId": replay_id,
            "evaluationId": evaluation_id,
            "caseId": case_id,
            "status": status,
            "score": 1 if oracle["passed"] else 0,
            "workerMode": worker_mode,
            "reasoningMaxTokens": reasoning_max_tokens,
            "frontierCalls": 0,
            "planSourceType": (
                "capability-adapted"
                if diagnostic_only
                else "frozen-frontier"
            ),
            "diagnosticOnly": diagnostic_only,
            "sourcePlan": str(source_plan),
            "sourcePlanSha256": canonical_json_sha256(plan.raw),
            "sourcePromptEvidence": source_prompts,
            "sessionDir": str(session_dir),
            "elapsedSeconds": elapsed,
            "localUsage": usage,
            "taskEvidence": {
                task_id: {
                    "status": task_state.get("status"),
                    "error": task_state.get("error"),
                    "gateResult": task_state.get("gateResult"),
                    "artifact": task_state.get("artifact"),
                    "verificationResults": task_state.get(
                        "verificationResults",
                        [],
                    ),
                    "generationAttempts": task_state.get(
                        "generationAttempts",
                        [],
                    ),
                    "reasoningAttempts": task_state.get(
                        "reasoningAttempts",
                        [],
                    ),
                }
                for task_id, task_state in session.state.get(
                    "tasks",
                    {},
                ).items()
            },
            "oracle": {
                "passed": bool(oracle["passed"]),
                "exitCode": oracle.get("exitCode"),
                "evidence": str(oracle.get("evidence", ""))[:MAX_LOG_BYTES],
            },
            "recordedAt": utc_now(),
        }
        _exclusive_json(case_replay_root / "result.json", result)
        results.append(result)

    required_cases = sorted(case["caseId"] for case in pilot_cases)
    promotion_gate = local_replay_promotion_gate(
        required_cases,
        results,
    )
    if diagnostic_only:
        promotion_gate = capability_diagnostic_gate(promotion_gate)
    passed_cases = promotion_gate["passedCases"]
    gate_passed = promotion_gate["status"] == "passed"
    replay = {
        "schemaVersion": 1,
        "replayId": replay_id,
        "evaluationId": evaluation_id,
        "workerMode": worker_mode,
        "reasoningMaxTokens": reasoning_max_tokens,
        "frontierCalls": 0,
        "planSourceType": (
            "capability-adapted"
            if diagnostic_only
            else "frozen-frontier"
        ),
        "diagnosticOnly": diagnostic_only,
        "mlxSwarmCommit": source_revision["commit"],
        "model": {
            "repository": replay_config_source.model.repository,
            "revision": replay_config_source.model.revision,
            "localPath": replay_config_source.model.local_path,
        },
        "requiredCases": required_cases,
        "passedCases": passed_cases,
        "results": results,
        "promotionGate": promotion_gate,
        "recordedAt": utc_now(),
    }
    _exclusive_json(replay_root / "replay.json", replay)

    if not diagnostic_only:
        state_path = evaluation_dir / "evaluation.json"
        current_state = _read_json(state_path)
        update_local_replay_state(
            current_state,
            gate_passed=gate_passed,
            replay_id=replay_id,
            worker_mode=worker_mode,
            required_cases=required_cases,
            passed_cases=passed_cases,
            recorded_at=replay["recordedAt"],
        )
        current_state["updatedAt"] = utc_now()
        _atomic_json(state_path, current_state)
    return replay


def replayable_pilot_status(value: Any) -> bool:
    """Allow diagnostics after either a valid or invalid sealed pilot."""
    return value in {"completed", "invalid"}


def update_local_replay_state(
    state: dict[str, Any],
    *,
    gate_passed: bool,
    replay_id: str,
    worker_mode: str,
    required_cases: Sequence[str],
    passed_cases: Sequence[str],
    recorded_at: str,
) -> None:
    """Record replay evidence without letting an invalid pilot unlock spend."""
    pilot_status = state.get("pilotStatus")
    measured_eligible = gate_passed and pilot_status == "completed"
    state["localReplayGate"] = {
        "status": "passed" if gate_passed else "failed",
        "measuredEligible": measured_eligible,
        "pilotStatus": pilot_status,
        "replayId": replay_id,
        "workerMode": worker_mode,
        "requiredCases": list(required_cases),
        "passedCases": list(passed_cases),
        "recordedAt": recorded_at,
    }
    if pilot_status == "invalid":
        state["measuredStatus"] = "locked_invalid_pilot"
        state["status"] = (
            "pilot_invalid_local_replay_passed"
            if gate_passed
            else "pilot_invalid_local_replay_failed"
        )
        return
    state["measuredStatus"] = (
        "pending" if measured_eligible else "locked_local_replay"
    )
    state["status"] = (
        "pilot_completed"
        if measured_eligible
        else "pilot_completed_local_replay_failed"
    )


def local_replay_promotion_gate(
    required_cases: Sequence[str],
    results: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Require every distinct frozen calibration case to pass its oracle."""
    required = sorted(set(required_cases))
    passed = sorted({
        str(result.get("caseId"))
        for result in results
        if (
            result.get("status") == "completed"
            and result.get("score") == 1
            and result.get("caseId") in required
        )
    })
    gate_passed = bool(required) and passed == required
    return {
        "status": "passed" if gate_passed else "failed",
        "measuredEligible": gate_passed,
        "requiredCases": required,
        "passedCases": passed,
        "reason": (
            "Every frozen calibration replay passed the independent oracle."
            if gate_passed
            else "Measured work remains locked until every frozen "
            "calibration replay passes the independent oracle."
        ),
    }


def capability_diagnostic_gate(
    replay_gate: dict[str, Any],
) -> dict[str, Any]:
    """Prevent capability-adapted evidence from unlocking measured work."""
    capability_passed = replay_gate.get("status") == "passed"
    return {
        **replay_gate,
        "capabilityResult": "passed" if capability_passed else "failed",
        "diagnosticOnly": True,
        "measuredEligible": False,
        "reason": (
            "Every capability-adapted calibration passed, but diagnostic "
            "plans cannot unlock measured work. Freeze a new evaluation to "
            "create a promotion-authoritative replay."
            if capability_passed
            else "The capability-adapted calibration failed; measured work "
            "remains locked."
        ),
    }


def frozen_prompt_evidence(case_root: Path) -> list[dict[str, str]]:
    """Locate immutable initial local prompts without changing their contents."""
    patterns = sorted(
        (
            case_root
            / "arms"
            / "mlx-swarm"
            / "artifacts"
        ).glob("*/*/attempts/*/attempt-001.json")
    )
    evidence: list[dict[str, str]] = []
    for path in patterns:
        try:
            attempt = _read_json(path)
        except (EvaluationError, OSError):
            continue
        prompt_digest = attempt.get("promptSha256")
        if isinstance(prompt_digest, str) and _SHA256.fullmatch(
            prompt_digest
        ):
            evidence.append({
                "taskId": str(attempt.get("taskId", "")),
                "promptSha256": prompt_digest,
                "path": str(path),
            })
    return evidence


def install_frozen_prompt_replay(
    session: Session,
    evidence: Sequence[dict[str, str]],
) -> None:
    """Copy exact saved initial prompts into a digest-bound replay session."""
    prompt_root = session.dir / "prompt-replay"
    prompt_root.mkdir(parents=True, exist_ok=True)
    replay: dict[str, dict[str, str]] = {}
    for item in evidence:
        task_id = _identifier(item["taskId"], "promptReplay.taskId")
        if task_id in replay:
            raise EvaluationError(
                f"Duplicate frozen prompt evidence for task {task_id}."
            )
        attempt_path = Path(item["path"]).resolve()
        attempt = _read_json(attempt_path)
        prompt = attempt.get("prompt")
        if not isinstance(prompt, str):
            raise EvaluationError(
                f"Frozen prompt evidence has no prompt for {task_id}."
            )
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if digest != item["promptSha256"]:
            raise EvaluationError(
                f"Frozen prompt evidence digest changed for {task_id}."
            )
        relative = Path("prompt-replay") / f"{task_id}.txt"
        _exclusive_text(session.dir / relative, prompt)
        replay[task_id] = {
            "path": str(relative),
            "sha256": digest,
            "sourceAttempt": str(attempt_path),
        }
    session.state["promptReplay"] = replay
    session._save()


def replay_failure_evidence(session: Session) -> str:
    """Return the most specific local failure without exposing rejected raw text."""
    messages: list[str] = []
    for task in session.state.get("tasks", {}).values():
        error = task.get("error")
        if isinstance(error, str) and error:
            messages.append(error)
        for violation in task.get("gateResult", {}).get("violations", []):
            message = violation.get("message")
            if isinstance(message, str) and message:
                messages.append(message)
        for verification in task.get("verificationResults", []):
            if not isinstance(verification, dict):
                continue
            relative = verification.get("output")
            if not isinstance(relative, str) or not relative:
                continue
            output_path = (session.dir / relative).resolve()
            if not _is_within(output_path, session.dir) or not output_path.is_file():
                continue
            output = output_path.read_text(
                encoding="utf-8",
                errors="replace",
            ).strip()
            if output:
                messages.append(output[-8_000:])
    return "; ".join(dict.fromkeys(messages))[:MAX_LOG_BYTES]


def retained_session_candidate_diff(session: Session) -> str:
    """Return the last validated mutating artifact even after an explicit revert."""
    tasks = session.state.get("tasks", {})
    if not isinstance(tasks, dict):
        return ""
    for task in reversed(list(tasks.values())):
        if (
            not isinstance(task, dict)
            or task.get("artifactType") not in {"patch", "test-suite"}
        ):
            continue
        normalized = task.get("normalizedOutput")
        artifact = task.get("artifact")
        if (
            isinstance(normalized, str)
            and normalized.startswith("diff --git ")
            and isinstance(artifact, dict)
            and isinstance(artifact.get("sha256"), str)
        ):
            return normalized
    return ""


@contextmanager
def exclusive_case_lock(
    evaluation_dir: Path,
    case_id: str,
) -> Iterable[None]:
    """Prevent concurrent preparation or execution of the same frozen case."""
    _identifier(case_id, "caseId")
    locks = evaluation_dir / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    path = locks / f"{case_id}.lock"
    token = f"{os.getpid()}:{time.time_ns()}"
    for attempt in range(2):
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            owner = ""
            try:
                owner = path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            pid_text = owner.partition(":")[0]
            if attempt == 0 and pid_text.isdigit() and not _pid_is_alive(
                int(pid_text)
            ):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise EvaluationError(
                f"Evaluation case is already locked: {case_id}"
            )
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            break
    else:  # pragma: no cover - loop always returns or raises
        raise EvaluationError(f"Could not lock evaluation case: {case_id}")
    try:
        yield
    finally:
        try:
            if path.read_text(encoding="utf-8").strip() == token:
                path.unlink()
        except FileNotFoundError:
            pass


def validate_pilot_evidence(
    evaluation_dir: Path,
    cases: Sequence[dict[str, Any]],
    results: Sequence[dict[str, Any]],
    profile: EvaluationProfile,
    store: EvaluationStore,
) -> None:
    """Prove the calibration gate before measured work can be unlocked."""
    if len(results) != len(cases) * 2:
        raise EvaluationError("Pilot serialization is incomplete.")
    for result in results:
        validate_arm_result(result)
        if result["frontierUsage"]["usageStatus"] != "reported":
            raise EvaluationError(
                "Pilot usage capture is incomplete; measured work is locked."
            )
        if (
            result["patch"]["sha256"] is not None
            and str(result["oracle"]["evidence"]).startswith(
                "Independent oracle could not apply candidate patch:"
            )
        ):
            raise EvaluationError(
                "Pilot oracle isolation failed; measured work is locked."
            )
    for case in cases:
        runtime_path = (
            evaluation_dir / "cases" / case["caseId"] / "runtime.json"
        )
        runtime = _read_json(runtime_path)
        base = Path(_text(runtime.get("baseSnapshot"), "runtime.baseSnapshot"))
        environment = Path(
            _text(runtime.get("environment"), "runtime.environment")
        )
        if not base.is_dir() or not environment.is_dir():
            raise EvaluationError("Pilot preparation evidence is incomplete.")
        if _git_text(base, ["remote"]):
            raise EvaluationError(
                "Pilot isolation failed: frozen workspace retains a remote."
            )
        if len(_git_text(base, ["rev-list", "--all"]).splitlines()) != 1:
            raise EvaluationError(
                "Pilot isolation failed: frozen workspace exposes history."
            )
        _number(
            runtime.get("preparationSeconds"),
            "runtime.preparationSeconds",
            0,
            10**9,
        )
    store._check_storage(profile)


def run_swarm_with_synthetic_operator(
    config: SwarmConfig,
    plan_path: Path,
    session_dir: Path,
    max_repair: int,
    *,
    timeout: int,
) -> CommandResult:
    argv = [
        sys.executable,
        "-m",
        "mlx_swarm.cli",
        "--config",
        str(config.source),
        "run",
        str(plan_path),
        "--session-dir",
        str(session_dir),
        "--max-repair",
        str(max_repair),
    ]
    started = time.perf_counter()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.monotonic() + timeout
    decided: set[str] = set()
    timed_out = False
    infrastructure_error: str | None = None
    while process.poll() is None:
        if time.monotonic() >= deadline:
            timed_out = True
            try:
                os.killpg(process.pid, 15)
            except ProcessLookupError:
                pass
            break
        state_path = session_dir / "session.json"
        if state_path.is_file():
            try:
                state = _read_json(state_path)
            except EvaluationError:
                time.sleep(0.1)
                continue
            for task_id, task in state.get("tasks", {}).items():
                status = task.get("status")
                if status == "awaiting_approval" and task_id not in decided:
                    manifest, _ = load_artifact(session_dir, task_id)
                    submit_artifact_decision(
                        session_dir,
                        task_id,
                        action="apply",
                        artifact_sha256=manifest["sha256"],
                        source="evaluation-harness",
                    )
                    decided.add(task_id)
                elif (
                    status == "verification_failed"
                    and f"reject:{task_id}" not in decided
                ):
                    infrastructure_error = task_verification_infrastructure_error(
                        session_dir,
                        task,
                    )
                    if infrastructure_error is not None:
                        manifest, _ = load_artifact(session_dir, task_id)
                        submit_artifact_decision(
                            session_dir,
                            task_id,
                            action="reject",
                            artifact_sha256=manifest["sha256"],
                            source="evaluation-harness",
                            reason=(
                                "Verifier infrastructure failed; measurement "
                                "is invalid."
                            ),
                        )
                        decided.add(f"reject:{task_id}")
                        continue
                    manifest, _ = load_artifact(session_dir, task_id)
                    submit_artifact_decision(
                        session_dir,
                        task_id,
                        action="reject",
                        artifact_sha256=manifest["sha256"],
                        source="evaluation-harness",
                        reason="Frozen evaluation verifier failed.",
                    )
                    decided.add(f"reject:{task_id}")
        time.sleep(0.1)
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, 9)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
    return CommandResult(
        argv=tuple(argv),
        returncode=process.returncode,
        stdout=stdout[:MAX_LOG_BYTES].decode("utf-8", "replace"),
        stderr=stderr[:MAX_LOG_BYTES].decode("utf-8", "replace"),
        elapsed_seconds=time.perf_counter() - started,
        timed_out=timed_out,
        infrastructure_error=infrastructure_error,
    )


def task_verification_infrastructure_error(
    session_dir: Path,
    task: dict[str, Any],
) -> str | None:
    """Return deterministic infrastructure evidence from the latest verifier."""
    results = task.get("verificationResults")
    if not isinstance(results, list) or not results:
        return None
    latest = results[-1]
    if not isinstance(latest, dict):
        return None
    relative_output = latest.get("output")
    if not isinstance(relative_output, str) or not relative_output:
        return None
    path = (session_dir / relative_output).resolve()
    if not _is_within(path, session_dir.resolve()) or not path.is_file():
        return "Verifier evidence path is missing or escapes the session."
    evidence = path.read_text(encoding="utf-8", errors="replace")
    return oracle_infrastructure_failure({"evidence": evidence})


def collect_buggy_execution_trace(
    case: dict[str, Any],
    *,
    evaluation_root: Path,
    workspace: Path,
    environment: Path,
    profile: EvaluationProfile,
) -> dict[str, list[int]]:
    """Collect buggy-revision called functions using only approved test argv."""
    evaluation_root = evaluation_root.resolve()
    workspace = workspace.resolve()
    environment = environment.resolve()
    container_environment = Path(
        container_path(environment, evaluation_root)
    )
    python = str(container_environment / "bin" / "python")
    commands = case.get("verificationArgv", [])
    if not isinstance(commands, list) or not commands:
        return {}
    raw_command = commands[0]
    if not isinstance(raw_command, list) or not raw_command:
        return {}
    executable = raw_command[0]
    rest = list(raw_command[1:])
    if executable in {"python", "python3"}:
        target = rest
    elif executable in {"pytest", "py.test"}:
        target = ["-m", "pytest", *rest]
    else:
        return {}
    argv = [
        python,
        "-m",
        "trace",
        "--listfuncs",
        f"--ignore-dir={container_environment}:/usr",
        *(
            ["--module", target[1], *target[2:]]
            if len(target) >= 2 and target[0] == "-m"
            else target
        ),
    ]
    result = run_command(
        docker_runtime_argv(
            image=profile.container.image,
            platform_name=profile.container.platform,
            evaluation_root=evaluation_root,
            cwd=workspace,
            argv=argv,
            network="none",
        ),
        cwd=evaluation_root,
        timeout=min(
            profile.frontier.local_timeout_seconds,
            1_800,
        ),
        max_output_bytes=5_000_000,
    )
    if result.timed_out:
        return {}
    called: dict[str, set[str]] = {}
    prefix = container_path(workspace, evaluation_root).rstrip("/") + "/"
    pattern = re.compile(
        r"^filename: (.+), modulename: .*?, funcname: (.+)$"
    )
    for line in result.stdout.splitlines():
        match = pattern.fullmatch(line.strip())
        if match is None:
            continue
        filename, function_name = match.groups()
        if not filename.startswith(prefix):
            continue
        relative = filename[len(prefix):]
        if _is_non_production_path(relative):
            continue
        called.setdefault(relative, set()).add(function_name)
    traced: dict[str, list[int]] = {}
    for relative, functions in called.items():
        source = workspace / relative
        if not source.is_file() or source.suffix not in {".py", ".pyi"}:
            continue
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        lines: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                continue
            if node.name not in functions:
                continue
            start = max(1, node.lineno)
            end = max(start, getattr(node, "end_lineno", start))
            lines.update(range(start, min(end, start + 300) + 1))
        if "<module>" in functions:
            lines.update(range(1, 101))
        if lines:
            traced[relative] = sorted(lines)[:10_000]
    cover_root = (
        evaluation_root
        / "cache"
        / "execution-traces"
        / _identifier(str(case.get("caseId", "trace")), "case.caseId")
    )
    if cover_root.exists():
        shutil.rmtree(cover_root)
    cover_root.mkdir(parents=True)
    count_argv = [
        python,
        "-m",
        "trace",
        "--count",
        f"--coverdir={container_path(cover_root, evaluation_root)}",
        f"--ignore-dir={container_environment}:/usr",
        *(
            ["--module", target[1], *target[2:]]
            if len(target) >= 2 and target[0] == "-m"
            else target
        ),
    ]
    try:
        count_result = run_command(
            docker_runtime_argv(
                image=profile.container.image,
                platform_name=profile.container.platform,
                evaluation_root=evaluation_root,
                cwd=workspace,
                argv=count_argv,
                network="none",
            ),
            cwd=evaluation_root,
            timeout=min(
                profile.frontier.local_timeout_seconds,
                1_800,
            ),
            max_output_bytes=5_000_000,
        )
        if not count_result.timed_out:
            executed = _executed_lines_from_trace_cover(
                workspace,
                cover_root,
            )
            if executed:
                traced = executed
    finally:
        shutil.rmtree(cover_root, ignore_errors=True)
    return dict(sorted(traced.items()))


def _executed_lines_from_trace_cover(
    workspace: Path,
    cover_root: Path,
) -> dict[str, list[int]]:
    """Map stdlib trace count files back to frozen workspace line numbers."""
    traced: dict[str, list[int]] = {}
    for source in sorted(workspace.rglob("*.py")):
        if source.is_symlink() or not source.is_file():
            continue
        try:
            relative = source.relative_to(workspace).as_posix()
        except ValueError:
            continue
        module_parts = list(Path(relative).with_suffix("").parts)
        if module_parts and module_parts[-1] == "__init__":
            module_parts.pop()
        module = ".".join(module_parts)
        if not module:
            continue
        cover = cover_root / f"{module}.cover"
        if not cover.is_file():
            continue
        try:
            annotated = cover.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
        except OSError:
            continue
        lines = [
            line_number
            for line_number, text in enumerate(annotated, start=1)
            if re.match(r"^\s*\d+:", text)
        ]
        if lines:
            traced[relative] = lines[:10_000]
    return traced


def _source_context_line_ranges(
    source_context: str,
    *,
    production_only: bool = True,
) -> dict[str, list[list[int]]]:
    """Extract bounded repository ranges from sealed SOURCE blocks."""
    ranges: dict[str, list[list[int]]] = {}
    for path, raw_start, raw_end in re.findall(
        r"(?m)^SOURCE (.+):L(\d+)-L(\d+)$",
        source_context,
    ):
        if production_only and _is_non_production_path(path):
            continue
        start = int(raw_start)
        end = int(raw_end)
        if start <= 0 or end < start:
            continue
        ranges.setdefault(path, []).append([start, end])
    return dict(sorted(ranges.items()))


def collect_buggy_runtime_locals(
    case: dict[str, Any],
    *,
    evaluation_root: Path,
    workspace: Path,
    environment: Path,
    profile: EvaluationProfile,
    source_context: str,
) -> list[dict[str, Any]]:
    """Capture bounded, value-safe locals on sealed production source lines."""
    evaluation_root = evaluation_root.resolve()
    workspace = workspace.resolve()
    environment = environment.resolve()
    ranges = _source_context_line_ranges(source_context)
    if not ranges:
        return []
    commands = case.get("verificationArgv", [])
    if not isinstance(commands, list) or not commands:
        return []
    raw_command = commands[0]
    if not isinstance(raw_command, list) or not raw_command:
        return []
    executable = raw_command[0]
    rest = list(raw_command[1:])
    if executable in {"python", "python3"}:
        target = rest
    elif executable in {"pytest", "py.test"}:
        target = ["-m", "pytest", *rest]
    else:
        return []
    if not target:
        return []

    trace_root = evaluation_root / "cache" / "execution-traces"
    trace_root.mkdir(parents=True, exist_ok=True)
    case_id = _identifier(
        str(case.get("caseId", "trace")),
        "case.caseId",
    )
    runner_path = trace_root / "runtime-locals-runner.py"
    ranges_path = trace_root / f"{case_id}.ranges.json"
    output_path = trace_root / f"{case_id}.locals.json"
    runner_path.write_text(_TRACE_LOCALS_RUNNER, encoding="utf-8")
    _atomic_json(ranges_path, ranges)
    output_path.unlink(missing_ok=True)

    container_environment = Path(
        container_path(environment, evaluation_root)
    )
    python = str(container_environment / "bin" / "python")
    argv = [
        python,
        container_path(runner_path, evaluation_root),
        container_path(workspace, evaluation_root),
        container_path(output_path, evaluation_root),
        container_path(ranges_path, evaluation_root),
        *target,
    ]
    try:
        result = run_command(
            docker_runtime_argv(
                image=profile.container.image,
                platform_name=profile.container.platform,
                evaluation_root=evaluation_root,
                cwd=workspace,
                argv=argv,
                network="none",
            ),
            cwd=evaluation_root,
            timeout=min(
                profile.frontier.local_timeout_seconds,
                1_800,
            ),
            max_output_bytes=1_000_000,
        )
        if result.timed_out or not output_path.is_file():
            return []
        raw = _read_json(output_path)
        if (
            not isinstance(raw, dict)
            or raw.get("schemaVersion") != 1
            or not isinstance(raw.get("records"), list)
        ):
            return []
        records: list[dict[str, Any]] = []
        for item in raw["records"][:4000]:
            if not isinstance(item, dict) or set(item) != {
                "path",
                "line",
                "function",
                "sample",
                "locals",
            }:
                continue
            path = item["path"]
            line = item["line"]
            function = item["function"]
            sample = item["sample"]
            local_values = item["locals"]
            path_ranges = ranges.get(path) if isinstance(path, str) else None
            if (
                not path_ranges
                or isinstance(line, bool)
                or not isinstance(line, int)
                or not any(start <= line <= end for start, end in path_ranges)
                or not isinstance(function, str)
                or not function
                or len(function) > 200
                or isinstance(sample, bool)
                or not isinstance(sample, int)
                or sample <= 0
                or not isinstance(local_values, dict)
            ):
                continue
            records.append({
                "path": path,
                "line": line,
                "function": function,
                "sample": sample,
                "locals": local_values,
            })
        return records
    finally:
        ranges_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)


def run_case_verifier(
    manifest_path: Path,
    workspace: Path,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    manifest = _read_json(manifest_path)
    _exact_keys(
        manifest,
        "verifier",
        {
            "schemaVersion",
            "caseId",
            "evaluationRoot",
            "containerImage",
            "containerDigest",
            "containerPlatform",
            "environment",
            "commands",
            "timeoutSeconds",
        },
    )
    if _integer(manifest["schemaVersion"], "verifier.schemaVersion", 1, 1) != 1:
        raise EvaluationError("Unsupported verifier schema.")
    evaluation_root = Path(
        _text(manifest["evaluationRoot"], "verifier.evaluationRoot")
    ).resolve()
    if not evaluation_root.is_dir():
        raise EvaluationError("Verifier evaluation root is missing.")
    workspace = workspace.resolve()
    manifest_path = manifest_path.resolve()
    if (
        not _is_within(workspace, evaluation_root)
        or not _is_within(manifest_path, evaluation_root)
    ):
        raise EvaluationError(
            "Verifier paths must remain inside the frozen evaluation root."
        )
    container_image = _text(
        manifest["containerImage"],
        "verifier.containerImage",
    )
    container_digest = _text(
        manifest["containerDigest"],
        "verifier.containerDigest",
    )
    if _IMAGE_DIGEST.fullmatch(container_digest) is None:
        raise EvaluationError("Verifier container digest is invalid.")
    container_platform = _enum(
        manifest["containerPlatform"],
        "verifier.containerPlatform",
        {"linux/amd64", "linux/arm64"},
    )
    inspect_container_contract(
        container_image,
        container_digest,
        container_platform,
        cwd=evaluation_root,
    )
    environment = Path(
        _text(manifest["environment"], "verifier.environment")
    ).resolve()
    if (
        not environment.is_dir()
        or not _is_within(environment, evaluation_root)
    ):
        raise EvaluationError("Verifier environment is missing.")
    commands = _list(
        manifest["commands"],
        "verifier.commands",
        minimum=1,
        maximum=100,
    )
    timeout = _integer(
        manifest["timeoutSeconds"],
        "verifier.timeoutSeconds",
        1,
        3_600,
    )
    started = time.perf_counter()
    deadline = (
        time.monotonic() + timeout_seconds
        if timeout_seconds is not None
        else None
    )
    logs: list[str] = []
    exit_code = 0
    timed_out = False
    for index, raw_command in enumerate(commands):
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            exit_code = -1
            logs.append("Verifier deadline expired before the next command.")
            break
        argv = rewrite_benchmark_argv(
            _unique_or_repeated_text_array(
                raw_command,
                f"verifier.commands[{index}]",
                minimum=1,
                maximum=128,
            ),
            Path(container_path(environment, evaluation_root)),
            for_setup=False,
            require_environment=False,
        )
        result = run_command(
            docker_runtime_argv(
                image=container_image,
                platform_name=container_platform,
                evaluation_root=evaluation_root,
                cwd=workspace,
                argv=argv,
                network="none",
            ),
            cwd=evaluation_root,
            timeout=(
                timeout
                if deadline is None
                else max(
                    1,
                    min(timeout, math.floor(deadline - time.monotonic())),
                )
            ),
            max_output_bytes=MAX_LOG_BYTES,
        )
        logs.append(
            f"$ {' '.join(argv)}\n{result.stdout}{result.stderr}"
        )
        exit_code = result.returncode
        timed_out = result.timed_out
        if result.timed_out or result.returncode != 0:
            break
    evidence = "\n".join(logs)[-MAX_LOG_BYTES:]
    return {
        "passed": exit_code == 0 and not timed_out,
        "exitCode": exit_code,
        "evidence": evidence,
        "elapsedSeconds": time.perf_counter() - started,
        "timedOut": timed_out,
    }


def oracle_infrastructure_failure(
    result: dict[str, Any],
) -> str | None:
    """Identify preparation failures that cannot be repaired by source edits."""
    evidence = str(result.get("evidence") or "").lower()
    markers = (
        "infrastructure_error:",
        "failed to connect to the docker api",
        "cannot connect to the docker daemon",
        "permission denied while trying to connect to the docker api",
        "pinned benchmark container is unavailable",
        "docker image digest mismatch",
        "active docker context is unavailable",
        "verifier evaluation root is missing",
        "verifier environment is missing",
        "benchmark virtual environment has no python",
        "no module named 'mlx_swarm'",
    )
    marker = next((value for value in markers if value in evidence), None)
    if marker is None:
        return None
    return (
        "Verifier infrastructure failed independently of the candidate: "
        f"{marker}"
    )


def rewrite_benchmark_argv(
    raw_argv: Sequence[str],
    environment: Path,
    *,
    for_setup: bool,
    require_environment: bool = True,
) -> list[str]:
    if not raw_argv or raw_argv[0] not in _SAFE_BENCHMARK_COMMANDS:
        raise EvaluationError("Benchmark command is not allowlisted.")
    executable = raw_argv[0]
    rest = list(raw_argv[1:])
    python = environment / "bin" / "python"
    if require_environment and not python.is_file():
        raise EvaluationError("Benchmark virtual environment has no Python.")
    if executable in {"python", "python3"}:
        return [str(python), *rest]
    if executable in {"pytest", "py.test", "tox"}:
        module = "pytest" if executable in {"pytest", "py.test"} else "tox"
        return [str(python), "-m", module, *rest]
    if executable in {"pip", "pip3"}:
        if not for_setup or not rest or rest[0] != "install":
            raise EvaluationError(
                "Only allowlisted setup pip install commands are permitted."
            )
        return ["uv", "pip", *rest]
    return [executable, *rest]


def normalize_setup_parallelism(argv: Sequence[str]) -> list[str]:
    """Replace BugsInPy's ambiguous build_ext -j 0 with a fixed job count."""
    values = list(argv)
    if "setup.py" not in values or "build_ext" not in values:
        return values
    for flag in ("-j", "--parallel"):
        if flag not in values:
            continue
        index = values.index(flag) + 1
        if index < len(values) and values[index] == "0":
            values[index] = str(BENCHMARK_BUILD_JOBS)
    return values


def dependency_environment_receipt(
    case: dict[str, Any],
    profile: EvaluationProfile,
) -> dict[str, Any]:
    payload = {
        "schemaVersion": 1,
        "project": case["project"],
        "pythonVersion": case["pythonVersion"],
        "pythonBootstrap": list(profile.python_bootstrap),
        "requirements": case_dependency_constraints(case, profile),
        "dependencyRoots": list(
            profile.dependency_roots[case["project"]]
        ),
        "containerDigest": profile.container.digest,
        "containerPlatform": profile.container.platform,
    }
    return {
        **payload,
        "sha256": canonical_json_sha256(payload),
    }


def canonical_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def dependency_requirements(
    requirements: Sequence[str],
    project: str,
    profile: EvaluationProfile,
) -> list[str]:
    requirements = case_dependency_constraints(
        {"project": project, "requirements": list(requirements)},
        profile,
    )
    by_name: dict[str, str] = {}
    for requirement in requirements:
        match = re.match(r"^([A-Za-z0-9_.-]+)", requirement)
        if match is None:
            raise EvaluationError("Pinned dependency has no package name.")
        by_name[canonical_package_name(match.group(1))] = requirement
    selected: list[str] = []
    for root in profile.dependency_roots[project]:
        canonical = canonical_package_name(root)
        requirement = by_name.get(canonical)
        if requirement is None:
            raise EvaluationError(
                f"{project} dependency root is absent from the case freeze: {root}"
            )
        selected.append(requirement)
    return selected


def case_dependency_constraints(
    case: dict[str, Any],
    profile: EvaluationProfile,
) -> list[str]:
    values: dict[str, str] = {}
    order: list[str] = []
    for requirement in [
        *case["requirements"],
        *profile.dependency_pins[case["project"]],
    ]:
        match = re.match(r"^([A-Za-z0-9_.-]+)", requirement)
        if match is None:
            raise EvaluationError("Pinned dependency has no package name.")
        name = canonical_package_name(match.group(1))
        if name not in values:
            order.append(name)
        values[name] = requirement
    return [values[name] for name in order]


def validate_resolved_dependencies(
    freeze_output: str,
    constraints: Sequence[str],
    bootstrap: Sequence[str],
) -> list[str]:
    allowed: dict[str, set[str]] = {}
    for requirement in [*constraints, *bootstrap]:
        base = requirement.partition(";")[0].strip()
        match = re.fullmatch(
            r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==(.+)",
            base,
        )
        if match is None:
            raise EvaluationError("Dependency constraint is not exact.")
        allowed.setdefault(
            canonical_package_name(match.group(1)),
            set(),
        ).add(match.group(2))
    resolved: list[str] = []
    for raw_line in freeze_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==(.+)", line)
        if match is None:
            raise EvaluationError(
                f"Resolved dependency is not an exact registry pin: {line}"
            )
        name = canonical_package_name(match.group(1))
        if match.group(2) not in allowed.get(name, set()):
            raise EvaluationError(
                f"Resolved dependency escaped the frozen constraints: {line}"
            )
        resolved.append(line)
    if not resolved:
        raise EvaluationError("Resolved dependency lock is empty.")
    return sorted(resolved, key=str.lower)


def dependency_environment_path(
    evaluation_dir: Path,
    case: dict[str, Any],
    profile: EvaluationProfile,
) -> Path:
    receipt = dependency_environment_receipt(case, profile)
    project = _identifier(
        str(case["project"]).lower(),
        "case.project",
    )
    return (
        evaluation_dir
        / "cache"
        / "environments"
        / f"{project}-{receipt['sha256'][:16]}"
    )


def container_path(path: Path, evaluation_root: Path) -> str:
    root = evaluation_root.resolve()
    target = path.resolve()
    if not _is_within(target, root):
        raise EvaluationError(
            "Benchmark runtime path escapes the evaluation root."
        )
    relative = target.relative_to(root)
    return "/evaluation" if not relative.parts else f"/evaluation/{relative.as_posix()}"


def docker_runtime_argv(
    *,
    image: str,
    platform_name: str,
    evaluation_root: Path,
    cwd: Path,
    argv: Sequence[str],
    network: str,
    extra_env: Sequence[str] = (),
) -> list[str]:
    if network not in {"bridge", "none"}:
        raise EvaluationError("Unsupported benchmark container network mode.")
    if any(
        re.fullmatch(r"[A-Z][A-Z0-9_]*=[A-Za-z0-9._/: -]+", item) is None
        for item in extra_env
    ):
        raise EvaluationError("Invalid benchmark container environment value.")
    workdir = container_path(cwd, evaluation_root)
    container_name = (
        "mlx-swarm-eval-"
        + hashlib.sha256(
            f"{os.getpid()}:{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:20]
    )
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--label",
        "mlx-swarm.evaluation=true",
        "--platform",
        platform_name,
        "--network",
        network,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--volume",
        f"{evaluation_root.resolve()}:/evaluation",
        "--workdir",
        workdir,
        "--env",
        f"HOME={workdir}",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        f"PYTHONPATH={workdir}",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "NO_COLOR=1",
        "--env",
        "UV_CACHE_DIR=/tmp/uv-cache",
        "--env",
        "UV_PYTHON_INSTALL_DIR=/evaluation/cache/python",
        "--env",
        "CCACHE_DIR=/evaluation/cache/ccache",
        "--env",
        "CCACHE_MAXSIZE=2G",
        "--env",
        "CCACHE_BASEDIR=/evaluation",
        "--env",
        "CCACHE_NOHASHDIR=true",
        "--env",
        "CC=ccache gcc",
        "--env",
        "CXX=ccache g++",
        *[
            value
            for item in extra_env
            for value in ("--env", item)
        ],
        image,
        *argv,
    ]


def docker_case_argv(
    profile: EvaluationProfile,
    evaluation_root: Path,
    cwd: Path,
    argv: Sequence[str],
    *,
    network: str,
    extra_env: Sequence[str] = (),
) -> list[str]:
    return docker_runtime_argv(
        image=profile.container.image,
        platform_name=profile.container.platform,
        evaluation_root=evaluation_root,
        cwd=cwd,
        argv=argv,
        network=network,
        extra_env=extra_env,
    )


def is_dependency_or_project_install(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    if argv[0] in {"pip", "pip3"}:
        return True
    if argv[0] not in {"python", "python3"}:
        return False
    rest = list(argv[1:])
    if len(rest) >= 2 and rest[:2] == ["setup.py", "install"]:
        return True
    compact = "".join(rest)
    return compact.startswith("-mpipinstall")


def sanitized_case_environment(
    environment: Path,
    workspace: Path,
) -> dict[str, str]:
    path = f"{environment / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin"
    values = {
        "PATH": path,
        "HOME": str(workspace),
        "TMPDIR": tempfile.gettempdir(),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PYTHONPATH": str(workspace),
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
    }
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        if name in os.environ:
            values[name] = os.environ[name]
    return values


def build_task_packet(
    case: dict[str, Any],
    runtime: dict[str, Any],
    approved_write_roots: Sequence[str],
    *,
    maximum_characters: int = 120_000,
) -> str:
    roots = "\n".join(f"- {path}" for path in approved_write_roots)
    tree, sources = deterministic_case_context(case, runtime)
    execution_map = _render_executed_line_map(
        runtime.get("executedSourceLines", {}),
        source_context=sources,
    )
    runtime_state = _render_runtime_local_evidence(
        runtime.get("runtimeLocalEvidence", []),
        source_context=sources,
        failure_evidence=str(runtime.get("failureEvidence", "")),
    )
    packet = (
        f"CASE: {case['caseId']}\n"
        f"PROJECT: {case['project']}\n"
        f"OBJECTIVE: {case['objective']}\n"
        "BOUNDARY: Modify production code only. Do not modify tests, Git "
        "metadata, dependencies, or benchmark evidence.\n"
        "APPROVED WRITE ROOTS (identical for both paired arms):\n"
        f"{roots}\n"
        "ACCEPTANCE COMMANDS (fixed argv, never a shell):\n"
        f"{json.dumps(case['verificationArgv'], sort_keys=True)}\n"
        "FROZEN REPOSITORY TREE:\n"
        f"{tree}\n"
        "FROZEN BUGGY-RUN EXECUTED LINE MAP:\n"
        f"{execution_map}\n"
        "FROZEN BUGGY-RUN LOCAL STATE SAMPLES:\n"
        f"{runtime_state}\n"
        "FROZEN RELEVANT TEST AND TRACEBACK SOURCE CONTEXT:\n"
        f"{sources}\n"
        "INITIAL FAILURE EVIDENCE:\n"
        f"{runtime['failureEvidence'][:20_000]}"
    )
    if len(packet) <= maximum_characters:
        return packet
    marker = "\n...[task packet truncated deterministically]"
    return packet[:maximum_characters - len(marker)] + marker


def _render_executed_line_map(
    raw: Any,
    *,
    source_context: str | None = None,
) -> str:
    """Render bounded production-only executed line ranges."""
    if not isinstance(raw, dict):
        return "(unavailable)"
    selected_ranges: dict[str, list[tuple[int, int]]] = {}
    if isinstance(source_context, str):
        for path, raw_start, raw_end in re.findall(
            r"(?m)^SOURCE (.+):L(\d+)-L(\d+)$",
            source_context,
        ):
            selected_ranges.setdefault(path, []).append(
                (int(raw_start), int(raw_end)),
            )
    rows: list[str] = []
    for path, values in sorted(raw.items()):
        if (
            not isinstance(path, str)
            or _is_non_production_path(path)
            or Path(path).suffix not in {".py", ".pyi"}
            or not isinstance(values, list)
        ):
            continue
        lines = sorted({
            value
            for value in values
            if isinstance(value, int) and value > 0
        })
        if selected_ranges:
            ranges_for_path = selected_ranges.get(path, [])
            lines = [
                value
                for value in lines
                if any(
                    start <= value <= end
                    for start, end in ranges_for_path
                )
            ]
        if not lines:
            continue
        ranges: list[str] = []
        start = previous = lines[0]
        for value in lines[1:]:
            if value == previous + 1:
                previous = value
                continue
            ranges.append(
                str(start) if start == previous else f"{start}-{previous}"
            )
            start = previous = value
        ranges.append(
            str(start) if start == previous else f"{start}-{previous}"
        )
        rendered = ",".join(ranges[:240])
        if len(ranges) > 240:
            rendered += ",...[ranges truncated]"
        rows.append(f"- {path}: {rendered}")
        if sum(len(row) for row in rows) >= 12_000:
            rows.append("...[executed line map truncated]")
            break
    return "\n".join(rows) if rows else "(unavailable)"


def _render_runtime_local_evidence(
    raw: Any,
    *,
    source_context: str,
    failure_evidence: str,
) -> str:
    """Rank and render bounded safe-local snapshots from the buggy verifier."""
    if not isinstance(raw, list):
        return "(unavailable)"
    allowed = _source_context_line_ranges(source_context)
    contrast_rows = _render_runtime_local_contrasts(
        raw,
        allowed=allowed,
        failure_evidence=failure_evidence,
    )
    source_blocks = [
        (index, path, int(raw_start), int(raw_end))
        for index, (path, raw_start, raw_end) in enumerate(re.findall(
            r"(?m)^SOURCE (.+):L(\d+)-L(\d+)$",
            source_context,
        ))
        if not _is_non_production_path(path)
    ]
    query_terms = _context_term_counts(failure_evidence)
    ranked: list[
        tuple[float, int, str, str, int, int, tuple[str, ...], str]
    ] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        line = item.get("line")
        function = item.get("function")
        sample = item.get("sample")
        local_values = item.get("locals")
        path_ranges = allowed.get(path) if isinstance(path, str) else None
        if (
            not path_ranges
            or isinstance(line, bool)
            or not isinstance(line, int)
            or not any(start <= line <= end for start, end in path_ranges)
            or not isinstance(function, str)
            or isinstance(sample, bool)
            or not isinstance(sample, int)
            or not isinstance(local_values, dict)
        ):
            continue
        source_block = next(
            (
                index
                for index, block_path, start, end in source_blocks
                if block_path == path and start <= line <= end
            ),
            len(source_blocks),
        )
        observed_strings = _runtime_local_strings(local_values)
        encoded = json.dumps(
            _compact_runtime_local_value(local_values),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded) > 800:
            encoded = encoded[:760] + "...[locals truncated]"
        evidence_text = f"{path} {function} {encoded}"
        evidence_terms = _context_term_counts(evidence_text)
        score = sum(
            min(query_terms.get(term, 0), 8) * min(count, 3)
            for term, count in evidence_terms.items()
        )
        if function in failure_evidence:
            score += 100_000
        if path in failure_evidence:
            score += 10_000
        for observed in observed_strings:
            if len(observed) >= 6 and observed in failure_evidence:
                score += 2_000 + min(len(observed), 240)
        row = (
            f"- {path}:L{line} {function} sample={sample} "
            f"locals={encoded}"
        )
        ranked.append((
            float(score),
            source_block,
            path,
            function,
            line,
            sample,
            tuple(observed_strings),
            row,
        ))
    ranked.sort(
        key=lambda item: (
            -item[0],
            item[1],
            item[2],
            item[3],
            item[4],
            item[5],
            item[7],
        )
    )
    ordered: list[
        tuple[float, int, str, str, int, int, tuple[str, ...], str]
    ] = []
    selected_rows: set[str] = set()
    for source_block in range(len(source_blocks) + 1):
        block_functions: list[tuple[str, str]] = []
        for item in ranked:
            (
                _score,
                item_block,
                path,
                function,
                _line,
                _sample,
                _has_strings,
                _row,
            ) = item
            function_key = (path, function)
            if (
                item_block != source_block
                or function_key in block_functions
            ):
                continue
            block_functions.append(function_key)
            if len(block_functions) >= 2:
                break
        for function_key in block_functions:
            function_items = [
                item
                for item in ranked
                if (
                    item[1] == source_block
                    and (item[2], item[3]) == function_key
                )
            ]
            choices = function_items[:1]
            observed_signatures = {
                choice[6] for choice in choices if choice[6]
            }
            for candidate in sorted(
                (
                    item for item in function_items if item[6]
                ),
                key=lambda item: (
                    item[4],
                    item[5],
                    item[7],
                ),
            ):
                if candidate[6] in observed_signatures:
                    continue
                choices.append(candidate)
                observed_signatures.add(candidate[6])
                if len(choices) >= 3:
                    break
            for choice in choices[:3]:
                row = choice[7]
                if row in selected_rows:
                    continue
                ordered.append(choice)
                selected_rows.add(row)
    ordered.extend(
        item for item in ranked if item[7] not in selected_rows
    )
    rows: list[str] = []
    if contrast_rows:
        rows.extend([
            "CAUSAL CONTRAST CANDIDATES (same executed location, distinct calls):",
            *contrast_rows,
            "RAW LOCAL SAMPLES:",
        ])
    used = sum(len(row) + 1 for row in rows)
    for (
        _score,
        _source_block,
        _path,
        _function,
        _line,
        _sample,
        _observed_strings,
        row,
    ) in ordered:
        addition = len(row) + 1
        if used + addition > MAX_TASK_PACKET_RUNTIME_STATE_CHARS:
            rows.append("...[runtime local samples truncated]")
            break
        rows.append(row)
        used += addition
        if len(rows) >= 100:
            rows.append("...[runtime local samples truncated]")
            break
    return "\n".join(rows) if rows else "(unavailable)"


def _render_runtime_local_contrasts(
    raw: list[Any],
    *,
    allowed: dict[str, list[tuple[int, int]]],
    failure_evidence: str,
) -> list[str]:
    """Expose deterministic scalar differences between calls at one location."""
    grouped: dict[
        tuple[str, str, int],
        list[tuple[int, dict[str, Any]]],
    ] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        function = item.get("function")
        line = item.get("line")
        sample = item.get("sample")
        local_values = item.get("locals")
        if (
            not isinstance(path, str)
            or not isinstance(function, str)
            or isinstance(line, bool)
            or not isinstance(line, int)
            or isinstance(sample, bool)
            or not isinstance(sample, int)
            or not isinstance(local_values, dict)
            or not any(
                start <= line <= end
                for start, end in allowed.get(path, [])
            )
        ):
            continue
        compact = _compact_runtime_local_value(local_values)
        if isinstance(compact, dict):
            grouped.setdefault((path, function, line), []).append(
                (sample, compact),
            )
    candidates: list[
        tuple[float, bool, tuple[str, str], int, str]
    ] = []
    failure_lower = failure_evidence.lower()
    preferred_terms = {
        "comment",
        "inside",
        "length",
        "line_str",
        "shape",
        "should",
        "type",
        "value",
    }
    for (path, function, line), samples in sorted(grouped.items()):
        unique = {
            sample: values for sample, values in samples
        }
        ordered = sorted(unique.items())
        if len(ordered) < 2:
            continue
        for (left_sample, left), (right_sample, right) in combinations(
            ordered[:4],
            2,
        ):
            left_flat = _flatten_runtime_local_scalars(left)
            right_flat = _flatten_runtime_local_scalars(right)
            differences: list[tuple[float, str, Any, Any]] = []
            for key in sorted(left_flat.keys() & right_flat.keys()):
                left_value = left_flat[key]
                right_value = right_flat[key]
                if left_value == right_value:
                    continue
                rendered_pair = (
                    f"{_render_runtime_scalar(left_value)} -> "
                    f"{_render_runtime_scalar(right_value)}"
                )
                score = 0.0
                lowered_key = key.lower()
                score += 20.0 * sum(
                    term in lowered_key for term in preferred_terms
                )
                if any(
                    isinstance(value, str)
                    and len(value) >= 6
                    and value.lower() in failure_lower
                    for value in (left_value, right_value)
                ):
                    score += 1_000.0
                score += min(len(rendered_pair), 240) / 100.0
                differences.append(
                    (score, key, left_value, right_value),
                )
            if not differences:
                continue
            differences.sort(key=lambda item: (-item[0], item[1]))
            rendered = "; ".join(
                f"{key}: {_render_runtime_scalar(left_value)} -> "
                f"{_render_runtime_scalar(right_value)}"
                for _score, key, left_value, right_value
                in differences[:8]
            )
            row = (
                f"- {path}:L{line} {function} sample={left_sample} "
                f"vs sample={right_sample}: {rendered}"
            )
            candidates.append((
                sum(item[0] for item in differences[:8]),
                any(
                    isinstance(left_value, str)
                    or isinstance(right_value, str)
                    for _score, _key, left_value, right_value
                    in differences
                ),
                (path, function),
                line,
                row,
            ))
    by_function: dict[
        tuple[str, str],
        list[tuple[float, bool, tuple[str, str], int, str]],
    ] = {}
    for candidate in candidates:
        by_function.setdefault(candidate[2], []).append(candidate)
    primary: list[
        tuple[float, bool, tuple[str, str], int, str]
    ] = []
    secondary: list[
        tuple[float, bool, tuple[str, str], int, str]
    ] = []
    for function_candidates in by_function.values():
        ordered = sorted(
            function_candidates,
            key=lambda item: (
                -int(item[1]),
                item[3],
                -item[0],
                item[4],
            ),
        )
        primary.append(ordered[0])
        secondary.extend(ordered[1:])
    candidates = [
        *sorted(
            primary,
            key=lambda item: (
                -int(item[1]),
                -item[0],
                item[2],
                item[3],
                item[4],
            ),
        ),
        *sorted(
            secondary,
            key=lambda item: (
                -int(item[1]),
                -item[0],
                item[2],
                item[3],
                item[4],
            ),
        ),
    ]
    rows: list[str] = []
    selected_rows: set[str] = set()
    selected_functions: dict[tuple[str, str], int] = {}
    used = 0
    for per_function_limit in (1, 2):
        for _score, _has_string, function_key, _line, row in candidates:
            if (
                row in selected_rows
                or selected_functions.get(function_key, 0)
                >= per_function_limit
            ):
                continue
            if used + len(row) + 1 > 5_000:
                continue
            rows.append(row)
            selected_rows.add(row)
            selected_functions[function_key] = (
                selected_functions.get(function_key, 0) + 1
            )
            used += len(row) + 1
            if len(rows) >= 12:
                return rows
    return rows


def _flatten_runtime_local_scalars(
    value: Any,
    *,
    prefix: str = "",
    depth: int = 0,
) -> dict[str, Any]:
    """Flatten bounded JSON-safe locals without evaluating application code."""
    if depth > 8:
        return {}
    if isinstance(value, dict):
        flattened: dict[str, Any] = {}
        for key, item in value.items():
            if key == "type":
                continue
            child = f"{prefix}.{key}" if prefix else key
            flattened.update(
                _flatten_runtime_local_scalars(
                    item,
                    prefix=child,
                    depth=depth + 1,
                )
            )
        return flattened
    if isinstance(value, list):
        return {
            f"{prefix}.length": len(value),
        } if prefix else {}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return {prefix: value} if prefix else {}
    return {}


def _render_runtime_scalar(value: Any) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(rendered) > 180:
        rendered = rendered[:160] + "...[truncated]"
    return rendered


def _runtime_local_strings(value: Any) -> list[str]:
    """Return bounded string observations from a safe-local summary."""
    found: list[str] = []
    pending = [value]
    while pending and len(found) < 32:
        current = pending.pop()
        if isinstance(current, dict):
            if (
                current.get("type") == "str"
                and isinstance(current.get("value"), str)
            ):
                found.append(current["value"])
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return found


def _compact_runtime_local_value(value: Any) -> Any:
    """Remove noisy identities/items while preserving scalar causal state."""
    if isinstance(value, dict):
        return {
            key: _compact_runtime_local_value(item)
            for key, item in value.items()
            if key not in {"items", "keys"}
        }
    if isinstance(value, list):
        return [
            _compact_runtime_local_value(item)
            for item in value[:4]
        ]
    return value


def ensure_pair_contract(
    case: dict[str, Any],
    runtime: dict[str, Any],
    *,
    maximum_characters: int,
) -> dict[str, Any]:
    """Freeze the one exact authority/evidence packet consumed by both arms."""
    base = Path(str(runtime["baseSnapshot"])).resolve()
    if not base.is_dir():
        raise EvaluationError("Pair contract base snapshot is unavailable.")
    roots = evaluation_write_roots(base)
    task_packet = build_task_packet(
        case,
        runtime,
        roots,
        maximum_characters=maximum_characters,
    )
    payload = {
        "schemaVersion": 1,
        "evaluationProtocolVersion": FAIR_EVALUATION_PROTOCOL_VERSION,
        "caseId": case["caseId"],
        "baseSha": _git_text(base, ["rev-parse", "HEAD"]),
        "approvedWriteRoots": roots,
        "taskPacketSha256": hashlib.sha256(
            task_packet.encode("utf-8")
        ).hexdigest(),
        "taskPacket": task_packet,
    }
    path = base.parent / "pair-contract.json"
    if path.is_file():
        existing = _read_json(path)
        if existing != payload:
            raise EvaluationError(
                "Frozen paired-arm contract differs from current case evidence."
            )
        return existing
    _exclusive_json(path, payload)
    return payload


def deterministic_case_context(
    case: dict[str, Any],
    runtime: dict[str, Any],
) -> tuple[str, str]:
    """Build identical non-future evidence with ranked implementation windows.

    Whole production files are actively harmful here: a large file consumes
    the packet from line one and can hide the implementation exercised by the
    failing test. Requested tests remain authoritative input, while production
    windows are ranked only from buggy-revision test text and failure evidence.
    No fixed-revision content participates in selection.
    """
    base_value = runtime.get("baseSnapshot")
    if not isinstance(base_value, str):
        return "(unavailable)", "(unavailable)"
    base = Path(base_value).resolve()
    if not base.is_dir():
        return "(unavailable)", "(unavailable)"
    files = [
        child
        for child in sorted(base.rglob("*"))
        if (
            child.is_file()
            and not child.is_symlink()
            and ".git" not in child.relative_to(base).parts
        )
    ]
    relative = [child.relative_to(base).as_posix() for child in files]
    tree = "\n".join(relative)
    if len(tree) > MAX_TASK_PACKET_TREE_CHARS:
        tree = (
            tree[:MAX_TASK_PACKET_TREE_CHARS]
            + "\n...[tree truncated deterministically]"
        )

    requested = {
        str(value)
        for value in case.get("testFiles", [])
        if isinstance(value, str)
    }
    failure = str(runtime.get("failureEvidence", ""))
    requested.update(
        path
        for path in relative
        if path in failure and _is_non_production_path(path)
    )
    blocks: list[str] = []
    used = 0
    by_relative = dict(zip(relative, files))
    initial_query = failure + "\n" + json.dumps(
        case.get("verificationArgv", [])
    )
    query_parts = [initial_query]
    requested_budget = min(30_000, MAX_TASK_PACKET_SOURCE_CHARS // 2)
    per_requested = max(
        4_000,
        requested_budget // max(1, len(requested)),
    )
    for path in sorted(requested):
        child = by_relative.get(path)
        if child is None:
            continue
        try:
            content = child.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        query_parts.append(content)
        lines = content.splitlines()
        requested_windows = _requested_source_windows(
            lines,
            initial_query,
            maximum_characters=per_requested,
        )
        for start, end, body in requested_windows:
            label = f"{path}:L{start}-L{end}"
            header = f"SOURCE {label}\n"
            footer = f"\nEND SOURCE {label}\n"
            remaining = MAX_TASK_PACKET_SOURCE_CHARS - used
            if remaining <= len(header) + len(footer):
                break
            block = header + body + footer
            if len(block) > remaining:
                continue
            blocks.append(block)
            used += len(block)
        if requested_windows and not (
            len(requested_windows) == 1
            and requested_windows[0][0] == 1
            and requested_windows[0][1] == max(1, len(lines))
        ):
            blocks.append(
                "...[requested source truncated deterministically]\n"
            )

    remaining = MAX_TASK_PACKET_SOURCE_CHARS - used
    if remaining > 0:
        traced_functions = _rank_traced_function_windows(
            files,
            relative,
            "\n".join(query_parts),
            executed_lines=runtime.get("executedSourceLines", {}),
        )
        for path, start, end, content in traced_functions:
            label = f"{path}:L{start}-L{end}"
            header = f"SOURCE {label}\n"
            footer = f"\nEND SOURCE {label}\n"
            block = header + content + footer
            if len(block) > remaining:
                continue
            blocks.append(block)
            used += len(block)
            remaining -= len(block)
            if remaining < 500:
                break
    if remaining > 0:
        ranked = _rank_production_windows(
            files,
            relative,
            "\n".join(query_parts),
            executed_lines=runtime.get("executedSourceLines", {}),
        )
        for path, start, end, content in ranked:
            label = f"{path}:L{start}-L{end}"
            header = f"SOURCE {label}\n"
            footer = f"\nEND SOURCE {label}\n"
            block = header + content + footer
            if len(block) > remaining:
                continue
            blocks.append(block)
            used += len(block)
            remaining -= len(block)
            if remaining < 500:
                break
    return tree or "(empty)", "".join(blocks) or "(none referenced)"


def _is_non_production_path(path: str) -> bool:
    lowered = path.lower()
    return (
        lowered.startswith(_NON_PRODUCTION_PREFIXES)
        or Path(lowered).name in _NON_PRODUCTION_NAMES
    )


def _numbered_source_lines(
    lines: Sequence[str],
    start: int,
    end: int,
) -> str:
    if not lines:
        return "00001 | "
    bounded_start = max(1, start)
    bounded_end = min(len(lines), max(bounded_start, end))
    return "\n".join(
        f"{line_number:05d} | {lines[line_number - 1]}"
        for line_number in range(bounded_start, bounded_end + 1)
    )


def _requested_source_windows(
    lines: Sequence[str],
    query: str,
    *,
    maximum_characters: int,
) -> list[tuple[int, int, str]]:
    normalized = list(lines) or [""]
    complete = _numbered_source_lines(normalized, 1, len(normalized))
    if len(complete) <= maximum_characters:
        return [(1, len(normalized), complete)]
    query_terms = _context_term_counts(query)
    priority_terms = {
        term for term in query_terms if term.startswith("test_")
    }
    candidates: list[tuple[float, int, int]] = []
    for offset in range(0, len(normalized), 50):
        end = min(len(normalized), offset + 100)
        window_text = "\n".join(normalized[offset:end])
        window_terms = _context_term_counts(window_text)
        score = sum(
            (
                min(query_terms.get(term, 0), 8)
                * min(count, 3)
                * (
                    12
                    if term.startswith("test_")
                    else 4
                    if "_" in term
                    else 1
                )
            )
            for term, count in window_terms.items()
        )
        score += 10_000 * len(priority_terms.intersection(window_terms))
        candidates.append((float(score), offset + 1, end))
        if end == len(normalized):
            break
    candidates.sort(key=lambda item: (-item[0], item[1]))
    selected: list[tuple[int, int, str]] = []
    used = 0
    for _score, start, end in candidates:
        if any(
            start <= prior_end and end >= prior_start
            for prior_start, prior_end, _body in selected
        ):
            continue
        body = "\n".join(
            f"{start + index:05d} | {line}"
            for index, line in enumerate(normalized[start - 1:end])
        )
        if selected and used + len(body) > maximum_characters:
            continue
        if not selected and len(body) > maximum_characters:
            body = body[:maximum_characters]
        selected.append((start, end, body))
        used += len(body)
        if used >= maximum_characters:
            break
    return sorted(selected, key=lambda item: item[0])


def _context_term_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for raw in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text):
        terms = [raw.lower(), *raw.lower().split("_")]
        for term in terms:
            if len(term) < 4 or term in _CONTEXT_STOP_WORDS:
                continue
            counts[term] = counts.get(term, 0) + 1
            singular = (
                term[:-2]
                if term.endswith("es") and len(term) > 5
                else term[:-1]
                if term.endswith("s") and len(term) > 4
                else term
            )
            if singular != term and singular not in _CONTEXT_STOP_WORDS:
                counts[singular] = counts.get(singular, 0) + 1
    return counts


def _rank_traced_function_windows(
    files: Sequence[Path],
    relative: Sequence[str],
    query: str,
    *,
    executed_lines: Any,
    maximum_windows: int = 8,
    maximum_lines: int = 80,
    context_before_lines: int = 12,
    context_after_lines: int = 100,
    maximum_context_lines: int = 180,
) -> list[tuple[str, int, int, str]]:
    """Select trace-ranked functions plus bounded neighboring source context."""
    if not isinstance(executed_lines, dict):
        return []
    query_terms = _context_term_counts(query)
    if not query_terms:
        return []
    by_relative = dict(zip(relative, files))
    candidates: list[
        tuple[float, str, int, int, list[str]]
    ] = []
    for path, raw_lines in executed_lines.items():
        child = by_relative.get(path)
        if (
            child is None
            or _is_non_production_path(path)
            or not isinstance(raw_lines, list)
        ):
            continue
        traced = {
            value
            for value in raw_lines
            if isinstance(value, int) and value > 0
        }
        if not traced:
            continue
        try:
            content = child.read_text(encoding="utf-8")
            source_lines = content.splitlines()
            tree = ast.parse(content)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                continue
            start = max(1, node.lineno)
            full_end = max(start, getattr(node, "end_lineno", start))
            if not any(start < value <= full_end for value in traced):
                continue
            end = min(full_end, start + maximum_lines - 1)
            window = source_lines[start - 1:end]
            name_terms = _context_term_counts(node.name)
            body_terms = _context_term_counts("\n".join(window))
            name_score = sum(
                12.0 * (1.0 + math.log1p(query_terms[term]))
                for term in name_terms
                if term in query_terms
            )
            body_score = sum(
                1.0 + math.log1p(query_terms[term])
                for term in body_terms
                if term in query_terms
            )
            if name_score + body_score <= 0:
                continue
            context_start = max(1, start - context_before_lines)
            context_end = min(
                len(source_lines),
                full_end + context_after_lines,
                context_start + maximum_context_lines - 1,
            )
            context_window = source_lines[
                context_start - 1:context_end
            ]
            candidates.append(
                (
                    name_score + body_score,
                    path,
                    context_start,
                    context_end,
                    context_window,
                )
            )
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    selected: list[tuple[str, int, int, str]] = []
    occupied: dict[str, list[tuple[int, int]]] = {}
    for _score, path, start, end, window in candidates:
        if any(
            start <= prior_end and end >= prior_start
            for prior_start, prior_end in occupied.get(path, [])
        ):
            continue
        occupied.setdefault(path, []).append((start, end))
        selected.append((
            path,
            start,
            end,
            "\n".join(
                f"{start + index:05d} | {line}"
                for index, line in enumerate(window)
            ),
        ))
        if len(selected) >= maximum_windows:
            break
    return selected


def _rank_production_windows(
    files: Sequence[Path],
    relative: Sequence[str],
    query: str,
    *,
    executed_lines: Any = None,
    window_lines: int = 60,
    stride_lines: int = 30,
    maximum_windows: int = 24,
) -> list[tuple[str, int, int, str]]:
    """Rank buggy-revision source windows by test/failure lexical evidence."""
    query_terms = _context_term_counts(query)
    if not query_terms:
        return []
    traced = executed_lines if isinstance(executed_lines, dict) else {}
    candidates: list[
        tuple[str, int, int, list[str], set[str]]
    ] = []
    for path, child in zip(relative, files):
        if _is_non_production_path(path):
            continue
        if Path(path).suffix not in {".py", ".pyi"}:
            continue
        try:
            lines = child.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        if not lines:
            continue
        for offset in range(0, len(lines), stride_lines):
            end_offset = min(len(lines), offset + window_lines)
            window = lines[offset:end_offset]
            terms = set(_context_term_counts("\n".join(window)))
            matching = terms.intersection(query_terms)
            raw_traced_lines = traced.get(path, [])
            has_trace = (
                isinstance(raw_traced_lines, list)
                and any(
                    isinstance(line_number, int)
                    and offset + 1 <= line_number <= end_offset
                    for line_number in raw_traced_lines
                )
            )
            if matching or has_trace:
                candidates.append(
                    (path, offset + 1, end_offset, window, matching)
                )
            if end_offset == len(lines):
                break
    if not candidates:
        return []
    document_frequency: dict[str, int] = {}
    for *_prefix, matching in candidates:
        for term in matching:
            document_frequency[term] = (
                document_frequency.get(term, 0) + 1
            )
    total = len(candidates)
    scored: list[
        tuple[float, str, int, int, list[str]]
    ] = []
    for path, start, end, window, matching in candidates:
        score = sum(
            (
                1.0
                + math.log1p(min(query_terms[term], 8))
                + (3.0 if "_" in term else 0.0)
            )
            * (
                1.0
                + math.log(
                    (total + 1) / (document_frequency[term] + 1)
                )
            )
            for term in matching
        )
        raw_traced_lines = traced.get(path, [])
        if isinstance(raw_traced_lines, list):
            executed_count = sum(
                1
                for line_number in raw_traced_lines
                if isinstance(line_number, int)
                and start <= line_number <= end
            )
            score += 10.0 * math.sqrt(executed_count)
        scored.append((score, path, start, end, window))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    best_by_path: dict[str, float] = {}
    matching_terms_by_path: dict[str, set[str]] = {}
    for path, _start, _end, _window, matching in candidates:
        matching_terms_by_path.setdefault(path, set()).update(matching)
    for score, path, _start, _end, _window in scored:
        best_by_path[path] = max(score, best_by_path.get(path, 0.0))
    query_lower = query.lower()
    eligible_paths: set[str] = set()
    for path in best_by_path:
        raw_traced_lines = traced.get(path, [])
        if isinstance(raw_traced_lines, list) and raw_traced_lines:
            best_by_path[path] += 15.0 * math.log1p(
                len(raw_traced_lines)
            )
            eligible_paths.add(path)
        normalized_path = path.lower()
        basename = Path(path).name.lower()
        stem = Path(path).stem.lower()
        path_is_referenced = (
            normalized_path in query_lower
            or basename in query_lower
            or (
                len(stem) >= 4
                and re.search(
                    rf"(?<![a-z0-9_]){re.escape(stem)}"
                    r"(?![a-z0-9_])",
                    query_lower,
                )
                is not None
            )
        )
        if path_is_referenced:
            best_by_path[path] += 30.0
            eligible_paths.add(path)
        if len(matching_terms_by_path.get(path, set())) >= 2:
            eligible_paths.add(path)
    selected_path_order = [
        path
        for path, _score in sorted(
            best_by_path.items(),
            key=lambda item: (-item[1], item[0]),
        )
        if path in eligible_paths
    ][:4]
    by_path: dict[
        str,
        list[tuple[float, str, int, int, list[str]]],
    ] = {path: [] for path in selected_path_order}
    for item in scored:
        if item[1] in by_path:
            by_path[item[1]].append(item)
    round_robin: list[
        tuple[float, str, int, int, list[str]]
    ] = []
    for rank in range(4):
        for path in selected_path_order:
            if rank < len(by_path[path]):
                round_robin.append(by_path[path][rank])
    scored = round_robin
    selected: list[tuple[str, int, int, str]] = []
    occupied: dict[str, list[tuple[int, int]]] = {}
    for _score, path, start, end, window in scored:
        if len(occupied.get(path, [])) >= 4:
            continue
        overlaps = any(
            start <= prior_end and end >= prior_start
            for prior_start, prior_end in occupied.get(path, [])
        )
        if overlaps:
            continue
        occupied.setdefault(path, []).append((start, end))
        selected.append(
            (
                path,
                start,
                end,
                "\n".join(
                    f"{start + index:05d} | {line}"
                    for index, line in enumerate(window)
                ),
            )
        )
        if len(selected) >= maximum_windows:
            break
    return selected


def frontier_alone_prompt(task_packet: str) -> str:
    return (
        "You are the frontier-alone baseline in a paired code-repair study.\n"
        "Solve the task directly in the current disposable Git repository. "
        "Inspect files and run tests as needed. Modify only paths below the "
        "approved write roots in the task packet. "
        "Do not commit. Do not access the network or any path outside this "
        "repository. Finish only when the working tree contains your final "
        "candidate patch.\n\n"
        f"{task_packet}"
    )


def frontier_alone_response_prompt(task_packet: str) -> str:
    """Prompt for the response-only frontier-alone arm (no file tools)."""
    return (
        "You are the frontier-alone baseline in a paired code-repair study.\n"
        "You have no terminal, file, or browser tools. You cannot inspect "
        "files or run tests. Diagnose the bug from the evidence below and "
        "return your repair as one strict edit-manifest-v1 JSON object.\n\n"
        "The edit-manifest-v1 schema is:\n"
        '{"edits": [{"path": "relative/path.py", "old": "exact text to '
        'replace", "new": "replacement text"}, ...]}\n\n'
        "Rules:\n"
        "- The top-level object must contain exactly one key: \"edits\".\n"
        "- Each edit must contain exactly path, old, and new (all strings).\n"
        "- Derive the change only from supplied SOURCE windows, not model "
        "memory or an assumed newer API.\n"
        "- Copy each old anchor from one supplied SOURCE window, remove only "
        "the five-digit line-number and ` | ` display prefixes, and verify "
        "that the resulting text is contiguous and unique in that file.\n"
        "- Use the executed-line map to locate the earliest branch that sends "
        "the failing input down the wrong path. Prefer a narrow edit at that "
        "branch over mutating upstream object state or a class-wide policy.\n"
        "- Use matching runtime-local samples to evaluate every new predicate "
        "on the failing call. Samples are observations from distinct calls; "
        "match their path, line, function, and identifying scalar values "
        "instead of treating all samples as one call.\n"
        "- Every new predicate must be supported by supplied source for both "
        "the failing input and a preserved control; do not assume an unseen "
        "runtime type or value.\n"
        "- A numeric runtime value does not prove membership in a named enum "
        "or set. Cite the supplied definition before relying on membership; "
        "otherwise prefer directly observed fields and existing predicates "
        "visible in the same SOURCE window.\n"
        "- Modify only paths below the approved write roots.\n"
        "- Do not modify tests, Git metadata, dependencies, or benchmark "
        "evidence.\n"
        "- Return only the JSON object. Do not wrap it in markdown code "
        "fences. Do not include explanations.\n\n"
        f"{task_packet}"
    )


def frontier_delegation_blueprint_prompt(
    task_packet: str,
    *,
    worker_capabilities: dict[str, Any],
) -> str:
    """Ask the frontier for the compact authority a small worker needs.

    The frontier performs diagnosis and exact-edit selection. The harness
    expands the accepted blueprint into the verbose Plan v2 contract so the
    model never has to echo source excerpts or mechanical gate boilerplate.
    """
    capability_json = json.dumps(
        worker_capabilities,
        ensure_ascii=False,
        sort_keys=True,
    )
    return (
        "You are the frontier commander in a paired code-repair study.\n"
        "You have exactly one planning response. There are no frontier calls "
        "between local worker waves.\n\n"
        "The local worker is deliberately small. Its frozen capability "
        f"contract is:\n{capability_json}\n\n"
        "You—not the local worker—must diagnose the failure, validate one "
        "falsifiable causal hypothesis against the supplied SOURCE windows, "
        "choose the narrowest supported change, and encode sealed line-range "
        "edits. The harness materializes those ranges into exact old/new "
        "anchors for the worker; the worker must not discover APIs, inspect "
        "missing source, choose a causal fix, or run commands.\n\n"
        "Return one compact strict JSON object with exactly this shape:\n"
        "{\n"
        '  "schemaVersion": 3,\n'
        '  "planId": "lowercase-id",\n'
        '  "objective": "exact objective from the task packet",\n'
        '  "diagnosis": {\n'
        '    "observedFailure": "string",\n'
        '    "causalHypothesis": "falsifiable string",\n'
        '    "validationEvidence": "source-trace explanation",\n'
        '    "falsificationCondition": "string",\n'
        '    "evidenceSources": ["exact SOURCE label"],\n'
        '    "candidateChange": "exact behavioral effect",\n'
        '    "failingPathPrediction": "string",\n'
        '    "preservedControlPrediction": "string",\n'
        '    "minimalityEvidence": "string",\n'
        '    "changeEvidenceSources": ["exact SOURCE label"]\n'
        "  },\n"
        '  "edits": [\n'
        '    {"path": "relative/path.py", '
        '"sourceLabel": "exact SOURCE label", '
        '"startLine": 1, "endLine": 1, '
        '"new": "replacement for those complete lines", '
        '"mustAdd": ["exact newly introduced text"], '
        '"mustRemove": ["exact removed text"]}\n'
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Return JSON only, without prose or a markdown fence.\n"
        "- Use exactly the listed keys; unknown fields are rejected.\n"
        "- evidenceSources must copy the text after `SOURCE ` exactly, without "
        "including the literal `SOURCE ` display prefix. Never invent a file, "
        "symbol, API, source excerpt, or test result.\n"
        "- Do not copy SOURCE contents into the diagnosis. Each edit must "
        "identify the smallest sufficient complete-line range inside its "
        "sourceLabel. The harness extracts the exact old text; never return "
        "an old field.\n"
        "- startLine and endLine are inclusive repository line numbers shown "
        "in the cited SOURCE block. path must match that SOURCE path. new is "
        "the complete replacement for those lines without display prefixes.\n"
        "- Each edit must declare mustAdd and mustRemove arrays. Every "
        "mustAdd item must be a non-whitespace exact substring present in "
        "new; at least one mustAdd item must be newly introduced unless a "
        "mustRemove item proves the change. Every mustRemove item must be an "
        "exact substring removed from the selected old range. Quote those "
        "literal changes in candidateChange so the structured edit "
        "demonstrably implements the stated intent.\n"
        "- Preserve the selected source indentation exactly unless indentation "
        "is the diagnosed cause. Mentally splice new into the complete file "
        "and reject your own candidate if it would not parse as Python.\n"
        "- Before emitting JSON, perform this grounding check in the same "
        "planning call: (1) locate the causal branch in a cited SOURCE "
        "window, (2) test the causal hypothesis against the failing path and "
        "one preserved control, and (3) re-read every edit range directly "
        "from that cited window. Do not use a symbol or API remembered from "
        "another revision.\n"
        "- Use the executed-line map to identify the earliest branch that "
        "sends the failing input down the wrong path. Prefer the narrowest "
        "local predicate or transformation at that branch; do not mutate "
        "upstream object state or a class-wide policy unless the supplied "
        "source proves a branch-local repair is impossible.\n"
        "- Runtime-local samples are bounded observations from distinct calls. "
        "Match path, line, function, and identifying scalar values before "
        "using a sample. Every new predicate must evaluate true for a supplied "
        "sample representing the failing call; do not guess an unreported "
        "attribute value.\n"
        "- In validationEvidence, explicitly evaluate every new predicate "
        "for the failing input and for one preserved control. If a required "
        "runtime type or value is not established by supplied evidence, the "
        "candidate is not validated and must not be selected.\n"
        "- A numeric runtime value does not prove membership in a named enum "
        "or set. Cite the supplied definition before relying on membership; "
        "otherwise prefer directly observed fields and existing predicates "
        "visible in the same SOURCE window.\n"
        "- Use path, never file, as the edit path key.\n"
        "- Keep the combined edit manifest short enough for the worker's "
        "frozen maximum generation budget.\n"
        "- Modify production code only and stay inside approved write roots.\n"
        "- If the evidence does not support a causal change, return no "
        "speculative substitute; an empty edits list will be rejected and "
        "the measurement will record planning failure.\n\n"
        f"{task_packet}"
    )


def parse_frontier_delegation_blueprint(
    response: str,
    *,
    objective: str,
    task_packet: str,
    repository: Path,
    approved_write_roots: Sequence[str],
    maximum_manifest_characters: int,
) -> dict[str, Any]:
    """Validate a compact frontier response before plan materialization."""
    payload = strip_one_json_fence(response)
    if len(payload.encode("utf-8")) > _MAX_FRONTIER_RESPONSE_BYTES:
        raise EvaluationError("Frontier delegation blueprint exceeds size limit.")
    try:
        blueprint = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise EvaluationError(
            f"Frontier delegation blueprint is not valid JSON: {exc}"
        ) from exc
    if not isinstance(blueprint, dict) or set(blueprint) != {
        "schemaVersion",
        "planId",
        "objective",
        "diagnosis",
        "edits",
    }:
        raise EvaluationError(
            "Frontier delegation blueprint has unknown or missing top-level "
            "fields."
        )
    if blueprint["schemaVersion"] != 3:
        raise EvaluationError(
            "Frontier delegation blueprint schemaVersion must be 3."
        )
    plan_id = blueprint["planId"]
    if not isinstance(plan_id, str) or not _IDENTIFIER.fullmatch(plan_id):
        raise EvaluationError(
            "Frontier delegation blueprint planId is invalid."
        )
    if blueprint["objective"] != objective:
        raise EvaluationError(
            "Frontier delegation blueprint objective differs from the "
            "frozen case objective."
        )
    diagnosis = blueprint["diagnosis"]
    diagnosis_keys = {
        "observedFailure",
        "causalHypothesis",
        "validationEvidence",
        "falsificationCondition",
        "evidenceSources",
        "candidateChange",
        "failingPathPrediction",
        "preservedControlPrediction",
        "minimalityEvidence",
        "changeEvidenceSources",
    }
    if not isinstance(diagnosis, dict) or set(diagnosis) != diagnosis_keys:
        raise EvaluationError(
            "Frontier delegation diagnosis has unknown or missing fields."
        )
    text_fields = diagnosis_keys - {
        "evidenceSources",
        "changeEvidenceSources",
    }
    for name in sorted(text_fields):
        value = diagnosis[name]
        if not isinstance(value, str) or not value.strip():
            raise EvaluationError(
                f"Frontier delegation diagnosis.{name} must be non-empty text."
            )
    known_sources = set(
        re.findall(r"(?m)^SOURCE ([^\n]+)$", task_packet)
    )
    for name in ("evidenceSources", "changeEvidenceSources"):
        values = diagnosis[name]
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(value, str) for value in values)
            or len(set(values)) != len(values)
        ):
            raise EvaluationError(
                f"Frontier delegation diagnosis.{name} must contain unique "
                "source labels."
            )
        normalized_values = [
            _normalize_evidence_source_label(value, known_sources)
            for value in values
        ]
        unknown = {
            value
            for value, normalized in zip(values, normalized_values)
            if normalized is None
        }
        if unknown:
            raise EvaluationError(
                f"Frontier delegation diagnosis.{name} references unknown "
                f"SOURCE labels: {', '.join(sorted(unknown))}"
            )
        diagnosis[name] = list(dict.fromkeys(
            value
            for value in normalized_values
            if value is not None
        ))
    edits = blueprint["edits"]
    if not isinstance(edits, list) or not (
        1 <= len(edits) <= MAX_FRONTIER_DELEGATION_EDITS
    ):
        raise EvaluationError(
            "Frontier delegation blueprint must contain 1 to "
            f"{MAX_FRONTIER_DELEGATION_EDITS} edits."
        )
    materialized_edits = _materialize_frontier_range_edits(
        edits,
        known_sources=known_sources,
        allowed_change_sources=set(diagnosis["changeEvidenceSources"]),
        repository=repository,
    )
    blueprint["edits"] = materialized_edits
    manifest = {"edits": materialized_edits}
    manifest_text = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(manifest_text) > maximum_manifest_characters:
        raise EvaluationError(
            "Frontier delegation edit manifest exceeds the frozen local "
            "worker generation budget."
        )
    materialize_frontier_edit_manifest(
        manifest_text,
        repository=repository,
        approved_write_roots=approved_write_roots,
    )
    return blueprint


def _materialize_frontier_range_edits(
    edits: list[Any],
    *,
    known_sources: set[str],
    allowed_change_sources: set[str],
    repository: Path,
) -> list[dict[str, str]]:
    """Turn sealed line-range edits into exact old/new worker manifests."""
    materialized: list[dict[str, str]] = []
    for index, raw in enumerate(edits):
        if not isinstance(raw, dict) or set(raw) != {
            "path",
            "sourceLabel",
            "startLine",
            "endLine",
            "new",
            "mustAdd",
            "mustRemove",
        }:
            raise EvaluationError(
                f"Frontier delegation edits[{index}] has unknown or missing "
                "fields."
            )
        path = raw["path"]
        source_label = raw["sourceLabel"]
        start = raw["startLine"]
        end = raw["endLine"]
        new = raw["new"]
        must_add = raw["mustAdd"]
        must_remove = raw["mustRemove"]
        if not isinstance(path, str) or not path:
            raise EvaluationError(
                f"Frontier delegation edits[{index}].path must be text."
            )
        if not isinstance(source_label, str):
            raise EvaluationError(
                f"Frontier delegation edits[{index}].sourceLabel must be text."
            )
        canonical_label = _normalize_evidence_source_label(
            source_label,
            known_sources,
        )
        if canonical_label is None:
            raise EvaluationError(
                f"Frontier delegation edits[{index}].sourceLabel is unknown."
            )
        if canonical_label not in allowed_change_sources:
            raise EvaluationError(
                f"Frontier delegation edits[{index}].sourceLabel is not cited "
                "by diagnosis.changeEvidenceSources."
            )
        label_match = re.fullmatch(
            r"(.+):L(\d+)-L(\d+)",
            canonical_label,
        )
        assert label_match is not None
        label_path, raw_label_start, raw_label_end = label_match.groups()
        if path != label_path:
            raise EvaluationError(
                f"Frontier delegation edits[{index}].path differs from its "
                "SOURCE label."
            )
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start <= 0
            or end < start
            or start < int(raw_label_start)
            or end > int(raw_label_end)
        ):
            raise EvaluationError(
                f"Frontier delegation edits[{index}] line range escapes its "
                "SOURCE label."
            )
        if not isinstance(new, str):
            raise EvaluationError(
                f"Frontier delegation edits[{index}].new must be text."
            )
        for field, values in (
            ("mustAdd", must_add),
            ("mustRemove", must_remove),
        ):
            if (
                not isinstance(values, list)
                or len(values) > 8
                or not all(
                    isinstance(value, str)
                    and bool(value.strip())
                    and len(value) <= 512
                    for value in values
                )
                or len(set(values)) != len(values)
            ):
                raise EvaluationError(
                    f"Frontier delegation edits[{index}].{field} must contain "
                    "up to 8 unique, non-whitespace text assertions."
                )
        if not must_add and not must_remove:
            raise EvaluationError(
                f"Frontier delegation edits[{index}] must declare at least "
                "one mustAdd or mustRemove assertion."
            )
        candidate = (repository / path).resolve()
        if (
            not _is_within(candidate, repository.resolve())
            or not candidate.is_file()
            or candidate.is_symlink()
        ):
            raise EvaluationError(
                f"Frontier delegation edits[{index}] source is unavailable."
            )
        try:
            lines = candidate.read_text(
                encoding="utf-8",
            ).splitlines(keepends=True)
        except (OSError, UnicodeDecodeError) as exc:
            raise EvaluationError(
                f"Frontier delegation edits[{index}] source is unreadable."
            ) from exc
        if end > len(lines):
            raise EvaluationError(
                f"Frontier delegation edits[{index}] line range exceeds its "
                "source."
            )
        old = "".join(lines[start - 1:end])
        if not old:
            raise EvaluationError(
                f"Frontier delegation edits[{index}] selects no source text."
            )
        if old.endswith("\n") and new and not new.endswith("\n"):
            new += "\n"
        for value in must_add:
            if value not in new:
                raise EvaluationError(
                    f"Frontier delegation edits[{index}].mustAdd assertion "
                    "is not present in new."
                )
        if (
            must_add
            and not any(value not in old for value in must_add)
            and not must_remove
        ):
            raise EvaluationError(
                f"Frontier delegation edits[{index}] must prove at least one "
                "newly introduced or removed text assertion."
            )
        for value in must_remove:
            if value not in old or value in new:
                raise EvaluationError(
                    f"Frontier delegation edits[{index}].mustRemove assertion "
                    "is not removed from the selected source."
                )
        materialized.append({
            "path": path,
            "old": old,
            "new": new,
        })
    return materialized


def _normalize_evidence_source_label(
    value: str,
    known_sources: set[str],
) -> str | None:
    """Canonicalize an exact label or a uniquely contained sealed subrange."""
    if value.startswith("SOURCE "):
        value = value.removeprefix("SOURCE ")
    if value in known_sources:
        return value
    match = re.fullmatch(r"(.+):L(\d+)-L(\d+)", value)
    if match is None:
        return None
    path, raw_start, raw_end = match.groups()
    start = int(raw_start)
    end = int(raw_end)
    if start <= 0 or end < start:
        return None
    parents: list[str] = []
    for candidate in known_sources:
        parent = re.fullmatch(r"(.+):L(\d+)-L(\d+)", candidate)
        if parent is None or parent.group(1) != path:
            continue
        parent_start = int(parent.group(2))
        parent_end = int(parent.group(3))
        if parent_start <= start and end <= parent_end:
            parents.append(candidate)
    return parents[0] if len(parents) == 1 else None


def materialize_frontier_delegation_plan(
    blueprint: dict[str, Any],
    *,
    task_packet: str,
    repository: Path,
    approved_write_roots: Sequence[str],
    max_repair: int,
    max_generation_tokens: int,
) -> dict[str, Any]:
    """Expand a validated compact blueprint into the strict Plan v2 contract."""
    diagnosis = blueprint["diagnosis"]
    evidence_labels = list(dict.fromkeys([
        *diagnosis["evidenceSources"],
        *diagnosis["changeEvidenceSources"],
    ]))
    authoritative_sources = [
        {
            "label": label,
            "content": _read_labeled_source(repository, label),
        }
        for label in evidence_labels
    ]
    for index, edit in enumerate(blueprint["edits"], start=1):
        authoritative_sources.append({
            "label": f"approved-edit-{index}-old:{edit['path']}",
            "content": edit["old"],
        })
    manifest_text = json.dumps(
        {"edits": blueprint["edits"]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    worker_prompt = (
        "Perform the frontier-approved exact edit delegation. Return exactly "
        "the following edit-manifest-v1 JSON object and nothing else. Do not "
        "add prose, markdown, keys, edits, or commands.\n\n"
        f"{manifest_text}"
    )
    return {
        "schemaVersion": 2,
        "planId": blueprint["planId"],
        "objective": blueprint["objective"],
        "context": {
            "objective": blueprint["objective"],
            "diagnosis": {
                "observedFailure": diagnosis["observedFailure"],
                "causalHypothesis": diagnosis["causalHypothesis"],
                "validationMethod": "source-trace",
                "validationEvidence": diagnosis["validationEvidence"],
                "falsificationCondition": diagnosis[
                    "falsificationCondition"
                ],
                "evidenceSources": diagnosis["evidenceSources"],
                "changeValidation": {
                    "candidateChange": diagnosis["candidateChange"],
                    "failingPathPrediction": diagnosis[
                        "failingPathPrediction"
                    ],
                    "preservedControlPrediction": diagnosis[
                        "preservedControlPrediction"
                    ],
                    "minimalityEvidence": diagnosis["minimalityEvidence"],
                    "evidenceSources": diagnosis[
                        "changeEvidenceSources"
                    ],
                },
            },
            "authoritativeSources": authoritative_sources,
            "constraints": [
                "Modify production code only.",
                "Apply only the frontier-approved exact edit manifest.",
                "Use only the configured verification profile.",
            ],
            "rejectionCriteria": [
                "Any path is outside the frozen approved write roots.",
                "Any old anchor is not unique at the frozen base revision.",
                "Worker output differs from edit-manifest-v1.",
            ],
            "outputProtocol": "edit-manifest-v1",
        },
        "tasks": [{
            "id": "apply-frontier-edits",
            "role": "implementation",
            "prompt": worker_prompt,
            "dependsOn": [],
            "artifactType": "patch",
            "workerOutputProtocol": "edit-manifest-v1",
            "allowedPaths": list(approved_write_roots),
            "verification": ["bugsinpy-acceptance"],
            "maxRepairAttempts": max(0, min(5, max_repair)),
            "outputProtocol": "edit-manifest-v1",
            "generationOverride": {
                "temperature": 0.0,
                "top_p": 1.0,
                "enable_thinking": False,
                "max_tokens": max_generation_tokens,
            },
            "gate": {
                "requiredPatterns": [],
                "forbiddenPatterns": [],
                "maxCharacters": max(20_000, len(manifest_text) * 2),
                "format": "json",
                "stripSingleCodeFence": True,
                "pythonSyntax": False,
                "jsonRequiredKeys": ["edits"],
                "jsonAllowedKeys": ["edits"],
                "jsonFieldEnums": {},
            },
        }],
    }


def _read_labeled_source(repository: Path, label: str) -> str:
    match = re.fullmatch(r"(.+):L([1-9][0-9]*)-L([1-9][0-9]*)", label)
    if match is None:
        raise EvaluationError(f"Invalid frozen SOURCE label: {label}")
    path_text, start_text, end_text = match.groups()
    relative = Path(path_text)
    if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
        raise EvaluationError(f"Unsafe frozen SOURCE label: {label}")
    candidate = (repository / relative).resolve()
    try:
        candidate.relative_to(repository.resolve())
    except ValueError as exc:
        raise EvaluationError(f"Frozen SOURCE label escapes repository: {label}") from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise EvaluationError(f"Frozen SOURCE label is unavailable: {label}")
    try:
        lines = candidate.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError) as exc:
        raise EvaluationError(
            f"Frozen SOURCE label is not readable UTF-8 text: {label}"
        ) from exc
    start = int(start_text)
    end = int(end_text)
    if start > end or end > len(lines):
        raise EvaluationError(f"Frozen SOURCE label range is invalid: {label}")
    return "\n".join(lines[start - 1:end])


def strip_one_json_fence(text: str) -> str:
    """Remove at most one outer JSON code fence from a model response."""
    stripped = text.strip()
    lines = stripped.splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip() in {"```", "```json", "```JSON"}
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip()
    return stripped


def materialize_frontier_edit_manifest(
    response: str,
    *,
    repository: Path,
    approved_write_roots: Sequence[str],
) -> str:
    """Convert a response-only edit-manifest into a unified Git diff.

    This mirrors the worker ``materialize_edit_manifest`` contract but
    operates directly on the frontier-alone arm repository without a
    workspace snapshot.  It enforces the same path-safety, uniqueness,
    and no-op checks.
    """
    from .workspace import (
        _path_within,
        _reject_symlink_components,
        _safe_patch_path,
    )

    payload = strip_one_json_fence(response)
    try:
        manifest = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise EvaluationError(
            f"Frontier edit manifest is not valid JSON: {exc}"
        ) from exc
    if not isinstance(manifest, dict) or set(manifest) != {"edits"}:
        raise EvaluationError(
            "Frontier edit manifest must contain exactly one top-level "
            "edits key."
        )
    edits = manifest["edits"]
    if not isinstance(edits, list) or not 1 <= len(edits) <= 64:
        raise EvaluationError(
            "Frontier edit manifest must contain 1 to 64 edits."
        )
    if len(payload.encode("utf-8")) > _MAX_FRONTIER_RESPONSE_BYTES:
        raise EvaluationError("Frontier edit manifest exceeds size limit.")
    originals: dict[str, str] = {}
    modified: dict[str, str] = {}
    order: list[str] = []
    for index, raw_edit in enumerate(edits):
        if not isinstance(raw_edit, dict) or set(raw_edit) != {
            "path",
            "old",
            "new",
        }:
            raise EvaluationError(
                f"Frontier edit {index + 1} must contain exactly path, "
                "old, and new."
            )
        path_value = raw_edit["path"]
        old = raw_edit["old"]
        new = raw_edit["new"]
        if not all(isinstance(value, str) for value in (path_value, old, new)):
            raise EvaluationError(
                f"Frontier edit {index + 1} path, old, and new must be "
                "strings."
            )
        try:
            path = _safe_patch_path(path_value)
        except WorkspaceError as exc:
            raise EvaluationError(str(exc)) from exc
        if not old:
            raise EvaluationError(
                f"Frontier edit {index + 1} old text must not be empty."
            )
        if old == new:
            raise EvaluationError(f"Frontier edit {index + 1} is a no-op.")
        if not any(_path_within(path, root) for root in approved_write_roots):
            raise EvaluationError(
                f"Frontier edit path is outside approved write roots: {path}"
            )
        candidate = repository / path
        try:
            _reject_symlink_components(repository, candidate, path)
        except WorkspaceError as exc:
            raise EvaluationError(str(exc)) from exc
        if not candidate.is_file():
            raise EvaluationError(
                f"Frontier edit path is not a regular file: {path}"
            )
        if path not in originals:
            try:
                content = candidate.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                raise EvaluationError(
                    f"Frontier edit path is not readable UTF-8 text: {path}"
                ) from exc
            if "\x00" in content:
                raise EvaluationError(
                    f"Frontier edit path contains binary data: {path}"
                )
            originals[path] = content
            modified[path] = content
            order.append(path)
        occurrences = modified[path].count(old)
        if occurrences != 1:
            raise EvaluationError(
                f"Frontier edit {index + 1} old text must match exactly "
                f"once in {path}; found {occurrences}."
            )
        modified[path] = modified[path].replace(old, new, 1)
    import difflib

    sections: list[str] = []
    for path in order:
        before = originals[path]
        after = modified[path]
        if before == after:
            continue
        if path.endswith(".py"):
            try:
                before_tree = ast.parse(before, filename=path)
            except SyntaxError:
                pass
            else:
                try:
                    after_tree = ast.parse(after, filename=path)
                except SyntaxError as exc:
                    location = (
                        f" at line {exc.lineno}"
                        if exc.lineno is not None
                        else ""
                    )
                    raise EvaluationError(
                        "Frontier edit manifest introduces invalid Python "
                        f"syntax in {path}{location}: {exc.msg}."
                    ) from exc
                _validate_python_bare_callables(
                    before_tree,
                    after_tree,
                    path=path,
                )
        unified = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                n=3,
                lineterm="\n",
            )
        )
        if not unified:
            raise EvaluationError(
                f"Could not materialize frontier edit diff: {path}"
            )
        sections.append(f"diff --git a/{path} b/{path}\n{unified}")
    if not sections:
        raise EvaluationError(
            "Frontier edit manifest produced no repository changes."
        )
    diff = "".join(sections)
    if not diff.endswith("\n"):
        diff += "\n"
    return diff


def _validate_python_bare_callables(
    before: ast.AST,
    after: ast.AST,
    *,
    path: str,
) -> None:
    """Reject new bare calls whose names have no evidence in the old file."""
    before_names = {
        node.id
        for node in ast.walk(before)
        if isinstance(node, ast.Name)
    }
    before_bound = _python_bound_names(before)
    after_bound = _python_bound_names(after)
    before_calls = {
        node.func.id
        for node in ast.walk(before)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
    }
    after_calls = {
        node.func.id
        for node in ast.walk(after)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
    }
    unresolved = sorted(
        (after_calls - before_calls)
        - before_names
        - before_bound
        - after_bound
        - set(dir(builtins))
    )
    if unresolved:
        raise EvaluationError(
            "Frontier edit manifest introduces unresolved bare callable"
            f"{'s' if len(unresolved) != 1 else ''} in {path}: "
            + ", ".join(unresolved)
            + "."
        )


def _python_bound_names(tree: ast.AST) -> set[str]:
    """Collect statically visible names without importing edited code."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    continue
                names.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(
            node.ctx,
            (ast.Store, ast.Param),
        ):
            names.add(node.id)
    return names


def validate_evaluation_plan(
    plan: Plan,
    repository: Path,
    approved_write_roots: Sequence[str],
) -> None:
    """Enforce the local arm's half of the paired fairness contract."""
    if not plan.workspace_execution:
        raise EvaluationError(
            "Evaluation plans must use schema-v2 workspace execution."
        )
    expected_roots = tuple(sorted(approved_write_roots))
    mutating = [task for task in plan.tasks if task.mutates_workspace]
    if not mutating:
        raise EvaluationError(
            "Evaluation plan must contain at least one mutating task."
        )
    for task in mutating:
        if tuple(sorted(task.allowed_paths)) != expected_roots:
            raise EvaluationError(
                f"Evaluation task {task.id} must use the exact paired-arm "
                "write roots; narrowing or widening path authority is not "
                "permitted."
            )
        if task.verification != ("bugsinpy-acceptance",):
            raise EvaluationError(
                f"Evaluation task {task.id} must use exactly the frozen "
                "bugsinpy-acceptance verification profile."
            )
        if task.worker_output_protocol != "edit-manifest-v1":
            raise EvaluationError(
                f"Evaluation task {task.id} must use edit-manifest-v1."
            )
        generation = task.generation_override
        if (
            generation.get("temperature") != 0
            or generation.get("top_p") != 1
            or generation.get("enable_thinking") is not False
            or not isinstance(generation.get("max_tokens"), int)
            or generation["max_tokens"] > 800
        ):
            raise EvaluationError(
                f"Evaluation task {task.id} must use the bounded "
                "deterministic edit-manifest generation settings."
            )

    if plan.context is None:
        raise EvaluationError(
            "Evaluation plans must provide authoritative worker context."
        )
    files = {
        child.relative_to(repository).as_posix(): child
        for child in repository.rglob("*")
        if (
            child.is_file()
            and not child.is_symlink()
            and ".git" not in child.relative_to(repository).parts
        )
    }
    verified_sources: set[str] = set()
    for source in plan.context.authoritative_sources:
        declared: list[str] = []
        prefix = "VERBATIM FILE:"
        if source.label.startswith(prefix):
            path = source.label[len(prefix):].strip()
            if path not in files:
                raise EvaluationError(
                    f"Authoritative source declares an unknown file: {path}"
                )
            declared = [path]
        else:
            declared = [
                path
                for path in files
                if path in source.label
            ]
        for path in declared:
            try:
                actual = files[path].read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                raise EvaluationError(
                    f"Authoritative source is not readable UTF-8: {path}"
                ) from exc
            if source.content not in actual:
                raise EvaluationError(
                    "Authoritative workspace source is not an exact "
                    f"contiguous excerpt: {path}"
                )
            verified_sources.add(path)

    if not verified_sources:
        raise EvaluationError(
            "Evaluation plan contains no verifiable workspace source excerpt."
        )
    for task in mutating:
        mentioned = {
            path for path in files if path in task.prompt
        }
        unverified = sorted(mentioned - verified_sources)
        if unverified:
            raise EvaluationError(
                f"Evaluation task {task.id} references source without an "
                "exact authoritative excerpt: "
                + ", ".join(unverified)
            )


def split_constraint_text(
    text: str,
    *,
    maximum: int = 4_000,
) -> list[str]:
    """Preserve a task packet in commander-sized deterministic chunks."""
    if maximum < 1:
        raise EvaluationError("Constraint chunk size must be positive.")
    return [
        text[index:index + maximum]
        for index in range(0, len(text), maximum)
    ]


def codex_command(
    profile: EvaluationProfile,
    *,
    cwd: Path,
    sandbox: str,
    output_last_message: Path,
) -> list[str]:
    return [
        profile.frontier.command,
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--model",
        profile.frontier.model,
        "--config",
        f'model_reasoning_effort="{profile.frontier.reasoning_effort}"',
        "--sandbox",
        sandbox,
        "--cd",
        str(cwd),
        "--output-last-message",
        str(output_last_message),
        "-",
    ]


def hermes_command(
    profile: EvaluationProfile,
    *,
    usage_file: Path,
    prompt_file: Path,
    request_timeout_seconds: int,
) -> list[str]:
    """Build one tool-free completion using the pinned Hermes environment.

    The configured ``hermes`` executable is inspected only to resolve its
    Python interpreter.  The packaged bridge then uses Hermes provider and
    credential resolution while bypassing the interactive agent loop.  The
    prompt stays in a file rather than process arguments, and the bridge makes
    exactly one OpenAI-compatible request with no tools.
    """
    if profile.frontier.adapter != "hermes-completion":
        raise EvaluationError(
            "hermes_command requires the hermes-completion adapter."
        )
    executable = shutil.which(profile.frontier.command)
    if executable is None:
        raise EvaluationError("Configured Hermes command is unavailable.")
    try:
        first_line = Path(executable).read_text(
            encoding="utf-8",
            errors="strict",
        ).splitlines()[0]
    except (OSError, UnicodeError, IndexError) as exc:
        raise EvaluationError(
            "Could not inspect the configured Hermes command."
        ) from exc
    if not first_line.startswith("#!"):
        raise EvaluationError(
            "Configured Hermes command has no pinned Python interpreter."
        )
    interpreter = Path(first_line[2:].strip())
    if (
        not interpreter.is_absolute()
        or not interpreter.is_file()
        or not os.access(interpreter, os.X_OK)
    ):
        raise EvaluationError(
            "Configured Hermes Python interpreter is unavailable."
        )
    bridge = Path(__file__).with_name("hermes_completion.py")
    if not bridge.is_file():
        raise EvaluationError("Packaged Hermes completion bridge is missing.")
    argv: list[str] = [
        str(interpreter),
        str(bridge),
        "--provider",
        profile.frontier.provider,
        "--model",
        profile.frontier.model,
        "--prompt-file",
        str(prompt_file),
        "--usage-file",
        str(usage_file),
        "--max-completion-tokens",
        str(profile.frontier.max_completion_tokens),
        "--reasoning-effort",
        profile.frontier.reasoning_effort,
        "--request-timeout-seconds",
        str(request_timeout_seconds),
    ]
    return argv


def frontier_command(
    profile: EvaluationProfile,
    *,
    cwd: Path,
    sandbox: str,
    output_last_message: Path,
    usage_file: Path | None = None,
    prompt_file: Path | None = None,
    request_timeout_seconds: int | None = None,
) -> list[str]:
    """Dispatch to the correct adapter command builder."""
    if profile.frontier.adapter == "codex-cli":
        return codex_command(
            profile,
            cwd=cwd,
            sandbox=sandbox,
            output_last_message=output_last_message,
        )
    if profile.frontier.adapter == "hermes-completion":
        if (
            usage_file is None
            or prompt_file is None
            or request_timeout_seconds is None
        ):
            raise EvaluationError(
                "hermes-completion adapter requires usage_file, prompt_file, "
                "and request_timeout_seconds."
            )
        return hermes_command(
            profile,
            usage_file=usage_file,
            prompt_file=prompt_file,
            request_timeout_seconds=request_timeout_seconds,
        )
    raise EvaluationError(
        f"Unsupported frontier adapter: {profile.frontier.adapter}"
    )


def frontier_environment(
    case_environment: Path | None = None,
) -> dict[str, str]:
    allowed = (
        "CODEX_HOME",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_FILE",
        "TMPDIR",
    )
    values = {
        name: os.environ[name]
        for name in allowed
        if name in os.environ
    }
    if case_environment is not None:
        original_path = values.get("PATH", os.defpath)
        values["PATH"] = f"{case_environment / 'bin'}:{original_path}"
        values["VIRTUAL_ENV"] = str(case_environment)
    return values


def usage_with_phases(
    phases: Sequence[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    reported = all(
        usage.get("usageStatus") == "reported"
        for _, usage in phases
    )
    fields = (
        "promptTokens",
        "cachedInputTokens",
        "completionTokens",
        "reasoningTokens",
        "totalTokens",
    )
    return {
        "usageStatus": "reported" if reported else "unavailable",
        "turns": sum(int(usage.get("turns", 0)) for _, usage in phases),
        **{
            field: (
                sum(int(usage[field]) for _, usage in phases)
                if reported
                else None
            )
            for field in fields
        },
        "phases": [
            {"phase": name, **usage}
            for name, usage in phases
        ],
    }


def empty_local_usage() -> dict[str, int]:
    return {
        "promptTokens": 0,
        "generationTokens": 0,
        "generationCalls": 0,
        "modelLoads": 0,
    }


def make_arm_result(
    *,
    case: dict[str, Any],
    arm: str,
    status: str,
    completed: bool,
    score: int,
    elapsed_seconds: float,
    phase_seconds: dict[str, float],
    frontier_usage: dict[str, Any],
    local_usage: dict[str, int],
    repairs: int,
    model_loads: int,
    review_verdict: str | None,
    patch: dict[str, Any],
    oracle: dict[str, Any],
) -> dict[str, Any]:
    result = {
        "schemaVersion": RESULT_SCHEMA_VERSION,
        "caseId": case["caseId"],
        "phase": case["phase"],
        "arm": arm,
        "status": status,
        "completed": completed,
        "score": score,
        "elapsedSeconds": elapsed_seconds,
        "phaseSeconds": phase_seconds,
        "frontierUsage": frontier_usage,
        "localUsage": local_usage,
        "repairs": repairs,
        "modelLoads": model_loads,
        "reviewVerdict": review_verdict,
        "patch": patch,
        "oracle": {
            "passed": bool(oracle["passed"]),
            "exitCode": oracle.get("exitCode"),
            "evidence": str(oracle.get("evidence", ""))[:MAX_LOG_BYTES],
        },
        "recordedAt": utc_now(),
    }
    return validate_arm_result(result)


def invalid_arm_result(
    case: dict[str, Any],
    arm: str,
    evidence: str,
) -> dict[str, Any]:
    return make_arm_result(
        case=case,
        arm=arm,
        status="invalid",
        completed=False,
        score=0,
        elapsed_seconds=0.0,
        phase_seconds={},
        frontier_usage=usage_with_phases([
            (
                "unavailable",
                {
                    "usageStatus": "unavailable",
                    "turns": 0,
                    "promptTokens": None,
                    "cachedInputTokens": None,
                    "completionTokens": None,
                    "reasoningTokens": None,
                    "totalTokens": None,
                    "malformedLines": 0,
                },
            )
        ]),
        local_usage=empty_local_usage(),
        repairs=0,
        model_loads=0,
        review_verdict=None,
        patch={"sha256": None, "changedFiles": 0},
        oracle={"passed": False, "exitCode": None, "evidence": evidence},
    )


def persist_candidate_patch(
    evidence_root: Path,
    diff: str,
) -> dict[str, Any]:
    if not diff:
        return {"sha256": None, "changedFiles": 0}
    evidence_root.mkdir(parents=True, exist_ok=True)
    path = evidence_root / "candidate.diff"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != diff:
            raise EvaluationError("Candidate patch evidence is immutable.")
    else:
        path.write_text(diff, encoding="utf-8")
    metadata = patch_metadata(diff)
    return {
        "sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "changedFiles": metadata["changedFiles"],
    }


def validate_candidate_diff(
    diff: str,
    case: dict[str, Any],
    repository: Path,
    *,
    allowed_paths: Sequence[str] | None = None,
) -> str | None:
    if not diff:
        return "Candidate patch is empty."
    try:
        metadata = patch_metadata(diff)
    except EvaluationError as exc:
        return str(exc)
    if any(_is_non_production_path(path) for path in metadata["paths"]):
        return "Candidate patch modifies tests or non-production evidence."
    approved = tuple(
        allowed_paths
        if allowed_paths is not None
        else evaluation_write_roots(repository)
    )
    outside = [
        path
        for path in metadata["paths"]
        if not any(
            _evaluation_path_within(path, root)
            for root in approved
        )
    ]
    if outside:
        return (
            "Candidate patch is outside the paired-arm approved write roots: "
            + ", ".join(outside)
        )
    if metadata["changedFiles"] > 20:
        return "Candidate patch changes more than 20 files."
    result = run_command(
        ["git", "apply", "--check", "--recount", "-"],
        cwd=repository,
        timeout=60,
        input_text=diff,
    )
    if result.returncode != 0:
        # The diff was produced against this already-modified repository. Test
        # it against HEAD by reversing first; independent scoring performs the
        # authoritative clean-apply check.
        reverse = run_command(
            ["git", "apply", "--reverse", "--check", "--recount", "-"],
            cwd=repository,
            timeout=60,
            input_text=diff,
        )
        if reverse.returncode != 0:
            return "Candidate patch is not a valid unified diff."
    del case
    return None


def write_evaluation_config(
    source: SwarmConfig,
    path: Path,
    artifacts_root: Path,
    verifier_manifest: Path,
    repository: Path,
    *,
    write_roots: Sequence[str] | None = None,
) -> None:
    docker_environment = docker_connection_environment(path.parent)
    docker_environment["PYTHONPATH"] = str(
        Path(__file__).resolve().parents[1]
    )
    model: dict[str, Any] = {"repository": source.model.repository}
    if source.model.revision:
        model["revision"] = source.model.revision
    if source.model.local_path:
        model["localPath"] = source.model.local_path
    payload = {
        "schemaVersion": 2,
        "model": model,
        "batch": {
            "maxWorkers": source.batch.max_workers,
            "prefillStepSize": source.batch.prefill_step_size,
            "maxPromptCharacters": source.batch.max_prompt_characters,
        },
        "artifacts": str(artifacts_root.resolve()),
        "enableThinking": source.enable_thinking,
        "seed": source.seed,
        "worker": {
            "mode": source.worker.mode,
            "reasoningMaxTokens": source.worker.reasoning_max_tokens,
            "capabilities": worker_capabilities_payload(
                source.worker.capabilities
            ),
        },
        "workspace": {
            "writeRoots": list(
                write_roots
                if write_roots is not None
                else evaluation_write_roots(repository)
            ),
            "verificationProfiles": {
                "bugsinpy-acceptance": {
                    "argv": [
                        sys.executable,
                        "-m",
                        "mlx_swarm.evaluation",
                        "verify-case",
                        str(verifier_manifest.resolve()),
                    ],
                    "cwd": ".",
                    "timeoutSeconds": 1_800,
                    "inheritEnv": [
                        "PATH",
                        "TMPDIR",
                        "LANG",
                        "LC_ALL",
                    ],
                    # Verification runs with a deliberately isolated HOME.
                    # Resolve the trusted operator's Docker context now so a
                    # nested pinned verifier does not silently fall back to
                    # /var/run/docker.sock.
                    "environment": docker_environment,
                }
            },
        },
    }
    _atomic_json(path, payload)


def docker_connection_environment(cwd: Path) -> dict[str, str]:
    """Freeze the active Docker endpoint for sanitized verifier subprocesses."""
    result: dict[str, str] = {}
    for name in ("DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        value = os.environ.get(name)
        if value:
            result[name] = value
    if "DOCKER_HOST" in result:
        return result

    inspected = run_command(
        ["docker", "context", "inspect"],
        cwd=cwd,
        timeout=30,
    )
    if inspected.timed_out or inspected.returncode != 0:
        raise EvaluationError(
            "Active Docker context is unavailable: "
            + (inspected.stderr or inspected.stdout).strip()
        )
    try:
        contexts = json.loads(inspected.stdout)
        endpoint = contexts[0]["Endpoints"]["docker"]["Host"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise EvaluationError(
            "Active Docker context did not report a usable endpoint."
        ) from exc
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise EvaluationError(
            "Active Docker context did not report a usable endpoint."
        )
    result["DOCKER_HOST"] = endpoint.strip()
    return result


def _evaluation_path_within(path: str, root: str) -> bool:
    path_parts = Path(path).parts
    root_parts = Path(root).parts
    return (
        len(path_parts) >= len(root_parts)
        and path_parts[:len(root_parts)] == root_parts
    )


def evaluation_write_roots(repository: Path) -> list[str]:
    roots: list[str] = []
    for child in sorted(repository.iterdir(), key=lambda value: value.name):
        name = child.name
        if (
            name == ".git"
            or name.startswith(".")
            or _is_non_production_path(name)
            or child.is_symlink()
        ):
            continue
        if child.is_dir() and name.lower() in {
            "benchmark",
            "benchmarks",
            "doc",
            "docs",
            "example",
            "examples",
            "test",
            "tests",
            "testing",
        }:
            continue
        roots.append(name)
    if not roots:
        raise EvaluationError(
            "Benchmark case exposes no production write roots."
        )
    return roots


def fresh_arm_repository(base: Path, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    validate_repository_symlinks(base)
    shutil.copytree(base, destination, symlinks=True)
    # A copied Git index retains the source checkout's inode/stat cache. Git
    # can therefore reject an otherwise exact clean-base patch with "does not
    # match index" until the destination index is refreshed.
    _run_checked(
        ["git", "update-index", "--refresh"],
        cwd=destination,
        timeout=60,
    )
    return destination


def copy_fixed_test_support(
    fixed_checkout: Path,
    buggy_checkout: Path,
    test_files: Sequence[str],
) -> None:
    """Copy fixed tests and their bounded Python import closure.

    The executable oracle must not combine a fixed test module with helper
    modules from the buggy revision.  At the same time, copying the entire
    fixed test tree could expose unrelated future tests.  Follow only imports
    that resolve below the selected test package and copy those modules,
    overwriting buggy counterparts when necessary.
    """
    copied: set[Path] = set()
    copied_bytes = 0
    queue = [Path(value) for value in test_files]
    allowed_roots: set[Path] = set()
    for relative in queue:
        parts = relative.parent.parts
        for index, part in enumerate(parts):
            if part.lower() in {"test", "tests", "testing"}:
                allowed_roots.add(Path(*parts[:index + 1]))
                break
    while queue:
        relative = queue.pop(0)
        if relative in copied:
            continue
        source = fixed_checkout / relative
        if not source.is_file():
            raise EvaluationError(
                f"Fixed test support file is missing: {relative}"
            )
        copied_bytes += source.stat().st_size
        if len(copied) >= 512 or copied_bytes > 10_000_000:
            raise EvaluationError(
                "Fixed test support exceeds the preparation limit."
            )
        target = buggy_checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.add(relative)
        if source.suffix != ".py":
            continue
        queue.extend(
            _fixed_test_imports(
                fixed_checkout,
                relative,
                allowed_roots,
            )
        )
        for parent in relative.parents:
            if parent == Path("."):
                break
            for support in (
                parent / "__init__.py",
                parent / "conftest.py",
            ):
                if (
                    fixed_checkout.joinpath(support).is_file()
                    and support not in copied
                    and any(
                        support == root or root in support.parents
                        for root in allowed_roots
                    )
                ):
                    queue.append(support)


def _fixed_test_imports(
    fixed_checkout: Path,
    relative: Path,
    allowed_roots: set[Path],
) -> list[Path]:
    try:
        tree = ast.parse(
            (fixed_checkout / relative).read_text(encoding="utf-8")
        )
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise EvaluationError(
            f"Cannot inspect fixed test support imports: {relative}"
        ) from exc
    candidates: set[Path] = set()
    for node in ast.walk(tree):
        modules: list[tuple[int, str]] = []
        if isinstance(node, ast.ImportFrom):
            if node.module:
                modules.append((node.level, node.module))
                modules.extend(
                    (
                        node.level,
                        f"{node.module}.{alias.name}",
                    )
                    for alias in node.names
                    if alias.name != "*"
                )
            elif node.level:
                modules.extend(
                    (node.level, alias.name)
                    for alias in node.names
                    if alias.name != "*"
                )
        elif isinstance(node, ast.Import):
            modules.extend((0, alias.name) for alias in node.names)
        for level, module in modules:
            if level:
                base = relative.parent
                for _ in range(level - 1):
                    base = base.parent
                module_path = base.joinpath(*module.split("."))
            else:
                module_path = Path(*module.split("."))
            for candidate in (
                module_path.with_suffix(".py"),
                module_path / "__init__.py",
            ):
                if not (fixed_checkout / candidate).is_file():
                    continue
                if not any(
                    candidate == root or root in candidate.parents
                    for root in allowed_roots
                ):
                    continue
                candidates.add(candidate)
    return sorted(candidates)


def validate_repository_symlinks(repository: Path) -> None:
    root = repository.resolve()
    for path in repository.rglob("*"):
        if not path.is_symlink():
            continue
        target_text = os.readlink(path)
        target = Path(target_text)
        if target.is_absolute():
            raise EvaluationError(
                f"Benchmark repository contains an absolute symlink: {path}"
            )
        resolved = (path.parent / target).resolve(strict=False)
        if not _is_within(resolved, root):
            raise EvaluationError(
                f"Benchmark repository symlink escapes its root: {path}"
            )


def _init_snapshot_repository(path: Path) -> None:
    git_path = path / ".git"
    if git_path.exists():
        if git_path.is_dir():
            shutil.rmtree(git_path)
        else:
            git_path.unlink()
    _run_checked(["git", "init", "-q"], cwd=path, timeout=30)
    _run_checked(
        ["git", "config", "user.name", "MLX Swarm Evaluation"],
        cwd=path,
        timeout=30,
    )
    _run_checked(
        ["git", "config", "user.email", "evaluation@localhost"],
        cwd=path,
        timeout=30,
    )
    _run_checked(["git", "add", "-A"], cwd=path, timeout=60)
    _run_checked(
        ["git", "commit", "-q", "--no-verify", "-m", "frozen buggy snapshot"],
        cwd=path,
        timeout=120,
    )


def _remove_generated_state(path: Path) -> None:
    for name in (
        ".coverage",
        ".eggs",
        ".hypothesis",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
    ):
        for candidate in path.rglob(name):
            if candidate.is_dir() and not candidate.is_symlink():
                shutil.rmtree(candidate, ignore_errors=True)
            elif candidate.exists():
                candidate.unlink(missing_ok=True)
    for candidate in path.rglob("*.pyc"):
        candidate.unlink(missing_ok=True)
    for candidate in path.rglob("*.egg-info"):
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate, ignore_errors=True)
        elif candidate.exists():
            candidate.unlink(missing_ok=True)


def _git_worktree_add(mirror: Path, destination: Path, commit: str) -> None:
    _run_checked(
        [
            "git",
            "--git-dir",
            str(mirror),
            "worktree",
            "add",
            "--detach",
            str(destination),
            commit,
        ],
        cwd=mirror.parent,
        timeout=300,
    )


def _git_worktree_remove(mirror: Path, destination: Path) -> None:
    if not destination.exists():
        return
    run_command(
        [
            "git",
            "--git-dir",
            str(mirror),
            "worktree",
            "remove",
            "--force",
            str(destination),
        ],
        cwd=mirror.parent,
        timeout=120,
    )


def _git_text(cwd: Path, argv: Sequence[str]) -> str:
    return _run_checked(["git", *argv], cwd=cwd, timeout=60).stdout.strip()


def _git_diff(repository: Path, base_sha: str) -> str:
    return _run_checked(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--binary",
            base_sha,
            "--",
        ],
        cwd=repository,
        timeout=60,
    ).stdout


def _venv_python(environment: Path) -> Path:
    candidate = environment / "bin" / "python"
    if not candidate.is_file():
        raise EvaluationError("Benchmark virtual environment has no Python.")
    return candidate


def validate_suite(
    value: Any,
    profile: EvaluationProfile | None = None,
) -> dict[str, Any]:
    suite = _object(value, "suite")
    _exact_keys(
        suite,
        "suite",
        {
            "schemaVersion",
            "suiteId",
            "profileId",
            "benchmark",
            "seed",
            "createdAt",
            "cases",
        },
    )
    if _integer(suite["schemaVersion"], "suite.schemaVersion", 1, 100) != 1:
        raise EvaluationError("Unsupported suite schema version.")
    _identifier(suite["suiteId"], "suite.suiteId")
    _identifier(suite["profileId"], "suite.profileId")
    _integer(suite["seed"], "suite.seed", 0, 2**31 - 1)
    _text(suite["createdAt"], "suite.createdAt")
    benchmark = _object(suite["benchmark"], "suite.benchmark")
    _exact_keys(
        benchmark,
        "suite.benchmark",
        {"name", "repository", "revision"},
    )
    _text(benchmark["name"], "suite.benchmark.name")
    _text(benchmark["repository"], "suite.benchmark.repository")
    if _GIT_SHA.fullmatch(
        _text(benchmark["revision"], "suite.benchmark.revision")
    ) is None:
        raise EvaluationError("suite benchmark revision is not pinned.")
    cases = _list(suite["cases"], "suite.cases", minimum=1, maximum=1_000)
    ids: set[str] = set()
    phase_counts = {"pilot": 0, "measured": 0}
    project_counts: dict[str, int] = {}
    for index, raw_case in enumerate(cases):
        case = _object(raw_case, f"suite.cases[{index}]")
        _exact_keys(
            case,
            f"suite.cases[{index}]",
            {
                "caseId",
                "project",
                "bugId",
                "repository",
                "buggyCommit",
                "fixedCommit",
                "pythonVersion",
                "testFiles",
                "setupArgv",
                "verificationArgv",
                "requirements",
                "reference",
                "phase",
                "objective",
            },
        )
        case_id = _identifier(case["caseId"], f"suite.cases[{index}].caseId")
        if case_id in ids:
            raise EvaluationError(f"Duplicate suite case: {case_id}")
        ids.add(case_id)
        _text(case["project"], f"suite.cases[{index}].project")
        _integer(case["bugId"], f"suite.cases[{index}].bugId", 1, 10**9)
        _text(case["repository"], f"suite.cases[{index}].repository")
        _commit(case["buggyCommit"], "suite case buggyCommit")
        _commit(case["fixedCommit"], "suite case fixedCommit")
        _python_version(case["pythonVersion"])
        _safe_relative_paths(
            _unique_text_array(
                case["testFiles"],
                f"suite.cases[{index}].testFiles",
                minimum=1,
                maximum=100,
            ),
            f"suite.cases[{index}].testFiles",
        )
        for command_field in ("setupArgv", "verificationArgv"):
            commands = _list(
                case[command_field],
                f"suite.cases[{index}].{command_field}",
                minimum=0 if command_field == "setupArgv" else 1,
                maximum=100,
            )
            for command_index, raw_command in enumerate(commands):
                command = _unique_or_repeated_text_array(
                    raw_command,
                    (
                        f"suite.cases[{index}].{command_field}"
                        f"[{command_index}]"
                    ),
                    minimum=1,
                    maximum=128,
                )
                if command[0] not in _SAFE_BENCHMARK_COMMANDS:
                    raise EvaluationError("Suite contains unsafe command.")
        _unique_or_repeated_text_array(
            case["requirements"],
            f"suite.cases[{index}].requirements",
            maximum=2_000,
        )
        reference = _object(
            case["reference"],
            f"suite.cases[{index}].reference",
        )
        _exact_keys(
            reference,
            f"suite.cases[{index}].reference",
            {"paths", "changedFiles", "changedLines", "sha256", "stratum"},
        )
        paths = _unique_text_array(
            reference["paths"],
            f"suite.cases[{index}].reference.paths",
            minimum=1,
            maximum=100,
        )
        _safe_relative_paths(paths, "reference paths")
        _integer(
            reference["changedFiles"],
            "reference.changedFiles",
            1,
            100,
        )
        _integer(
            reference["changedLines"],
            "reference.changedLines",
            1,
            100_000,
        )
        if _SHA256.fullmatch(
            _text(reference["sha256"], "reference.sha256")
        ) is None:
            raise EvaluationError("Reference digest is not SHA-256.")
        _enum(
            reference["stratum"],
            "reference.stratum",
            {"small", "medium", "large"},
        )
        phase = _enum(
            case["phase"],
            f"suite.cases[{index}].phase",
            {"pilot", "measured"},
        )
        phase_counts[phase] += 1
        if phase == "measured":
            project = case["project"]
            project_counts[project] = project_counts.get(project, 0) + 1
        _text(case["objective"], f"suite.cases[{index}].objective")
    if profile is not None:
        if phase_counts["pilot"] != profile.selection.pilot_size:
            raise EvaluationError("Suite pilot size differs from profile.")
        if phase_counts["measured"] != profile.selection.measured_size:
            raise EvaluationError("Suite measured size differs from profile.")
        if len(project_counts) < profile.selection.min_projects:
            raise EvaluationError("Suite covers too few measured projects.")
        if any(
            count > profile.selection.max_per_project
            for count in project_counts.values()
        ):
            raise EvaluationError("Suite exceeds measured project ceiling.")
    return suite


def clone_benchmark_metadata(
    profile: EvaluationProfile,
    destination: Path,
) -> Path:
    if destination.exists():
        raise EvaluationError("Benchmark destination already exists.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run_checked([
        "git",
        "clone",
        "--filter=blob:none",
        "--no-checkout",
        profile.benchmark_repository,
        str(destination),
    ], cwd=destination.parent, timeout=600)
    _run_checked([
        "git",
        "checkout",
        "--detach",
        profile.benchmark_revision,
    ], cwd=destination, timeout=300)
    actual = _run_checked(
        ["git", "rev-parse", "HEAD"],
        cwd=destination,
        timeout=30,
    ).stdout.strip()
    if actual != profile.benchmark_revision:
        raise EvaluationError("Benchmark checkout does not match pinned revision.")
    return destination


def resolve_case_commits(
    cases: Sequence[dict[str, Any]],
    repositories_root: Path,
) -> None:
    """Resolve BugsInPy's abbreviated commits before freezing the suite."""
    repositories_root.mkdir(parents=True, exist_ok=True)
    by_project: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        by_project.setdefault(case["project"], []).append(case)
    for project, project_cases in sorted(by_project.items()):
        if all(
            len(case[field]) == 40
            for case in project_cases
            for field in ("buggyCommit", "fixedCommit")
        ):
            continue
        mirror = repositories_root / f"{project}.git"
        if not mirror.is_dir():
            _run_checked(
                [
                    "git",
                    "clone",
                    "--mirror",
                    project_cases[0]["repository"],
                    str(mirror),
                ],
                cwd=repositories_root,
                timeout=900,
            )
        for case in project_cases:
            for field in ("buggyCommit", "fixedCommit"):
                commitish = _commitish(case[field], field)
                result = _run_checked(
                    [
                        "git",
                        "--git-dir",
                        str(mirror),
                        "rev-parse",
                        f"{commitish}^{{commit}}",
                    ],
                    cwd=repositories_root,
                    timeout=30,
                )
                resolved = result.stdout.strip()
                case[field] = _commit(resolved, field)


def preparation_summary(
    evaluation_dir: Path,
    suite: dict[str, Any],
) -> dict[str, Any]:
    phases: dict[str, list[float]] = {"pilot": [], "measured": []}
    for case in suite["cases"]:
        runtime_path = (
            evaluation_dir / "cases" / case["caseId"] / "runtime.json"
        )
        if not runtime_path.is_file():
            continue
        runtime = _read_json(runtime_path)
        seconds = _number(
            runtime.get("preparationSeconds"),
            "runtime.preparationSeconds",
            0,
            10**9,
        )
        phases[case["phase"]].append(seconds)
    values = phases["pilot"] + phases["measured"]
    return {
        "caseCount": len(values),
        "totalSeconds": sum(values),
        "medianSeconds": statistics.median(values) if values else None,
        "pilotSeconds": sum(phases["pilot"]),
        "measuredSeconds": sum(phases["measured"]),
    }


def study_context(
    suite: dict[str, Any],
    environment: dict[str, Any],
) -> dict[str, Any]:
    return {
        "benchmark": suite["benchmark"]["name"],
        "benchmarkRevision": suite["benchmark"]["revision"],
        "seed": suite["seed"],
        "recordedAt": environment.get("recordedAt"),
        "frontierAdapter": environment.get("frontierAdapter"),
        "frontierProvider": environment.get("frontierProvider"),
        "frontierModel": environment.get("frontierModel"),
        "frontierCommandVersion": environment.get(
            "frontierCommandVersion"
        ),
        "frontierToolsets": environment.get("frontierToolsets"),
        "frontierContextWindow": environment.get("frontierContextWindow"),
        "frontierMaxCompletionTokens": environment.get(
            "frontierMaxCompletionTokens"
        ),
        "reasoningEffort": environment.get("reasoningEffort"),
        "codexVersion": environment.get("codexVersion"),
        "mlxSwarmCommit": environment.get("mlxSwarmCommit"),
        "evaluationProtocolVersion": environment.get(
            "evaluationProtocolVersion"
        ),
        "localModel": environment.get("localModel"),
        "hardware": environment.get("hardware"),
        "runtime": environment.get("runtime"),
    }


def inspect_container(profile: EvaluationProfile) -> dict[str, Any]:
    return inspect_container_contract(
        profile.container.image,
        profile.container.digest,
        profile.container.platform,
        cwd=profile.source.parent,
    )


def inspect_container_contract(
    image: str,
    digest: str,
    platform_name: str,
    *,
    cwd: Path,
) -> dict[str, Any]:
    result = run_command(
        ["docker", "image", "inspect", image],
        cwd=cwd,
        timeout=30,
    )
    if result.timed_out or result.returncode != 0:
        raise EvaluationError(
            "Pinned benchmark container is unavailable: "
            + (result.stderr or result.stdout).strip()
        )
    try:
        values = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise EvaluationError(
            "Docker returned malformed image metadata."
        ) from exc
    if not isinstance(values, list) or len(values) != 1:
        raise EvaluationError("Docker returned ambiguous image metadata.")
    value = _object(values[0], "container image metadata")
    actual = value.get("Id")
    if actual != digest:
        raise EvaluationError(
            "Benchmark container digest mismatch: "
            f"expected {digest}, got {actual}."
        )
    size = value.get("Size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise EvaluationError("Docker image size is unavailable.")
    inspected = {
        "image": image,
        "digest": actual,
        "architecture": _text(
            value.get("Architecture"),
            "container architecture",
        ),
        "os": _text(value.get("Os"), "container os"),
        "sizeBytes": size,
    }
    actual_platform = f"{inspected['os']}/{inspected['architecture']}"
    if actual_platform != platform_name:
        raise EvaluationError(
            "Benchmark container platform mismatch: "
            f"expected {platform_name}, got {actual_platform}."
        )
    inspected["platform"] = actual_platform
    return inspected


def environment_fingerprint(
    config: SwarmConfig,
    profile: EvaluationProfile,
    *,
    container: dict[str, Any],
) -> dict[str, Any]:
    frontier_version = inspect_frontier_version(profile)
    source = mlx_swarm_source_revision()
    model_path = None
    model_fingerprint = None
    try:
        from .backend import _resolve_model_path

        resolved = _resolve_model_path(config)
        model_path = str(resolved)
        model_fingerprint = fingerprint_model_directory(resolved)
    except Exception:
        pass
    memory_bytes = None
    try:
        memory_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        pass
    python_runtime = python_runtime_identity()
    return {
        "schemaVersion": EVALUATION_SCHEMA_VERSION,
        "evaluationProtocolVersion": FAIR_EVALUATION_PROTOCOL_VERSION,
        "recordedAt": utc_now(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "codexVersion": (
            frontier_version
            if profile.frontier.adapter == "codex-cli"
            else None
        ),
        "frontierAdapter": profile.frontier.adapter,
        "frontierProvider": profile.frontier.provider,
        "frontierModel": profile.frontier.model,
        "frontierCommandVersion": frontier_version,
        "frontierToolsets": list(profile.frontier.toolsets),
        "frontierContextWindow": profile.frontier.context_window,
        "frontierMaxCompletionTokens": (
            profile.frontier.max_completion_tokens
        ),
        "reasoningEffort": profile.frontier.reasoning_effort or None,
        "mlxSwarmCommit": source["commit"],
        "mlxSwarmSourceDirty": source["dirty"],
        "hardware": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor() or "unknown",
            "memoryBytes": memory_bytes,
        },
        "runtime": {
            **python_runtime,
            "git": _best_effort_version(["git", "--version"]),
            "uv": _best_effort_version(["uv", "--version"]),
            "benchmarkBuildJobs": BENCHMARK_BUILD_JOBS,
            "container": container,
        },
        "localModel": {
            "repository": config.model.repository,
            "revision": config.model.revision,
            "path": model_path,
            "fingerprint": model_fingerprint,
        },
        "localWorker": {
            "mode": config.worker.mode,
            "reasoningMaxTokens": config.worker.reasoning_max_tokens,
            "capabilities": worker_capabilities_payload(
                config.worker.capabilities
            ),
        },
        "profileSha256": canonical_json_sha256(profile_payload(profile)),
    }


def python_runtime_identity() -> dict[str, Any]:
    """Return the exact Python and local-inference package identity."""
    packages: dict[str, str | None] = {}
    for name in ("mlx", "mlx-lm", "huggingface-hub"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "python": sys.version.split()[0],
        "pythonExecutable": str(Path(sys.executable).resolve()),
        "packages": packages,
    }


def inspect_codex_version(profile: EvaluationProfile) -> str:
    """Fail closed unless the configured Codex CLI matches the profile pin."""
    actual = _best_effort_version([profile.frontier.command, "--version"])
    if actual != profile.frontier.codex_version:
        raise EvaluationError(
            "Codex CLI version mismatch: "
            f"expected {profile.frontier.codex_version}, got "
            f"{actual or 'unavailable'}."
        )
    return actual


def inspect_frontier_version(profile: EvaluationProfile) -> str:
    """Fail closed unless the frontier command version matches the profile pin.

    For the codex-cli adapter this checks ``codexVersion`` via
    ``command --version``.  For hermes-completion it checks ``commandVersion``
    via ``command --version``.  The returned string is the frozen identity
    used in environment fingerprints and drift checks.
    """
    if profile.frontier.adapter == "codex-cli":
        return inspect_codex_version(profile)
    if profile.frontier.adapter in {
        "hermes-oneshot",
        "hermes-completion",
    }:
        actual = _best_effort_version(
            [profile.frontier.command, "--version"]
        )
        if actual != profile.frontier.command_version:
            raise EvaluationError(
                "Frontier command version mismatch: "
                f"expected {profile.frontier.command_version}, got "
                f"{actual or 'unavailable'}."
            )
        return actual
    raise EvaluationError(
        f"Unsupported frontier adapter: {profile.frontier.adapter}"
    )


def mlx_swarm_source_revision() -> dict[str, Any]:
    """Return the package source commit, never the evaluated target commit."""
    source_root = Path(__file__).resolve().parents[2]
    commit = _best_effort_output(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=source_root,
    )
    if not _GIT_SHA.fullmatch(commit):
        raise EvaluationError(
            "MLX Swarm source is not an identifiable Git revision."
        )
    status = run_command(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=source_root,
        timeout=30,
    )
    if status.returncode != 0 or status.timed_out:
        raise EvaluationError("Could not inspect MLX Swarm source state.")
    return {
        "root": str(source_root),
        "commit": commit,
        "dirty": bool(status.stdout.strip()),
    }


def fingerprint_model_directory(path: Path) -> str:
    records: list[dict[str, Any]] = []
    if path.is_dir():
        for child in sorted(path.rglob("*")):
            if not child.is_file():
                continue
            stat = child.stat()
            records.append({
                "path": str(child.relative_to(path)),
                "size": stat.st_size,
            })
    return canonical_json_sha256(records)


def sanitize_public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "caseId": row["caseId"],
        "project": row["project"],
        "frontier": _sanitize_arm(row["frontier"]),
        "swarm": _sanitize_arm(row["swarm"]),
        "frontierTokenDelta": row["frontierTokenDelta"],
        "timeDeltaSeconds": row["timeDeltaSeconds"],
    }


def remove_sensitive_preparation_sources(evaluation_dir: Path) -> None:
    """Remove upstream reference material before model execution."""
    root = evaluation_dir.resolve()
    for relative in (Path("benchmark"), Path("cache/repositories")):
        target = (root / relative).resolve()
        if not _is_within(target, root):
            raise EvaluationError("Preparation cleanup path escaped its root.")
        if target.is_dir() and not target.is_symlink():
            _remove_tree_with_retries(target)
        elif target.exists() or target.is_symlink():
            target.unlink()


def _remove_tree_with_retries(path: Path, *, attempts: int = 5) -> None:
    """Remove a tree despite short-lived filesystem metadata races."""
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.05 * (attempt + 1))
    raise EvaluationError(
        f"Could not remove sensitive preparation tree after {attempts} "
        f"attempts: {path}: {last_error}"
    )


def sanitize_suite(suite: dict[str, Any]) -> dict[str, Any]:
    return {
        **suite,
        "cases": [
            {
                "caseId": case["caseId"],
                "project": case["project"],
                "bugId": case["bugId"],
                "repository": case["repository"],
                "buggyCommit": case["buggyCommit"],
                "pythonVersion": case["pythonVersion"],
                "testFiles": case["testFiles"],
                "reference": case["reference"],
                "phase": case["phase"],
                "objective": case["objective"],
            }
            for case in suite["cases"]
        ],
    }


def sanitize_environment(value: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(value)
    local_model = dict(sanitized.get("localModel", {}))
    local_model["path"] = None
    sanitized["localModel"] = local_model
    return sanitized


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    pending = [os.fspath(path)]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(
                                follow_symlinks=False
                            ).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "—"
    value = max(0, int(round(float(seconds))))
    minutes, remaining = divmod(value, 60)
    return f"{minutes:02d}:{remaining:02d}"


def format_integer(value: int | float | None) -> str:
    if value is None:
        return "—"
    return f"{int(round(value)):,}"


def format_percentage(value: int | float | None) -> str:
    if value is None:
        return "—"
    return f"{float(value):.1f}%"


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "unknown memory"
    gibibytes = float(value) / 1024**3
    return f"{gibibytes:.1f} GiB memory"


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    max_output_bytes: int = MAX_LOG_BYTES,
) -> CommandResult:
    """Run one bounded array command with no shell and group timeout."""
    if not argv or any(not isinstance(value, str) or not value for value in argv):
        raise EvaluationError("Subprocess argv must contain non-empty strings.")
    started = time.perf_counter()
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(
            input_text.encode("utf-8") if input_text is not None else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        _remove_timed_out_docker_container(argv)
        try:
            os.killpg(process.pid, 15)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
    except BaseException:
        _remove_timed_out_docker_container(argv)
        try:
            os.killpg(process.pid, 15)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass
            process.communicate()
        raise
    return CommandResult(
        argv=tuple(argv),
        returncode=process.returncode,
        stdout=stdout[:max_output_bytes].decode("utf-8", "replace"),
        stderr=stderr[:max_output_bytes].decode("utf-8", "replace"),
        elapsed_seconds=time.perf_counter() - started,
        timed_out=timed_out,
    )


def _remove_timed_out_docker_container(argv: Sequence[str]) -> None:
    """Stop a named benchmark container when its attached CLI times out."""
    if list(argv[:2]) != ["docker", "run"] or "--name" not in argv:
        return
    name_index = list(argv).index("--name") + 1
    if name_index >= len(argv):
        return
    name = argv[name_index]
    if not re.fullmatch(r"mlx-swarm-eval-[0-9a-f]{20}", name):
        return
    try:
        subprocess.run(
            ["docker", "rm", "--force", name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_checked(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> CommandResult:
    result = run_command(argv, cwd=cwd, timeout=timeout, env=env)
    if result.timed_out:
        raise EvaluationError(f"Command timed out: {argv[0]}")
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise EvaluationError(
            f"Command failed ({result.returncode}): {' '.join(argv)}: {message}"
        )
    return result


def _arm_aggregate(
    results: Sequence[dict[str, Any]],
    *,
    include_local: bool,
) -> dict[str, Any]:
    frontier_totals = [
        total
        for total in (
            _reported_total(result["frontierUsage"])
            for result in results
        )
        if total is not None
    ]
    elapsed = [float(result["elapsedSeconds"]) for result in results]
    local_totals = [
        _local_total(result["localUsage"])
        for result in results
    ]
    return {
        "cases": len(results),
        "completed": sum(bool(result["completed"]) for result in results),
        "score": sum(int(result["score"]) for result in results),
        "completionRate": (
            sum(bool(result["completed"]) for result in results)
            / len(results)
            * 100.0
            if results
            else 0.0
        ),
        "scoreRate": (
            sum(int(result["score"]) for result in results)
            / len(results)
            * 100.0
            if results
            else 0.0
        ),
        "medianElapsedSeconds": (
            statistics.median(elapsed) if elapsed else None
        ),
        "frontierTokens": (
            sum(frontier_totals)
            if len(frontier_totals) == len(results)
            else None
        ),
        "medianFrontierTokens": (
            statistics.median(frontier_totals)
            if len(frontier_totals) == len(results) and frontier_totals
            else None
        ),
        "localTokens": sum(local_totals) if include_local else 0,
        "medianLocalTokens": (
            statistics.median(local_totals)
            if include_local and local_totals
            else 0
        ),
        "repairs": sum(int(result["repairs"]) for result in results),
        "medianRepairs": (
            statistics.median(int(result["repairs"]) for result in results)
            if results
            else 0
        ),
        "modelLoads": sum(int(result["modelLoads"]) for result in results),
    }


def _validate_frontier_usage(value: Any, name: str) -> None:
    usage = _object(value, name)
    _exact_keys(
        usage,
        name,
        {
            "usageStatus",
            "turns",
            "promptTokens",
            "cachedInputTokens",
            "completionTokens",
            "reasoningTokens",
            "totalTokens",
            "phases",
        },
    )
    status = _enum(
        usage["usageStatus"],
        f"{name}.usageStatus",
        {"reported", "unavailable"},
    )
    _integer(usage["turns"], f"{name}.turns", 0, 10**9)
    for field in (
        "promptTokens",
        "cachedInputTokens",
        "completionTokens",
        "reasoningTokens",
        "totalTokens",
    ):
        raw = usage[field]
        if status == "reported":
            _integer(raw, f"{name}.{field}", 0, 10**15)
        elif raw is not None:
            raise EvaluationError(
                f"{name}.{field} must be null when usage is unavailable."
            )
    phases = _list(usage["phases"], f"{name}.phases", maximum=10)
    phase_totals = {
        "turns": 0,
        "promptTokens": 0,
        "cachedInputTokens": 0,
        "completionTokens": 0,
        "reasoningTokens": 0,
        "totalTokens": 0,
    }
    all_reported = True
    for index, raw_phase in enumerate(phases):
        phase = _object(raw_phase, f"{name}.phases[{index}]")
        _exact_keys(
            phase,
            f"{name}.phases[{index}]",
            {
                "phase",
                "usageStatus",
                "turns",
                "promptTokens",
                "cachedInputTokens",
                "completionTokens",
                "reasoningTokens",
                "totalTokens",
                "malformedLines",
            },
            {"cacheWriteTokens"},
        )
        _text(phase["phase"], f"{name}.phases[{index}].phase")
        phase_status = _enum(
            phase["usageStatus"],
            f"{name}.phases[{index}].usageStatus",
            {"reported", "unavailable"},
        )
        turns = _integer(
            phase["turns"],
            f"{name}.phases[{index}].turns",
            0,
            10**9,
        )
        _integer(
            phase["malformedLines"],
            f"{name}.phases[{index}].malformedLines",
            0,
            10**9,
        )
        phase_totals["turns"] += turns
        all_reported = all_reported and phase_status == "reported"
        for field in (
            "promptTokens",
            "cachedInputTokens",
            "completionTokens",
            "reasoningTokens",
            "totalTokens",
        ):
            raw = phase[field]
            if phase_status == "reported":
                phase_totals[field] += _integer(
                    raw,
                    f"{name}.phases[{index}].{field}",
                    0,
                    10**15,
                )
            elif raw is not None:
                raise EvaluationError(
                    f"{name}.phases[{index}].{field} must be null "
                    "when usage is unavailable."
                )
    if phases:
        if (status == "reported") != all_reported:
            raise EvaluationError(
                f"{name}.usageStatus does not match its phase receipts."
            )
        if usage["turns"] != phase_totals["turns"]:
            raise EvaluationError(f"{name}.turns does not equal phase totals.")
        if status == "reported":
            for field in (
                "promptTokens",
                "cachedInputTokens",
                "completionTokens",
                "reasoningTokens",
                "totalTokens",
            ):
                if usage[field] != phase_totals[field]:
                    raise EvaluationError(
                        f"{name}.{field} does not equal phase totals."
                    )


def _validate_local_usage(value: Any, name: str) -> None:
    usage = _object(value, name)
    _exact_keys(
        usage,
        name,
        {
            "promptTokens",
            "generationTokens",
            "generationCalls",
            "modelLoads",
        },
    )
    for field in usage:
        _integer(usage[field], f"{name}.{field}", 0, 10**15)


def _reported_total(usage: dict[str, Any]) -> int | None:
    if usage.get("usageStatus") != "reported":
        return None
    total = usage.get("totalTokens")
    return int(total) if isinstance(total, int) else None


def _local_total(usage: dict[str, Any]) -> int:
    return int(usage.get("promptTokens", 0)) + int(
        usage.get("generationTokens", 0)
    )


def _usage_integer(
    usage: dict[str, Any],
    keys: Sequence[str],
    *,
    default: int | None = None,
) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    if default is not None:
        return default
    raise EvaluationError(
        f"Codex usage is missing required field: {'/'.join(keys)}"
    )


def _assign_patch_strata(candidates: list[dict[str, Any]]) -> None:
    values = sorted(
        candidate["reference"]["changedLines"]
        for candidate in candidates
    )
    one_third = values[max(0, math.ceil(len(values) / 3) - 1)]
    two_thirds = values[max(0, math.ceil(2 * len(values) / 3) - 1)]
    for candidate in candidates:
        changed = candidate["reference"]["changedLines"]
        candidate["reference"]["stratum"] = (
            "small"
            if changed <= one_third
            else "medium"
            if changed <= two_thirds
            else "large"
        )


def _rebalance_strata(
    measured: list[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    pilot: Sequence[dict[str, Any]],
    projects: Sequence[str],
    profile: EvaluationProfile,
) -> None:
    target_base, remainder = divmod(len(measured), 3)
    targets = {
        "small": target_base + (1 if remainder > 0 else 0),
        "medium": target_base + (1 if remainder > 1 else 0),
        "large": target_base,
    }
    selected_ids = {value["caseId"] for value in measured}
    excluded_ids = selected_ids | {value["caseId"] for value in pilot}
    project_counts = {
        project: sum(value["project"] == project for value in measured)
        for project in projects
    }
    for desired in ("small", "medium", "large"):
        while (
            sum(
                value["reference"]["stratum"] == desired
                for value in measured
            )
            < targets[desired]
        ):
            surplus = next(
                (
                    stratum
                    for stratum in ("large", "medium", "small")
                    if sum(
                        value["reference"]["stratum"] == stratum
                        for value in measured
                    )
                    > targets[stratum]
                ),
                None,
            )
            if surplus is None:
                break
            replacement_pair = None
            for current_index, current in enumerate(measured):
                if current["reference"]["stratum"] != surplus:
                    continue
                for candidate in candidates:
                    if (
                        candidate["caseId"] in excluded_ids
                        or candidate["project"] not in projects
                        or candidate["reference"]["stratum"] != desired
                    ):
                        continue
                    if candidate["project"] == current["project"]:
                        replacement_pair = (current_index, candidate)
                        break
                    if (
                        project_counts[candidate["project"]]
                        < profile.selection.max_per_project
                    ):
                        replacement_pair = (current_index, candidate)
                        break
                if replacement_pair is not None:
                    break
            if replacement_pair is None:
                raise EvaluationError(
                    "Unable to balance measured patch-size strata."
                )
            index, replacement = replacement_pair
            removed = measured[index]
            measured[index] = replacement
            excluded_ids.remove(removed["caseId"])
            excluded_ids.add(replacement["caseId"])
            project_counts[removed["project"]] -= 1
            project_counts[replacement["project"]] += 1


def _requirement_lines(
    text: str,
    *,
    project: str | None = None,
) -> list[str]:
    values: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if any(marker in line for marker in ("\x00", "\n", "\r")):
            raise EvaluationError("Requirement contains invalid characters.")
        if line.startswith("-e"):
            egg = re.search(r"#egg=([A-Za-z0-9_.-]+)", line)
            if (
                project is not None
                and egg is not None
                and egg.group(1).lower().replace("_", "-")
                == project.lower().replace("_", "-")
            ):
                # BugsInPy freezes the project itself in requirements. The
                # harness installs each isolated buggy/fixed workspace
                # explicitly instead of fetching that future-facing VCS URL.
                continue
            raise EvaluationError(
                "Editable dependency requirements are excluded."
            )
        if line.startswith(("--", "git+", "http:", "https:")):
            raise EvaluationError(
                "URL and option requirements are excluded."
            )
        match = re.match(r"^([A-Za-z0-9_.-]+)", line)
        if match is None or not is_exact_requirement(line):
            raise EvaluationError(
                "Benchmark dependencies must use exact == pins."
            )
        package = match.group(1).lower().replace("_", "-")
        canonical_project = (
            project.lower().replace("_", "-")
            if project is not None
            else None
        )
        if package == canonical_project:
            continue
        if package in {"pydivert", "pywin32"}:
            line = f"{line}; sys_platform == 'win32'"
        values.append(line)
    return values


def is_exact_requirement(value: str) -> bool:
    requirement = value.partition(";")[0].strip()
    return re.fullmatch(
        r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^=\s]+",
        requirement,
    ) is not None


def _is_non_production_path(path: str) -> bool:
    lowered = path.lower()
    return (
        lowered.startswith(_NON_PRODUCTION_PREFIXES)
        or "/test" in lowered
        or Path(lowered).name in _NON_PRODUCTION_NAMES
        or Path(lowered).suffix in {".lock", ".toml", ".yaml", ".yml"}
    )


def _python_version(value: Any) -> tuple[int, int]:
    if not isinstance(value, str):
        raise EvaluationError("Benchmark python_version is missing.")
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)(?:\.[0-9]+)?", value)
    if match is None:
        raise EvaluationError(f"Unsupported Python version: {value}")
    return int(match.group(1)), int(match.group(2))


def _commit(value: Any, name: str) -> str:
    if not isinstance(value, str) or _GIT_SHA.fullmatch(value) is None:
        raise EvaluationError(f"{name} must be a 40-character Git SHA.")
    return value


def _commitish(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{7,40}", value) is None
    ):
        raise EvaluationError(
            f"{name} must be a 7- to 40-character hexadecimal Git commit."
        )
    return value


def _safe_relative_paths(values: Iterable[str], name: str) -> None:
    for value in values:
        path = Path(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or ".git" in path.parts
            or not value
        ):
            raise EvaluationError(f"{name} contains an unsafe path: {value}")


def _sanitize_arm(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": value["schemaVersion"],
        "caseId": value["caseId"],
        "phase": value["phase"],
        "arm": value["arm"],
        "status": value["status"],
        "completed": value["completed"],
        "score": value["score"],
        "elapsedSeconds": value["elapsedSeconds"],
        "phaseSeconds": value["phaseSeconds"],
        "frontierUsage": value["frontierUsage"],
        "localUsage": value["localUsage"],
        "repairs": value["repairs"],
        "modelLoads": value["modelLoads"],
        "reviewVerdict": value["reviewVerdict"],
        "patch": value["patch"],
        "oracle": {
            "passed": value["oracle"]["passed"],
            "exitCode": value["oracle"]["exitCode"],
            "evidence": _sanitize_evidence(
                value["oracle"]["evidence"][:4_000]
            ),
        },
        "recordedAt": value["recordedAt"],
    }


def _sanitize_evidence(value: str) -> str:
    sanitized = re.sub(
        r"(?<![A-Za-z0-9_.-])/(?:Users|private|home|tmp|var)/[^\s:'\"]+",
        "<local-path>",
        value,
    )
    return re.sub(
        r"(?i)\b[A-Z]:\\(?:Users|Temp)\\[^\s:'\"]+",
        "<local-path>",
        sanitized,
    )


def _percentage(numerator: int | float, denominator: int | float | None) -> float | None:
    if denominator in {None, 0}:
        return None
    return float(numerator) / float(denominator) * 100.0


def _signed(value: int | float) -> str:
    return f"{value:+g}"


def _signed_duration(seconds: int | float) -> str:
    prefix = "+" if seconds >= 0 else "-"
    return prefix + format_duration(abs(seconds))


def _format_time_delta(
    frontier_seconds: int | float | None,
    swarm_seconds: int | float | None,
) -> str:
    if frontier_seconds is None or swarm_seconds is None:
        return "—"
    return _signed_duration(float(frontier_seconds) - float(swarm_seconds))


def _format_time_percentage(
    frontier_seconds: int | float | None,
    swarm_seconds: int | float | None,
) -> str:
    if frontier_seconds in {None, 0} or swarm_seconds is None:
        return "—"
    delta = (
        float(swarm_seconds) - float(frontier_seconds)
    ) / float(frontier_seconds) * 100.0
    return f"{delta:+.1f}%"


def _format_interval(lower: float | None, upper: float | None) -> str:
    if lower is None or upper is None:
        return "unavailable"
    return f"{lower:,.1f} to {upper:,.1f} tokens"


def _best_effort_version(argv: Sequence[str]) -> str | None:
    try:
        result = run_command(
            argv,
            cwd=Path.cwd(),
            timeout=10,
        )
    except Exception:
        return None
    output = (result.stdout or result.stderr).strip()
    return output.splitlines()[0].strip() if output else None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _best_effort_output(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
) -> str | None:
    try:
        result = run_command(
            argv,
            cwd=cwd or Path.cwd(),
            timeout=10,
        )
    except Exception:
        return None
    output = (result.stdout or result.stderr).strip()
    return output.splitlines()[-1] if output else None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvaluationError(f"JSON file not found: {path}") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise EvaluationError(f"Invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"JSON file must contain an object: {path}")
    return value


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _exclusive_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise EvaluationError(f"Evidence already exists: {path}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _exclusive_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise EvaluationError(f"Evidence already exists: {path}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvaluationError(f"{name} must be an object.")
    return value


def _list(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 100_000,
) -> list[Any]:
    if not isinstance(value, list):
        raise EvaluationError(f"{name} must be an array.")
    if not minimum <= len(value) <= maximum:
        raise EvaluationError(
            f"{name} must contain {minimum} to {maximum} entries."
        )
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
        raise EvaluationError(
            f"{name} is missing fields: {', '.join(sorted(missing))}."
        )
    if unknown:
        raise EvaluationError(
            f"{name} has unknown fields: {', '.join(sorted(unknown))}."
        )


def _text(
    value: Any,
    name: str,
    *,
    allow_empty: bool = False,
    maximum: int = 500_000,
) -> str:
    if not isinstance(value, str):
        raise EvaluationError(f"{name} must be a string.")
    if not allow_empty and not value.strip():
        raise EvaluationError(f"{name} must not be empty.")
    if len(value) > maximum:
        raise EvaluationError(f"{name} exceeds {maximum} characters.")
    return value


def _identifier(value: Any, name: str) -> str:
    text = _text(value, name)
    if _IDENTIFIER.fullmatch(text) is None:
        raise EvaluationError(f"{name} is not a safe identifier.")
    return text


def _integer(
    value: Any,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise EvaluationError(
            f"{name} must be an integer from {minimum} to {maximum}."
        )
    return value


def _number(
    value: Any,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise EvaluationError(
            f"{name} must be a finite number from {minimum} to {maximum}."
        )
    return float(value)


def _enum(value: Any, name: str, allowed: set[str]) -> str:
    text = _text(value, name)
    if text not in allowed:
        raise EvaluationError(
            f"{name} must be one of: {', '.join(sorted(allowed))}."
        )
    return text


def _unique_text_array(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 100_000,
) -> tuple[str, ...]:
    values = _unique_or_repeated_text_array(
        value,
        name,
        minimum=minimum,
        maximum=maximum,
    )
    if len(values) != len(set(values)):
        raise EvaluationError(f"{name} must not contain duplicates.")
    return tuple(values)


def _unique_or_repeated_text_array(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 100_000,
) -> list[str]:
    values = _list(value, name, minimum=minimum, maximum=maximum)
    return [
        _text(item, f"{name}[{index}]", maximum=8_192)
        for index, item in enumerate(values)
    ]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _module_main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m mlx_swarm.evaluation")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify-case")
    verify.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    if args.command == "verify-case":
        try:
            result = run_case_verifier(args.manifest.resolve(), Path.cwd())
        except EvaluationError as exc:
            print(f"INFRASTRUCTURE_ERROR: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["passed"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(_module_main())
