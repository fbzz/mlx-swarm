"""Read-only prompt and unified-diff projections for the review UI."""
# @lat: [[UI#Review surface]]

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

_DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@(?P<label>.*)$"
)


def parse_unified_diff(value: str) -> dict[str, Any]:
    """Parse a text Git diff into file/hunk/line records for safe rendering."""
    files: list[dict[str, Any]] = []
    current_file: dict[str, Any] | None = None
    current_hunk: dict[str, Any] | None = None
    old_line = 0
    new_line = 0
    for raw in value.splitlines():
        header = _DIFF_HEADER.match(raw)
        if header:
            current_file = {
                "oldPath": header.group(1),
                "newPath": header.group(2),
                "status": "modified",
                "additions": 0,
                "deletions": 0,
                "hunks": [],
            }
            files.append(current_file)
            current_hunk = None
            continue
        if current_file is None:
            continue
        if raw.startswith("new file mode "):
            current_file["status"] = "added"
            continue
        if raw.startswith("deleted file mode "):
            current_file["status"] = "deleted"
            continue
        hunk = _HUNK_HEADER.match(raw)
        if hunk:
            old_line = int(hunk.group("old"))
            new_line = int(hunk.group("new"))
            current_hunk = {
                "header": raw,
                "label": hunk.group("label").strip(),
                "oldStart": old_line,
                "newStart": new_line,
                "lines": [],
            }
            current_file["hunks"].append(current_hunk)
            continue
        if current_hunk is None or raw.startswith("\\ No newline"):
            continue
        if raw.startswith("+"):
            current_hunk["lines"].append({
                "type": "add",
                "oldLine": None,
                "newLine": new_line,
                "text": raw[1:],
            })
            current_file["additions"] += 1
            new_line += 1
        elif raw.startswith("-"):
            current_hunk["lines"].append({
                "type": "delete",
                "oldLine": old_line,
                "newLine": None,
                "text": raw[1:],
            })
            current_file["deletions"] += 1
            old_line += 1
        else:
            text = raw[1:] if raw.startswith(" ") else raw
            current_hunk["lines"].append({
                "type": "context",
                "oldLine": old_line,
                "newLine": new_line,
                "text": text,
            })
            old_line += 1
            new_line += 1
    return {
        "raw": value,
        "files": files,
        "summary": {
            "files": len(files),
            "additions": sum(item["additions"] for item in files),
            "deletions": sum(item["deletions"] for item in files),
        },
    }


def load_attempts(
    session_dir: Path,
    task_id: str,
    task_state: dict[str, Any],
) -> dict[str, Any]:
    """Load immutable prompt records after confinement and digest checks."""
    records: list[dict[str, Any]] = []
    for kind, key in (
        ("generation", "generationAttempts"),
        ("reasoning", "reasoningAttempts"),
    ):
        for summary in task_state.get(key, []):
            relative = summary.get("path")
            if not isinstance(relative, str):
                continue
            path = (session_dir / relative).resolve()
            if not _is_within(path, session_dir) or not path.is_file():
                continue
            import json

            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            prompt = value.get("prompt")
            output = value.get("output")
            if not isinstance(prompt, str) or not isinstance(output, str):
                continue
            prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            output_sha = hashlib.sha256(output.encode("utf-8")).hexdigest()
            if (
                prompt_sha != summary.get("promptSha256")
                or output_sha != summary.get("outputSha256")
            ):
                continue
            records.append({
                "kind": kind,
                "attempt": value.get("attempt"),
                "phase": value.get("phase"),
                "recordedAt": value.get("recordedAt"),
                "promptSha256": prompt_sha,
                "outputSha256": output_sha,
                "prompt": prompt,
                "output": output,
                "normalizedOutput": value.get("normalizedOutput"),
                "gateResult": value.get("gateResult"),
                "statistics": value.get("statistics"),
                "authoritative": value.get("authoritative", kind == "generation"),
            })
    records.sort(key=lambda item: (str(item.get("recordedAt") or ""), item["kind"]))
    return {"taskId": task_id, "attempts": records}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False
