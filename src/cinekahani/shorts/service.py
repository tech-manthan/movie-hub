"""Start and stop OpenShorts (Docker) around a job, so a 16 GB Mac is not running everything at once."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from ..core.config import Settings
from ..core.logs import get_logger
from .client import OpenShortsClient, ShortsError

log = get_logger("openshorts.service")


def compose_dir(settings: Settings) -> Path:
    d = settings.root / settings.shorts.compose_dir
    if not (d / "docker-compose.yml").is_file():
        raise ShortsError(f"OpenShorts is not set up at {d}. Run scripts/setup_openshorts.sh first.")
    if (d / "cloud").exists():
        raise ShortsError(f"{d}/cloud exists. It has a separate commercial license; remove it "
                          "(scripts/setup_openshorts.sh never checks it out).")
    return d


def start(settings: Settings, client: OpenShortsClient, *, runner=subprocess.run, sleep=time.sleep,
          wait_s: int = 600) -> bool:
    """Returns True if we started it (so the caller knows to stop it again)."""
    if client.healthy():
        return False
    d = compose_dir(settings)
    log.info("starting OpenShorts (first run builds a Docker image and can take a while)")
    try:
        p = runner(settings.shorts.start_command, cwd=d, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ShortsError("Docker is not installed or not on PATH (install Docker Desktop).") from exc
    if p.returncode != 0:
        raise ShortsError(f"Could not start OpenShorts: {(p.stderr or '').strip()[-400:]}")
    waited = 0
    while waited < wait_s:
        if client.healthy():
            return True
        sleep(5)
        waited += 5
    raise ShortsError(f"OpenShorts did not become healthy within {wait_s}s")


def stop(settings: Settings, *, runner=subprocess.run) -> None:
    d = compose_dir(settings)
    runner(settings.shorts.stop_command, cwd=d, capture_output=True, text=True)
    log.info("OpenShorts stopped")
