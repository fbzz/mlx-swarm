"""MLX batch backend — persistent model loading and grouped batched generation."""
# @lat: [[Backend]]

from __future__ import annotations

import gc
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Protocol

from .contracts import ROLE_DEFAULTS, SwarmConfig, TaskDef


class BatchBackend(Protocol):
    """Execution-facing backend contract used by the DAG executor."""

    def generate(
        self,
        tasks: list[TaskDef],
        prompts: list[str],
    ) -> tuple[list[str], dict[str, Any]]:
        ...

    def close(self) -> None:
        ...


def _resolve_model_path(config: SwarmConfig) -> Path:
    """Resolve the model path: explicit local_path > pinned revision > repo lookup."""
    if config.model.local_path:
        p = Path(config.model.local_path).expanduser().resolve()
        missing = [
            name
            for name in ("config.json", "model.safetensors", "tokenizer.json")
            if not (p / name).is_file()
        ]
        if missing:
            raise RuntimeError(f"Model path {p} missing: {', '.join(missing)}")
        return p

    if config.model.repository:
        from huggingface_hub import snapshot_download

        kwargs: dict[str, Any] = {"repo_id": config.model.repository}
        if config.model.revision:
            kwargs["revision"] = config.model.revision
        try:
            snapshot = snapshot_download(local_files_only=True, **kwargs)
        except Exception as exc:
            raise RuntimeError(
                f"Model not cached locally. Run: hf download {config.model.repository}"
                + (f" --revision {config.model.revision}" if config.model.revision else "")
            ) from exc
        p = Path(snapshot).resolve()
        missing = [
            name
            for name in ("config.json", "model.safetensors", "tokenizer.json")
            if not (p / name).is_file()
        ]
        if missing:
            raise RuntimeError(f"Model snapshot {p} missing: {', '.join(missing)}")
        return p

    raise RuntimeError(
        "No model configured. Set config.model.repository or config.model.localPath."
    )


def _role_generation_config(task: TaskDef, config: SwarmConfig) -> dict[str, Any]:
    """Merge validated role defaults with task-specific overrides."""
    result = ROLE_DEFAULTS.get(task.role, ROLE_DEFAULTS["general"]).copy()
    result.update(task.generation_override)
    result.setdefault("temperature", 0.2)
    result.setdefault("top_p", 0.9)
    result.setdefault("max_tokens", 1200)
    result.setdefault("enable_thinking", config.enable_thinking)
    result.setdefault("seed", config.seed)
    return result


def _render_prompt(tokenizer: Any, prompt: str, gen_cfg: dict[str, Any]) -> list[int]:
    """Render one worker request using the model's native chat template."""
    if getattr(tokenizer, "has_chat_template", False):
        rendered = tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt}],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=gen_cfg["enable_thinking"],
        )
        # Some distilled Qwen templates unconditionally end the generation
        # prefix with "<think>" and ignore enable_thinking=False. Close that
        # empty block in the assistant prefix so artifact tokens are generated
        # immediately instead of spending the full budget on hidden reasoning.
        if (
            not gen_cfg["enable_thinking"]
            and rendered.rstrip().endswith("<think>")
        ):
            rendered = rendered.rstrip() + "\n</think>\n\n"
        return list(tokenizer.encode(rendered, add_special_tokens=False))
    return list(tokenizer.encode(prompt))


def _response_parts(response: Any) -> tuple[list[str], Any]:
    if hasattr(response, "texts"):
        return list(response.texts), getattr(response, "stats", None)
    if isinstance(response, dict) and "texts" in response:
        return list(response["texts"]), response.get("stats")
    return list(response), None


def _stat_value(stats: Any, name: str, default: float = 0.0) -> float:
    if stats is None:
        return default
    if isinstance(stats, dict):
        return float(stats.get(name, default))
    return float(getattr(stats, name, default))


