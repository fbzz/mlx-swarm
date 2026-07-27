"""Strict JSON contracts for swarm manifests, plans, and configuration."""
# @lat: [[Config]], [[Plans]]

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
MAX_WORKERS = 32
MAX_PROMPT_CHARS = 120_000
MAX_TASKS_PER_PLAN = 128
MAX_REPAIR_ATTEMPTS = 2

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

ROLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "implementation": {"temperature": 0.15, "top_p": 0.9, "max_tokens": 1800},
    "test": {"temperature": 0.10, "top_p": 0.95, "max_tokens": 1600},
    "review": {"temperature": 0.0, "top_p": 1.0, "max_tokens": 700},
    "general": {"temperature": 0.2, "top_p": 0.9, "max_tokens": 1200},
}


class ContractError(ValueError):
    """Raised when a configuration, plan, or manifest is invalid."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelConfig:
    repository: str
    revision: str
    local_path: str = ""


@dataclass(frozen=True)
class BatchConfig:
    max_workers: int = MAX_WORKERS
    prefill_step_size: int = 512
    max_prompt_characters: int = MAX_PROMPT_CHARS


@dataclass(frozen=True)
class SwarmConfig:
    source: Path = field(default_factory=Path)
    model: ModelConfig = field(default_factory=lambda: ModelConfig("", ""))
    batch: BatchConfig = field(default_factory=BatchConfig)
    artifacts_dir: Path = field(default_factory=lambda: Path(".swarm/runs"))
    enable_thinking: bool = False
    seed: int = 20260727


def load_config(path: Path) -> SwarmConfig:
    """Load and validate a swarm configuration JSON file."""
    raw = _read_json(path)
    _exact_keys(raw, "config", {"schemaVersion", "model", "batch", "artifacts"}, {"enableThinking", "seed"})

    schema_version = _integer(raw.get("schemaVersion", 1), "config.schemaVersion", 1, 100)
    if schema_version != CONFIG_SCHEMA_VERSION:
        raise ContractError(f"Unsupported config schema version: {schema_version}")

    model_raw = _object(raw["model"], "config.model")
    _exact_keys(model_raw, "config.model", {"repository"}, {"revision", "localPath"})
    model = ModelConfig(
        repository=_text(model_raw["repository"], "config.model.repository"),
        revision=_text(model_raw.get("revision", ""), "config.model.revision", allow_empty=True),
        local_path=_text(model_raw.get("localPath", ""), "config.model.localPath", allow_empty=True),
    )

    batch_raw = _object(raw.get("batch", {}), "config.batch")
    _exact_keys(batch_raw, "config.batch", set(), {"maxWorkers", "prefillStepSize", "maxPromptCharacters"})
    batch = BatchConfig(
        max_workers=_integer(batch_raw.get("maxWorkers", MAX_WORKERS), "config.batch.maxWorkers", 1, MAX_WORKERS),
        prefill_step_size=_integer(batch_raw.get("prefillStepSize", 512), "config.batch.prefillStepSize", 64, 8192),
        max_prompt_characters=_integer(
            batch_raw.get("maxPromptCharacters", MAX_PROMPT_CHARS),
            "config.batch.maxPromptCharacters",
            1024,
            500_000,
        ),
    )

    artifacts_dir = Path(_text(raw.get("artifacts", ".swarm/runs"), "config.artifacts"))
    if not artifacts_dir.is_absolute():
        artifacts_dir = (path.parent / artifacts_dir).resolve()

    return SwarmConfig(
        source=path.resolve(),
        model=model,
        batch=batch,
        artifacts_dir=artifacts_dir,
        enable_thinking=_boolean(raw.get("enableThinking", False), "config.enableThinking"),
        seed=_integer(raw.get("seed", 20260727), "config.seed", 0, 2**31 - 1),
    )


# ---------------------------------------------------------------------------
# Plan and tasks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskDef:
    id: str
    role: str  # implementation | test | review | general
    prompt: str
    gate: "OutputGate | None" = None
    depends_on: tuple[str, ...] = ()
    max_repair_attempts: int = MAX_REPAIR_ATTEMPTS
    generation_override: dict[str, Any] = field(default_factory=dict)
    output_protocol: str = ""


@dataclass(frozen=True)
class GatePattern:
    identifier: str
    pattern: str


@dataclass(frozen=True)
class OutputGate:
    required_patterns: tuple[GatePattern, ...] = ()
    forbidden_patterns: tuple[GatePattern, ...] = ()
    max_characters: int = 20_000
    output_format: str = "text"  # text | json
    strip_single_code_fence: bool = True
    python_syntax: bool = False
    json_required_keys: tuple[str, ...] = ()
    json_allowed_keys: tuple[str, ...] = ()
    json_field_enums: dict[str, tuple[Any, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextSource:
    label: str
    content: str
    origin: str = "inline"
    sha256: str = ""


@dataclass(frozen=True)
class TaskContext:
    objective: str
    authoritative_sources: tuple[ContextSource, ...] = ()
    constraints: tuple[str, ...] = ()
    rejection_criteria: tuple[str, ...] = ()
    output_protocol: str = "Return complete code only — no markdown fences, no prose, no preamble."


@dataclass(frozen=True)
class Plan:
    source: Path
    plan_id: str
    objective: str
    context: TaskContext | None
    tasks: tuple[TaskDef, ...]
    raw: dict[str, Any]

    def topological_order(self) -> list[list[TaskDef]]:
        """Return tasks grouped by dependency level for batch execution."""
        remaining = list(self.tasks)
        completed: set[str] = set()
        levels: list[list[TaskDef]] = []
        while remaining:
            ready = [t for t in remaining if all(dep in completed for dep in t.depends_on)]
            if not ready:
                raise ContractError(
                    f"Circular dependency or missing task among: {[t.id for t in remaining]}"
                )
            levels.append(ready)
            completed.update(t.id for t in ready)
            remaining = [t for t in remaining if t.id not in completed]
        return levels


def load_plan(path: Path, config: SwarmConfig) -> Plan:
    """Load and validate a plan JSON file."""
    raw = _read_json(path)
    _exact_keys(raw, "plan", {"planId", "objective", "tasks"}, {"context", "schemaVersion"})
    schema_version = _integer(raw.get("schemaVersion", 1), "plan.schemaVersion", 1, 100)
    if schema_version != MANIFEST_SCHEMA_VERSION:
        raise ContractError(f"Unsupported plan schema version: {schema_version}")

    plan_id = _identifier(raw["planId"], "plan.planId")
    objective = _text(raw["objective"], "plan.objective")

    context: TaskContext | None = None
    if "context" in raw:
        context = _parse_context(raw["context"], config)

    task_list = raw["tasks"]
    if not isinstance(task_list, list):
        raise ContractError("plan.tasks must be an array.")
    if not 1 <= len(task_list) <= MAX_TASKS_PER_PLAN:
        raise ContractError(f"plan.tasks must contain 1 to {MAX_TASKS_PER_PLAN} entries.")

    tasks: list[TaskDef] = []
    task_ids: set[str] = set()
    for i, t_raw in enumerate(task_list):
        name = f"plan.tasks[{i}]"
        t = _object(t_raw, name)
        _exact_keys(
            t,
            name,
            {"id", "role", "prompt"},
            {
                "gate",
                "dependsOn",
                "maxRepairAttempts",
                "generationOverride",
                "outputProtocol",
            },
        )
        tid = _identifier(t["id"], f"{name}.id")
        if tid in task_ids:
            raise ContractError(f"Duplicate task id: {tid}")
        task_ids.add(tid)
        role = _text(t["role"], f"{name}.role")
        if role not in ROLE_DEFAULTS:
            raise ContractError(f"{name}.role must be one of: {', '.join(ROLE_DEFAULTS)}")
        prompt = _text(t["prompt"], f"{name}.prompt")
        gate = _parse_gate(t["gate"], f"{name}.gate") if "gate" in t else None
        depends_on = tuple(
            _identifier(d, f"{name}.dependsOn[{j}]")
            for j, d in enumerate(_list(t.get("dependsOn", []), f"{name}.dependsOn"))
        )
        for dep in depends_on:
            if dep == tid:
                raise ContractError(f"{name} depends on itself: {dep}")
        max_repair = _integer(
            t.get("maxRepairAttempts", MAX_REPAIR_ATTEMPTS),
            f"{name}.maxRepairAttempts",
            0,
            5,
        )
        gen_override = _parse_generation_override(
            t.get("generationOverride", {}),
            f"{name}.generationOverride",
        )
        tasks.append(
            TaskDef(
                id=tid,
                role=role,
                prompt=prompt,
                gate=gate,
                depends_on=depends_on,
                max_repair_attempts=max_repair,
                generation_override=gen_override,
                output_protocol=_text(
                    t.get("outputProtocol", ""),
                    f"{name}.outputProtocol",
                    allow_empty=True,
                ),
            )
        )

    # Validate all depends_on targets exist
    for t in tasks:
        for dep in t.depends_on:
            if dep not in task_ids:
                raise ContractError(f"Task {t.id} depends on unknown task: {dep}")

    plan = Plan(
        source=path.resolve(),
        plan_id=plan_id,
        objective=objective,
        context=context,
        tasks=tuple(tasks),
        raw=raw,
    )
    plan.topological_order()
    return plan


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_context(raw: Any, config: SwarmConfig) -> TaskContext:
    ctx = _object(raw, "context")
    _exact_keys(ctx, "context", {"objective", "authoritativeSources", "constraints", "rejectionCriteria", "outputProtocol"})
    sources: list[ContextSource] = []
    for i, s_raw in enumerate(_list(ctx["authoritativeSources"], "context.authoritativeSources")):
        name = f"context.authoritativeSources[{i}]"
        s = _object(s_raw, name)
        _exact_keys(s, name, {"label", "content"})
        content = _text(s["content"], f"{name}.content")
        sources.append(
            ContextSource(
                label=_text(s["label"], f"{name}.label"),
                content=content,
                origin="inline",
                sha256=hashlib.sha256(content.encode()).hexdigest(),
            )
        )
    return TaskContext(
        objective=_text(ctx["objective"], "context.objective"),
        authoritative_sources=tuple(sources),
        constraints=tuple(_text_array(ctx["constraints"], "context.constraints")),
        rejection_criteria=tuple(_text_array(ctx["rejectionCriteria"], "context.rejectionCriteria")),
        output_protocol=_text(ctx["outputProtocol"], "context.outputProtocol"),
    )


def _parse_gate(raw: Any, name: str) -> OutputGate:
    g = _object(raw, name)
    _exact_keys(
        g,
        name,
        {"requiredPatterns", "forbiddenPatterns", "maxCharacters"},
        {
            "format",
            "stripSingleCodeFence",
            "pythonSyntax",
            "jsonRequiredKeys",
            "jsonAllowedKeys",
            "jsonFieldEnums",
        },
    )
    output_format = _text(g.get("format", "text"), f"{name}.format")
    if output_format not in {"text", "json"}:
        raise ContractError(f"{name}.format must be one of: text, json")

    json_required_keys = _unique_text_array(
        g.get("jsonRequiredKeys", []),
        f"{name}.jsonRequiredKeys",
    )
    json_allowed_keys = _unique_text_array(
        g.get("jsonAllowedKeys", []),
        f"{name}.jsonAllowedKeys",
    )
    if (json_required_keys or json_allowed_keys) and output_format != "json":
        raise ContractError(f"{name} JSON key rules require format=json.")
    if json_allowed_keys:
        unknown_required = set(json_required_keys) - set(json_allowed_keys)
        if unknown_required:
            raise ContractError(
                f"{name}.jsonRequiredKeys not allowed: {', '.join(sorted(unknown_required))}"
            )

    json_field_enums = _parse_json_field_enums(
        g.get("jsonFieldEnums", {}),
        f"{name}.jsonFieldEnums",
    )
    if json_field_enums and output_format != "json":
        raise ContractError(f"{name}.jsonFieldEnums requires format=json.")
    if json_allowed_keys:
        unknown_enum_fields = set(json_field_enums) - set(json_allowed_keys)
        if unknown_enum_fields:
            raise ContractError(
                f"{name}.jsonFieldEnums fields not allowed: "
                f"{', '.join(sorted(unknown_enum_fields))}"
            )

    return OutputGate(
        required_patterns=_parse_patterns(g["requiredPatterns"], f"{name}.requiredPatterns"),
        forbidden_patterns=_parse_patterns(g["forbiddenPatterns"], f"{name}.forbiddenPatterns"),
        max_characters=_integer(g["maxCharacters"], f"{name}.maxCharacters", 1, 100_000),
        output_format=output_format,
        strip_single_code_fence=_boolean(
            g.get("stripSingleCodeFence", True),
            f"{name}.stripSingleCodeFence",
        ),
        python_syntax=_boolean(g.get("pythonSyntax", False), f"{name}.pythonSyntax"),
        json_required_keys=json_required_keys,
        json_allowed_keys=json_allowed_keys,
        json_field_enums=json_field_enums,
    )


def _parse_generation_override(raw: Any, name: str) -> dict[str, Any]:
    value = _object(raw, name)
    _exact_keys(
        value,
        name,
        set(),
        {"temperature", "top_p", "max_tokens", "seed", "enable_thinking"},
    )
    result: dict[str, Any] = {}
    if "temperature" in value:
        result["temperature"] = _number(
            value["temperature"],
            f"{name}.temperature",
            0.0,
            2.0,
        )
    if "top_p" in value:
        top_p = _number(value["top_p"], f"{name}.top_p", 0.0, 1.0)
        if top_p == 0:
            raise ContractError(f"{name}.top_p must be greater than 0.")
        result["top_p"] = top_p
    if "max_tokens" in value:
        result["max_tokens"] = _integer(
            value["max_tokens"],
            f"{name}.max_tokens",
            1,
            8192,
        )
    if "seed" in value:
        result["seed"] = _integer(
            value["seed"],
            f"{name}.seed",
            0,
            2**31 - 1,
        )
    if "enable_thinking" in value:
        result["enable_thinking"] = _boolean(
            value["enable_thinking"],
            f"{name}.enable_thinking",
        )
    return result


def _parse_json_field_enums(raw: Any, name: str) -> dict[str, tuple[Any, ...]]:
    value = _object(raw, name)
    result: dict[str, tuple[Any, ...]] = {}
    for key, choices_raw in value.items():
        field_name = _text(key, f"{name} key")
        choices = _list(
            choices_raw,
            f"{name}.{field_name}",
            minimum=1,
            maximum=32,
        )
        for i, choice in enumerate(choices):
            if isinstance(choice, (dict, list)):
                raise ContractError(
                    f"{name}.{field_name}[{i}] must be a JSON scalar."
                )
        result[field_name] = tuple(choices)
    return result


def _parse_patterns(raw: Any, name: str) -> tuple[GatePattern, ...]:
    items = _list(raw, name)
    if len(items) > 32:
        raise ContractError(f"{name} exceeds 32 entries.")
    patterns: list[GatePattern] = []
    seen: set[str] = set()
    for i, p_raw in enumerate(items):
        p = _object(p_raw, f"{name}[{i}]")
        _exact_keys(p, f"{name}[{i}]", {"id", "pattern"})
        pid = _identifier(p["id"], f"{name}[{i}].id")
        if pid in seen:
            raise ContractError(f"Duplicate pattern id: {pid}")
        seen.add(pid)
        expr = _text(p["pattern"], f"{name}[{i}].pattern")
        if len(expr) > 2000:
            raise ContractError(f"{name}[{i}].pattern exceeds 2000 characters.")
        re.compile(expr, re.MULTILINE)
        patterns.append(GatePattern(identifier=pid, pattern=expr))
    return tuple(patterns)


# ---------------------------------------------------------------------------
# JSON primitives
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"Invalid JSON in {path} at line {exc.lineno}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"Expected JSON object in {path}.")
    return value


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{name} must be an object.")
    return value


def _exact_keys(value: dict[str, Any], name: str, required: set[str], optional: set[str] | None = None) -> None:
    optional = optional or set()
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ContractError(f"{name} missing: {', '.join(sorted(missing))}")
    if unknown:
        raise ContractError(f"{name} unknown fields: {', '.join(sorted(unknown))}")


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{name} must be an integer.")
    if not minimum <= value <= maximum:
        raise ContractError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _number(
    value: Any,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{name} must be a number.")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ContractError(f"{name} must be between {minimum} and {maximum}.")
    return result


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{name} must be a boolean.")
    return value


def _text(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{name} must be a string.")
    result = value.strip()
    if not allow_empty and not result:
        raise ContractError(f"{name} must not be empty.")
    return result


def _identifier(value: Any, name: str) -> str:
    result = _text(value, name)
    if _IDENTIFIER.fullmatch(result) is None:
        raise ContractError(f"{name} must match {_IDENTIFIER.pattern}.")
    return result


def _list(value: Any, name: str, *, minimum: int = 0, maximum: int = 128) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{name} must be an array.")
    if not minimum <= len(value) <= maximum:
        raise ContractError(f"{name} must contain {minimum} to {maximum} entries.")
    return value


def _text_array(value: Any, name: str, *, minimum: int = 0, maximum: int = 64) -> tuple[str, ...]:
    items = _list(value, name, minimum=minimum, maximum=maximum)
    return tuple(_text(e, f"{name}[{i}]") for i, e in enumerate(items))


def _unique_text_array(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 64,
) -> tuple[str, ...]:
    items = _text_array(value, name, minimum=minimum, maximum=maximum)
    if len(set(items)) != len(items):
        raise ContractError(f"{name} must not contain duplicates.")
    return items
