"""Settings: config/studio.yaml for options, .env for secrets."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Channel(BaseModel):
    name: str = "Cine Kahani"
    studio_credit: str = ""


class Paths(BaseModel):
    projects: str = "projects"
    music: str = "music"
    services: str = "services"
    logs: str = "logs"


class Tmdb(BaseModel):
    locale: dict[str, str] = {"hi": "hi-IN", "en": "en-US"}
    max_age_days: int = 30


class Llm(BaseModel):
    command: list[str] = ["claude", "-p"]
    model: str | None = None
    timeout_s: int = 900
    retries: int = 2
    retry_delay_s: float = 5


class ScriptCfg(BaseModel):
    require_approval: bool = True
    words_per_minute: dict[str, int] = {"en": 150, "hi": 140}
    target_minutes: dict[str, int] = {
        "review": 8, "ending_explained": 8, "things_you_missed": 7,
        "cast_facts": 6, "should_you_watch": 5, "comparison": 8,
    }
    length_tolerance: tuple[float, float] = (0.6, 1.4)


class TrailerGuard(BaseModel):
    enabled: bool = True
    max_snippet_seconds: float = 3
    max_share_of_runtime: float = 0.20
    max_continuous_seconds: float = 3
    min_narration_share: float = 0.80


class Guardrails(BaseModel):
    trailer: TrailerGuard = TrailerGuard()
    vary_formats: bool = True


class Cooldown(BaseModel):
    between_chunks_s: float = 20
    check_thermal: bool = True


class Settings(BaseModel):
    channel: Channel = Channel()
    paths: Paths = Paths()
    languages: list[str] = ["hi", "en"]
    hindi_script: str = "devanagari"
    tmdb: Tmdb = Tmdb()
    llm: Llm = Llm()
    script: ScriptCfg = ScriptCfg()
    guardrails: Guardrails = Guardrails()
    cooldown: Cooldown = Cooldown()
    root: Path = Field(default=Path("."), exclude=True)
    env: dict[str, str] = Field(default_factory=dict, exclude=True)

    def path(self, name: str) -> Path:
        return self.root / getattr(self.paths, name)

    @property
    def credit_line(self) -> str:
        parts = [f"{self.channel.name}."]
        if self.channel.studio_credit:
            parts.append(self.channel.studio_credit)
        parts.append("Movie and series data from TMDB. This product uses the TMDB API "
                     "but is not endorsed or certified by TMDB.")
        return " ".join(parts)


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("\"'")
    return out


def load_settings(root: Path | str = ".", config_file: str = "config/studio.yaml") -> Settings:
    """Load YAML (optional) plus .env. Real environment variables win over .env."""
    root = Path(root).resolve()
    data: dict = {}
    cfg = root / config_file
    if cfg.is_file():
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    local = root / "config" / "studio.local.yaml"
    if local.is_file():
        data = _merge(data, yaml.safe_load(local.read_text(encoding="utf-8")) or {})
    settings = Settings(**data)
    settings.root = root
    env = parse_env_file(root / ".env")
    env.update({k: v for k, v in os.environ.items() if k.startswith(("TMDB_", "CINEKAHANI_"))})
    settings.env = env
    return settings


def _merge(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], value)
        else:
            base[key] = value
    return base