class MLXBatchBackend:
    """Keep one MLX model resident while executing all waves in a plan."""

    def __init__(self, config: SwarmConfig):
        self.config = config
        self.model_path = _resolve_model_path(config)
        self.model: Any = None
        self.tokenizer: Any = None
        self.mx: Any = None
        self.batch_generate_fn: Any = None
        self.make_sampler_fn: Any = None
        self.load_seconds = 0.0
        self._load_reported = False

    def open(self) -> None:
        if self.model is not None:
            return

        import mlx.core as mx
        from mlx_lm import load
        from mlx_lm.generate import batch_generate
        from mlx_lm.sample_utils import make_sampler

        load_started = time.perf_counter()
        self.model, self.tokenizer = load(
            str(self.model_path),
            lazy=False,
            tokenizer_config={"fix_mistral_regex": True},
        )
        self.load_seconds = time.perf_counter() - load_started
        self.mx = mx
        self.batch_generate_fn = batch_generate
        self.make_sampler_fn = make_sampler

    def generate(
        self,
        tasks: list[TaskDef],
        prompts: list[str],
    ) -> tuple[list[str], dict[str, Any]]:
        if len(tasks) != len(prompts):
            raise RuntimeError(
                f"Tasks ({len(tasks)}) and prompts ({len(prompts)}) length mismatch."
            )
        if not tasks:
            return [], {"batchSize": 0, "groups": []}
        if len(tasks) > self.config.batch.max_workers:
            raise RuntimeError(
                f"Batch size {len(tasks)} exceeds max_workers "
                f"{self.config.batch.max_workers}."
            )

        self.open()
        generation_configs = [
            _role_generation_config(task, self.config)
            for task in tasks
        ]
        tokenized = [
            _render_prompt(self.tokenizer, prompt, gen_cfg)
            for prompt, gen_cfg in zip(prompts, generation_configs)
        ]

        # One sampler is shared by an MLX BatchGenerator, so tasks with different
        # sampling parameters are grouped while the model remains loaded.
        groups: dict[tuple[float, float, int], list[int]] = defaultdict(list)
        for index, gen_cfg in enumerate(generation_configs):
            key = (
                float(gen_cfg["temperature"]),
                float(gen_cfg["top_p"]),
                int(gen_cfg["seed"]),
            )
            groups[key].append(index)

        output_texts = [""] * len(tasks)
        group_stats: list[dict[str, Any]] = []
        generation_seconds = 0.0
        prompt_tokens = 0
        generation_tokens = 0

        for (temperature, top_p, seed), indices in groups.items():
            self.mx.random.seed(seed)
            sampler = self.make_sampler_fn(temp=temperature, top_p=top_p)
            max_tokens = [
                int(generation_configs[index]["max_tokens"])
                for index in indices
            ]

            started = time.perf_counter()
            response = self.batch_generate_fn(
                model=self.model,
                tokenizer=self.tokenizer,
                prompts=[tokenized[index] for index in indices],
                max_tokens=max_tokens,
                verbose=False,
                sampler=sampler,
                prefill_step_size=self.config.batch.prefill_step_size,
                prefill_batch_size=min(8, len(indices)),
                completion_batch_size=min(
                    self.config.batch.max_workers,
                    len(indices),
                ),
            )
            elapsed = time.perf_counter() - started
            generation_seconds += elapsed

            texts, stats_obj = _response_parts(response)
            if len(texts) != len(indices):
                raise RuntimeError(
                    f"MLX returned {len(texts)} outputs for {len(indices)} prompts."
                )
            for index, text in zip(indices, texts):
                output_texts[index] = text

            group_prompt_tokens = int(_stat_value(stats_obj, "prompt_tokens"))
            group_generation_tokens = int(
                _stat_value(stats_obj, "generation_tokens")
            )
            prompt_tokens += group_prompt_tokens
            generation_tokens += group_generation_tokens
            group_stats.append({
                "taskIds": [tasks[index].id for index in indices],
                "temperature": temperature,
                "topP": top_p,
                "seed": seed,
                "maxTokens": max_tokens,
                "generationSeconds": elapsed,
                "promptTokens": group_prompt_tokens,
                "generationTokens": group_generation_tokens,
                "generationTokensPerSecond": _stat_value(
                    stats_obj,
                    "generation_tps",
                ),
            })

        load_seconds = 0.0 if self._load_reported else self.load_seconds
        self._load_reported = True
        stats: dict[str, Any] = {
            "loadSeconds": load_seconds,
            "modelReused": load_seconds == 0.0,
            "generationSeconds": generation_seconds,
            "batchSize": len(tasks),
            "promptTokens": prompt_tokens,
            "generationTokens": generation_tokens,
            "peakMemoryGigabytes": float(self.mx.get_peak_memory()) / 1e9,
            "groups": group_stats,
        }
        return output_texts, stats

    def close(self) -> None:
        if self.model is None:
            return
        self.model = None
        self.tokenizer = None
        if self.mx is not None:
            self.mx.clear_cache()
        gc.collect()

    def __enter__(self) -> "MLXBatchBackend":
        self.open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def generate_batch(
    config: SwarmConfig,
    tasks: list[TaskDef],
    prompts: list[str],
) -> tuple[list[str], dict[str, Any]]:
    """Compatibility helper for one-off generation outside the executor."""
    backend = MLXBatchBackend(config)
    try:
        return backend.generate(tasks, prompts)
    finally:
        backend.close()
