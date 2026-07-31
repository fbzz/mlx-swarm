"""One-request Claude Code CLI completion bridge.

This module invokes the pinned ``claude`` CLI in headless print mode: each
process makes exactly one single-turn request with every workspace tool
disallowed, consumes the machine-readable JSON envelope, and records one
strict usage receipt in the shared adapter shape.  The subscription-backed
CLI remains the authority for credentials; its agent loop is deliberately
bounded to one turn so the run stays a pure completion.
"""
# @lat: [[economics-evaluation]]

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_DISALLOWED_TOOLS = (
    "Agent",
    "Bash",
    "Edit",
    "Glob",
    "Grep",
    "LS",
    "MultiEdit",
    "NotebookEdit",
    "Read",
    "Skill",
    "SlashCommand",
    "Task",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Write",
)

_SYSTEM_DISCIPLINE = (
    "Return exactly one JSON object. Do not use tools, Markdown fences, "
    "XML, or explanatory prose."
)


def _non_negative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"Claude envelope did not report {name}.")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _failed_receipt(
    *,
    provider: str,
    model: str,
    api_calls: int,
) -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "api_calls": api_calls,
        "model": model,
        "provider": provider,
        "completed": False,
        "failed": True,
    }


def receipt_from_envelope(
    envelope: dict[str, Any],
    *,
    provider: str,
    model: str,
) -> tuple[dict[str, Any], str]:
    """Map one Claude headless JSON envelope to the strict usage receipt."""
    if envelope.get("is_error") is not False:
        raise RuntimeError("Claude envelope reports an error.")
    if envelope.get("subtype") != "success":
        raise RuntimeError("Claude envelope did not complete successfully.")
    if envelope.get("num_turns") != 1:
        raise RuntimeError("Claude run used more than one turn.")
    model_usage = envelope.get("modelUsage")
    if not isinstance(model_usage, dict) or model not in model_usage:
        raise RuntimeError(
            "Claude envelope does not attribute usage to the pinned model."
        )
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        raise RuntimeError("Claude envelope omitted usage.")
    uncached = _non_negative_int(usage.get("input_tokens"), "input_tokens")
    cache_read = _non_negative_int(
        usage.get("cache_read_input_tokens", 0),
        "cache_read_input_tokens",
    )
    cache_write = _non_negative_int(
        usage.get("cache_creation_input_tokens", 0),
        "cache_creation_input_tokens",
    )
    output_tokens = _non_negative_int(
        usage.get("output_tokens"),
        "output_tokens",
    )
    input_tokens = uncached + cache_read + cache_write
    result = envelope.get("result")
    has_content = isinstance(result, str) and bool(result.strip())
    receipt = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": 0,
        "total_tokens": input_tokens + output_tokens,
        "api_calls": 1,
        "model": model,
        "provider": provider,
        "completed": has_content,
        "failed": not has_content,
    }
    return receipt, result.strip() if has_content else ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--usage-file", type=Path, required=True)
    parser.add_argument("--max-completion-tokens", type=int, required=True)
    parser.add_argument("--request-timeout-seconds", type=int, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not 1 <= args.max_completion_tokens <= 131_072:
        print("Invalid max completion token limit.", file=sys.stderr)
        return 2
    if not 1 <= args.request_timeout_seconds <= 86_400:
        print("Invalid request timeout.", file=sys.stderr)
        return 2
    try:
        prompt = args.prompt_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"Could not read prompt: {exc}", file=sys.stderr)
        return 2
    if not prompt.strip():
        print("Prompt is empty.", file=sys.stderr)
        return 2

    calls = 0
    receipt_written = False
    try:
        argv = [
            args.command,
            "-p",
            "--model",
            args.model,
            "--output-format",
            "json",
            "--max-turns",
            "1",
            "--append-system-prompt",
            _SYSTEM_DISCIPLINE,
            "--disallowedTools",
            *_DISALLOWED_TOOLS,
        ]
        env = dict(os.environ)
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(
            args.max_completion_tokens
        )
        if "USER" not in env:
            # The harness passes a minimal allowlisted environment; the
            # Claude CLI's keychain credential lookup needs the invoking
            # user's identity.
            import pwd

            env["USER"] = pwd.getpwuid(os.getuid()).pw_name
        calls = 1
        process = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=args.request_timeout_seconds,
            env=env,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(
                "Claude CLI exited with "
                f"{process.returncode}: {process.stderr.strip()[:400]}"
            )
        try:
            envelope = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Claude envelope is not valid JSON: {exc}"
            ) from exc
        if not isinstance(envelope, dict):
            raise RuntimeError("Claude envelope is not a JSON object.")
        receipt, content = receipt_from_envelope(
            envelope,
            provider=args.provider,
            model=args.model,
        )
        _atomic_json(args.usage_file, receipt)
        receipt_written = True
        if not content:
            raise RuntimeError("Claude returned no final response content.")
        sys.stdout.write(content)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        if not receipt_written:
            try:
                _atomic_json(
                    args.usage_file,
                    _failed_receipt(
                        provider=args.provider,
                        model=args.model,
                        api_calls=calls,
                    ),
                )
            except OSError:
                pass
        print(f"Claude completion failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
