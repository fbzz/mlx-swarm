"""Compatibility entrypoint for ``python -m swarm_agents.cli``."""

from __future__ import annotations

import sys

from mlx_swarm.cli import main


if __name__ == "__main__":
    sys.exit(main())
