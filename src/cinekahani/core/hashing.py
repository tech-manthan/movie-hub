from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_json(obj: Any) -> str:
    return sha(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str))


def hash_file(path: Path) -> str:
    return sha(path.read_bytes())
