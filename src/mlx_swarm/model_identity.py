"""Stable identities for the load-relevant local MLX model payload."""
# @lat: [[Backend]]

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MODEL_FINGERPRINT_ALGORITHM = "mlx-model-content-v1"
_MODEL_METADATA_NAMES = {
    "added_tokens.json",
    "chat_template.jinja",
    "config.json",
    "generation_config.json",
    "merges.txt",
    "model.safetensors.index.json",
    "preprocessor_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
    "vocab.json",
}


def model_directory_identity(path: Path) -> dict[str, Any]:
    """Hash model weights and inference metadata, excluding cache churn."""
    root = path.expanduser().resolve()
    records: list[dict[str, Any]] = []
    if root.is_dir():
        for child in sorted(root.rglob("*")):
            if not child.is_file():
                continue
            relative = child.relative_to(root)
            if not _is_load_relevant(relative):
                continue
            records.append({
                "path": relative.as_posix(),
                "size": child.stat().st_size,
                "sha256": file_sha256(child),
            })
    encoded = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "algorithm": MODEL_FINGERPRINT_ALGORITHM,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "fileCount": len(records),
        "files": records,
    }


def model_metadata(
    path: Path,
    *,
    declared_context_tokens: int = 0,
) -> dict[str, Any]:
    """Inspect load metadata needed to audit a configured local checkpoint."""
    root = path.expanduser().resolve()
    config_path = root / "config.json"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "metadataReady": False,
            "error": f"Cannot read model config.json: {exc}",
            "declaredContextTokens": declared_context_tokens,
            "reportedContextTokens": None,
            "contextCompatible": None,
        }
    if not isinstance(raw, dict):
        return {
            "metadataReady": False,
            "error": "Model config.json must contain an object.",
            "declaredContextTokens": declared_context_tokens,
            "reportedContextTokens": None,
            "contextCompatible": None,
        }
    text_config = raw.get("text_config")
    if not isinstance(text_config, dict):
        text_config = {}
    reported_context = _positive_integer_or_none(
        text_config.get(
            "max_position_embeddings",
            raw.get("max_position_embeddings"),
        )
    )
    quantization = raw.get("quantization")
    if not isinstance(quantization, dict):
        quantization = raw.get("quantization_config")
    if not isinstance(quantization, dict):
        quantization = {}
    quantization_bits = _positive_integer_or_none(
        quantization.get("bits")
    )
    context_compatible = (
        None
        if not declared_context_tokens or reported_context is None
        else declared_context_tokens <= reported_context
    )
    warnings: list[str] = []
    if not declared_context_tokens:
        warnings.append(
            "Worker contextWindowTokens is unreported; the runtime cannot "
            "preflight prompt plus completion length."
        )
    elif reported_context is None:
        warnings.append(
            "The checkpoint does not report max_position_embeddings."
        )
    elif not context_compatible:
        warnings.append(
            f"Configured contextWindowTokens {declared_context_tokens} "
            f"exceeds checkpoint metadata {reported_context}."
        )
    return {
        "metadataReady": True,
        "modelType": text_config.get(
            "model_type",
            raw.get("model_type"),
        ),
        "declaredContextTokens": declared_context_tokens,
        "reportedContextTokens": reported_context,
        "contextCompatible": context_compatible,
        "quantizationBits": quantization_bits,
        "warnings": warnings,
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _positive_integer_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _is_load_relevant(relative: Path) -> bool:
    parts = relative.parts
    if any(part in {".cache", "__pycache__"} for part in parts):
        return False
    name = relative.name
    if (
        name.startswith(".")
        or name.endswith((".lock", ".incomplete", ".tmp"))
    ):
        return False
    return (
        name in _MODEL_METADATA_NAMES
        or name.endswith(".safetensors")
        or name.startswith("chat_template.")
    )
