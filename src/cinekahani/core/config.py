"""Settings: config/studio.yaml for options, .env for secrets."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


ENV_PREFIXES = ("TMDB_", "CINEKAHANI_", "GEMINI_", "OMNIVOICE_", "YOUTUBE_", "UPLOADPOST_")


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


class VoiceCfg(BaseModel):
    base_url: str = "http://127.0.0.1:3900"
    engine: str = "voxcpm2"          # Apache-2.0 weights; never the non-commercial default
    profile: dict[str, str] = {}     # language -> VoiceStudio profile id (set by `voice setup`)
    speed: float = 1.0
    seed: int = 42
    max_chunk_chars: int = 350
    chunk_gap_ms: int = 120
    pause_ms: dict[str, int] = {"paragraph": 450, "section": 900}
    timeout_s: int = 900
    retries: int = 2
    retry_delay_s: float = 5
    start_command: list[str] = []    # optional: command that launches the VoiceStudio backend
    stop_command: list[str] = []


class SubtitleCfg(BaseModel):
    enabled: bool = True
    align: str = "proportional"      # proportional | whisper (needs faster-whisper or mlx-whisper)
    max_chars: dict[str, int] = {"en": 42, "hi": 34}
    font: dict[str, str] = {"en": "Arial", "hi": "Kohinoor Devanagari"}
    size: int = 54


class MusicCfg(BaseModel):
    volume: float = 0.10
    fade_s: float = 3
    allowed_licenses: list[str] = ["cc0", "pixabay", "royalty-free", "cc-by", "public-domain", "own"]
    avoid_recent: int = 3            # do not reuse a track used in the last N videos


class VideoCfg(BaseModel):
    width: int = 1920
    height: int = 1080
    fps: int = 30
    encoder: str = "auto"            # auto | libx264 | h264_videotoolbox
    crf: int = 20
    bitrate: str = "8M"              # used by the hardware encoder
    shot_seconds: tuple[float, float] = (5, 9)
    intro_seconds: float = 3
    outro_seconds: float = 5
    batch_size: int = 6              # shots rendered before a cooldown pause
    subtitles: SubtitleCfg = SubtitleCfg()
    music: MusicCfg = MusicCfg()
    trailer_download_command: list[str] = ["yt-dlp", "-f", "mp4/best[height<=1080]", "-o"]


class ShortsCfg(BaseModel):
    base_url: str = "http://127.0.0.1:8000"
    compose_dir: str = "services/openshorts"
    target_clips: int = 4
    min_seconds: int = 20
    max_seconds: int = 55
    auto_hook: bool = True
    captions: bool = True
    max_trailer_share: float = 0.25  # a Short may contain at most this much trailer footage
    poll_seconds: float = 10
    timeout_s: int = 3600
    start_command: list[str] = ["docker", "compose", "up", "-d", "--build", "backend"]
    stop_command: list[str] = ["docker", "compose", "down"]


class PackageCfg(BaseModel):
    title_options: int = 5
    ai_voice_note: str = "Narration: my own voice, recreated with AI voice cloning."
    hashtags: int = 3
    thumbnail_text_script: str = "latin"     # latin | native (native needs Pillow with raqm)


class YoutubeCfg(BaseModel):
    category_id: str = "24"          # Entertainment
    privacy: str = "private"         # uploads stay private until you schedule or publish
    synthetic_media_flag: bool = True    # sets status.containsSyntheticMedia; set false if the API rejects it
    chunk_mb: int = 8
    made_for_kids: bool = False


class UploadPostCfg(BaseModel):
    user: str = ""
    platforms: list[str] = ["instagram", "youtube"]
    timezone: str = "Asia/Kolkata"


class PublishCfg(BaseModel):
    youtube: YoutubeCfg = YoutubeCfg()
    uploadpost: UploadPostCfg = UploadPostCfg()
    review_port: int = 8765


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
    voice: VoiceCfg = VoiceCfg()
    video: VideoCfg = VideoCfg()
    shorts: ShortsCfg = ShortsCfg()
    package: PackageCfg = PackageCfg()
    publish: PublishCfg = PublishCfg()
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
    env.update({k: v for k, v in os.environ.items() if k.startswith(ENV_PREFIXES)})
    settings.env = env
    return settings


def _merge(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], value)
        else:
            base[key] = value
    return base


def save_local(root: Path | str, patch: dict) -> Path:
    """Merge `patch` into config/studio.local.yaml (git-ignored personal overrides)."""
    path = Path(root) / "config" / "studio.local.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    path.write_text(yaml.safe_dump(_merge(data or {}, patch), sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    return path
