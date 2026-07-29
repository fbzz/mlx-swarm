"""One-request OpenAI-compatible completion bridge for Hermes providers.

This module is executed by the Python interpreter from the configured Hermes
installation.  Hermes remains the authority for provider and credential
resolution, but its interactive agent loop is deliberately bypassed: each
process makes exactly one model API request, exposes no tools, and records one
strict usage receipt.
"""
# @lat: [[economics-evaluation]]

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _non_negative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"Provider did not report {name}.")
    return value


def _detail_token(value: Any, name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, dict):
        return _non_negative_int(value.get(name, 0), name)
    return _non_negative_int(getattr(value, name, 0), name)


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
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
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from openai import OpenAI

        runtime = resolve_runtime_provider(
            requested=args.provider,
            target_model=args.model,
        )
        if runtime.get("api_mode") != "chat_completions":
            raise RuntimeError(
                "Hermes provider does not expose chat_completions."
            )
        api_key = runtime.get("api_key")
        base_url = runtime.get("base_url")
        if not isinstance(api_key, str) or not api_key:
            raise RuntimeError("Hermes provider credential is unavailable.")
        if not isinstance(base_url, str) or not base_url:
            raise RuntimeError("Hermes provider base URL is unavailable.")

        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=args.request_timeout_seconds,
            max_retries=0,
        )
        calls = 1
        response = client.chat.completions.create(
            model=args.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Return exactly one JSON object. Do not use tools, "
                        "Markdown fences, XML, or explanatory prose."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=args.max_completion_tokens,
            response_format={"type": "json_object"},
        )
        usage = response.usage
        if usage is None:
            raise RuntimeError("Provider omitted usage.")
        input_tokens = _non_negative_int(usage.prompt_tokens, "prompt_tokens")
        output_tokens = _non_negative_int(
            usage.completion_tokens,
            "completion_tokens",
        )
        total_tokens = _non_negative_int(usage.total_tokens, "total_tokens")
        if total_tokens != input_tokens + output_tokens:
            raise RuntimeError("Provider token arithmetic is inconsistent.")
        choices = response.choices
        if len(choices) != 1:
            raise RuntimeError("Provider did not return exactly one choice.")
        content = choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Provider returned no final response content.")

        receipt = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": _detail_token(
                getattr(usage, "prompt_tokens_details", None),
                "cached_tokens",
            ),
            "cache_write_tokens": 0,
            "reasoning_tokens": _detail_token(
                getattr(usage, "completion_tokens_details", None),
                "reasoning_tokens",
            ),
            "total_tokens": total_tokens,
            "api_calls": calls,
            "model": args.model,
            "provider": args.provider,
            "completed": True,
            "failed": False,
        }
        _atomic_json(args.usage_file, receipt)
        sys.stdout.write(content.strip())
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
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
        print(f"Hermes completion failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
