"""Aggregate operational statistics from durable session evidence."""
# @lat: [[Session]]

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TERMINAL_FAILURES = {
    "failed",
    "rejected",
    "rejected_by_operator",
    "verification_failed",
}


def collect_stats(artifacts_dir: Path) -> dict[str, Any]:
    """Scan every session ledger below *artifacts_dir* and aggregate.

    Only reads `session.json` files; never mutates evidence. Sessions that
    fail to parse are counted and skipped rather than aborting the scan.
    """
    sessions = 0
    unreadable = 0
    session_status: dict[str, int] = {}
    task_status: dict[str, int] = {}
    execution_modes: dict[str, int] = {}
    first_pass = {"passed": 0, "failed": 0}
    repairs_used = 0
    escalations = 0
    generation_tokens = 0
    generation_calls = 0
    tasks_total = 0
    blocked = 0

    for ledger in sorted(artifacts_dir.glob("*/*/session.json")):
        try:
            state = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable += 1
            continue
        if not isinstance(state, dict) or "tasks" not in state:
            unreadable += 1
            continue
        sessions += 1
        status = str(state.get("status"))
        session_status[status] = session_status.get(status, 0) + 1
        for task in state["tasks"].values():
            tasks_total += 1
            t_status = str(task.get("status"))
            task_status[t_status] = task_status.get(t_status, 0) + 1
            if t_status == "blocked":
                blocked += 1
            mode = str(task.get("executionMode", "local-agent"))
            execution_modes[mode] = execution_modes.get(mode, 0) + 1
            attempts = task.get("generationAttempts") or []
            if attempts:
                key = (
                    "passed"
                    if attempts[0].get("gatePassed") is True
                    else "failed"
                )
                first_pass[key] += 1
            repairs_used += int(task.get("repairAttempts") or 0)
            if task.get("escalatedMaxTokens"):
                escalations += 1
        for batch in state.get("batches") or []:
            statistics = batch.get("statistics") or {}
            generation_tokens += int(
                statistics.get("generationTokens") or 0
            )
            generation_calls += int(
                statistics.get("generationCalls") or 0
            )

    attempted = first_pass["passed"] + first_pass["failed"]
    return {
        "artifactsDir": str(artifacts_dir),
        "sessions": sessions,
        "unreadableSessions": unreadable,
        "sessionStatus": dict(sorted(session_status.items())),
        "tasks": tasks_total,
        "taskStatus": dict(sorted(task_status.items())),
        "executionModes": dict(sorted(execution_modes.items())),
        "blockedShare": round(blocked / tasks_total, 4) if tasks_total else None,
        "firstPassGate": {
            **first_pass,
            "rate": (
                round(first_pass["passed"] / attempted, 4)
                if attempted
                else None
            ),
        },
        "repairAttemptsUsed": repairs_used,
        "escalatedTasks": escalations,
        "localGenerationTokens": generation_tokens,
        "localGenerationCalls": generation_calls,
    }
