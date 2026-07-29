"""Compatibility namespace for the former ``swarm_agents`` package.

Use :mod:`mlx_swarm` instead. This namespace is retained temporarily for the
0.3 compatibility release.
"""

from __future__ import annotations

import importlib
import sys
import warnings

warnings.warn(
    "swarm_agents is deprecated; import mlx_swarm instead.",
    DeprecationWarning,
    stacklevel=2,
)

for _module_name in (
    "backend",
    "contracts",
    "executor",
    "gates",
    "prompting",
    "session",
    "ui",
):
    sys.modules[f"{__name__}.{_module_name}"] = importlib.import_module(
        f"mlx_swarm.{_module_name}"
    )

from mlx_swarm import __version__  # noqa: E402,F401
