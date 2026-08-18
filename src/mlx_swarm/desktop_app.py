"""Electron process launcher for the standalone MLX Swarm app."""
# @lat: [[UI#Desktop shell]]

from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path

from .app_storage import default_app_root


def launch_electron(url: str) -> subprocess.Popen[bytes]:
    """Open the secure Electron shell and stop the API when it exits."""
    repository_root = Path(__file__).resolve().parents[2]
    executable = Path(
        os.environ.get(
            "MLX_SWARM_ELECTRON",
            repository_root / "node_modules" / ".bin" / "electron",
        )
    ).expanduser()
    main_script = repository_root / "desktop" / "main.cjs"
    if not executable.is_file():
        raise RuntimeError(
            "Electron is not installed. Run `npm install` in the MLX Swarm "
            "repository or set MLX_SWARM_ELECTRON."
        )
    if not main_script.is_file():
        raise RuntimeError(f"Desktop entrypoint not found: {main_script}")
    env = {
        **os.environ,
        "MLX_SWARM_APP_URL": url,
        "MLX_SWARM_CACHE_ROOT": str(default_app_root()),
    }
    for key in (
        "ELECTRON_RUN_AS_NODE",
        "ELECTRON_NO_ASAR",
        "ELECTRON_NO_ATTACH_CONSOLE",
    ):
        env.pop(key, None)
    workspace = os.environ.get("MLX_SWARM_WORKSPACE_ROOT")
    if workspace:
        env["MLX_SWARM_WORKSPACE_ROOT"] = workspace
    process = subprocess.Popen(
        [str(executable), str(main_script)],
        cwd=repository_root,
        env=env,
        shell=False,
    )

    def stop_when_window_closes() -> None:
        process.wait()
        if process.returncode is not None:
            os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(
        target=stop_when_window_closes,
        name="mlx-swarm-electron-watch",
        daemon=True,
    ).start()
    return process
