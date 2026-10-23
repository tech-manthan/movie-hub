from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .cache import Manifest

SUBDIRS = ["metadata", "script", "audio", "visuals", "renders", "clips"]


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "untitled"


@dataclass
class Project:
    root: Path

    @property
    def slug(self) -> str:
        return self.root.name

    def dir(self, name: str) -> Path:
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def manifest(self) -> Manifest:
        return Manifest(self.root / "manifest.json")

    @classmethod
    def create(cls, projects_dir: Path, slug: str) -> "Project":
        proj = cls(projects_dir / slug)
        for sub in SUBDIRS:
            (proj.root / sub).mkdir(parents=True, exist_ok=True)
        return proj

    @classmethod
    def open(cls, projects_dir: Path, slug: str) -> "Project":
        root = projects_dir / slug
        if not root.is_dir():
            known = sorted(p.name for p in projects_dir.iterdir()) if projects_dir.is_dir() else []
            hint = f" Known projects: {', '.join(known)}." if known else " Run `cinekahani meta` first."
            raise FileNotFoundError(f"No project '{slug}'.{hint}")
        return cls(root)
