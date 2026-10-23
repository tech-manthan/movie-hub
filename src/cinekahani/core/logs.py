from __future__ import annotations

import logging
from pathlib import Path

LOGGER = "cinekahani"


def setup_logging(log_dir: Path | None = None, verbose: bool = False) -> logging.Logger:
    log = logging.getLogger(LOGGER)
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s  %(message)s"))
    log.addHandler(console)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "cinekahani.log", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        log.addHandler(fh)
    return log


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER}.{name}" if name else LOGGER)
