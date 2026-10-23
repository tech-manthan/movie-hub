"""Keep a fanless MacBook Air from throttling: pause between heavy chunks."""
from __future__ import annotations

import re
import subprocess
import time
from typing import Callable

from .config import Cooldown
from .logs import get_logger

log = get_logger("thermal")


def cpu_speed_limit() -> int | None:
    """macOS: CPU_Speed_Limit from `pmset -g therm` (100 = no throttling). None if unknown."""
    try:
        out = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
    return int(m.group(1)) if m else None


def cool_down(cfg: Cooldown, *, sleep: Callable[[float], None] = time.sleep,
              probe: Callable[[], int | None] = cpu_speed_limit, max_extra_s: float = 600) -> float:
    """Sleep the configured gap; if the Mac reports throttling, keep waiting. Returns seconds slept."""
    slept = cfg.between_chunks_s
    if slept > 0:
        sleep(slept)
    if cfg.check_thermal:
        waited = 0.0
        while waited < max_extra_s:
            limit = probe()
            if limit is None or limit >= 100:
                break
            log.warning("Mac is throttling (CPU limit %d%%); pausing 20s", limit)
            sleep(20)
            waited += 20
            slept += 20
    return slept
