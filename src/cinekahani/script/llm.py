"""Runs the Claude Code CLI (`claude -p`) with the prompt on stdin."""
from __future__ import annotations

import shutil
import subprocess
import time
from typing import Callable

from ..core.config import Llm
from ..core.logs import get_logger
from ..core.retry import retry_call

log = get_logger("llm")


class LlmError(RuntimeError):
    pass


class ClaudeCli:
    def __init__(self, cfg: Llm, *, runner: Callable | None = None, sleep=time.sleep):
        self.cfg = cfg
        self.runner = runner or subprocess.run
        self.sleep = sleep

    @property
    def model_name(self) -> str:
        return self.cfg.model or "default"

    def __call__(self, prompt: str) -> str:
        exe = self.cfg.command[0]
        if self.runner is subprocess.run and shutil.which(exe) is None:
            raise LlmError(f"'{exe}' was not found. Install Claude Code and run `claude` once to log in, "
                           "or change llm.command in config/studio.yaml.")
        cmd = list(self.cfg.command)
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]

        def once() -> str:
            try:
                p = self.runner(cmd, input=prompt, capture_output=True, text=True,
                                timeout=self.cfg.timeout_s)
            except subprocess.TimeoutExpired as exc:
                raise LlmError(f"claude timed out after {self.cfg.timeout_s}s") from exc
            if p.returncode != 0:
                raise LlmError(f"claude exited with {p.returncode}: {(p.stderr or '').strip()[:500]}")
            out = (p.stdout or "").strip()
            if not out:
                raise LlmError("claude returned no text")
            return out

        return retry_call(once, attempts=self.cfg.retries + 1, base_delay=self.cfg.retry_delay_s,
                          retry_on=(LlmError,), sleep=self.sleep, what="claude -p")
