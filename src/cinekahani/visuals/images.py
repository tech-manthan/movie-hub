"""Download TMDB artwork (cached) and compose poster cards and title cards with Pillow."""
from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..core.logs import get_logger
from ..core.project import Project
from ..core.retry import retry_call
from ..meta.models import TitleMeta
from .fonts import find_font
from .styles import Style

log = get_logger("images")


class ImageError(RuntimeError):
    pass


def _dest(project: Project, url: str) -> Path:
    ext = Path(url.split("?")[0]).suffix or ".jpg"
    return project.dir("visuals") / "images" / (hashlib.sha1(url.encode()).hexdigest()[:16] + ext)


def download_images(project: Project, urls: list[str], *, client: httpx.Client | None = None,
                    sleep=None) -> list[Path]:
    """Download missing images; returns local paths (skips ones that fail, with a warning)."""
    http = client or httpx.Client(timeout=60, follow_redirects=True)
    out: list[Path] = []
    for url in urls:
        dest = _dest(project, url)
        if not dest.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)

            def fetch(u=url):
                r = http.get(u)
                if r.status_code >= 500 or r.status_code == 429:
                    raise httpx.HTTPError(f"status {r.status_code}")
                r.raise_for_status()
                return r.content

            try:
                data = retry_call(fetch, attempts=3, base_delay=1, retry_on=(httpx.HTTPError,),
                                  sleep=sleep or __import__("time").sleep, what=f"download {url}")
                tmp = dest.with_suffix(".part")
                tmp.write_bytes(data)
                with Image.open(tmp) as im:
                    im.verify()
                tmp.replace(dest)
            except Exception as exc:                      # a bad image must not stop the whole video
                log.warning("skipping image %s: %s", url, exc)
                dest.with_suffix(".part").unlink(missing_ok=True)
                continue
        out.append(dest)
    return out


def collect_artwork(project: Project, meta: TitleMeta, *, client=None, sleep=None) -> dict[str, list[Path]]:
    if meta.sample:
        return {"posters": [], "backdrops": []}
    return {"posters": download_images(project, [i.url for i in meta.posters], client=client, sleep=sleep),
            "backdrops": download_images(project, [i.url for i in meta.backdrops], client=client, sleep=sleep)}


def _font(script: str, size: int):
    path = find_font(script)
    try:
        return ImageFont.truetype(path, size) if path else ImageFont.load_default(size)
    except OSError:
        return ImageFont.load_default(size)


def _cover(im: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / im.width, h / im.height)
    im = im.resize((int(im.width * scale) + 1, int(im.height * scale) + 1), Image.LANCZOS)
    left, top = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((left, top, left + w, top + h))


def poster_card(poster: Path, backdrop: Path | None, out: Path, w: int, h: int) -> Path:
    """Poster centred over a blurred, darkened backdrop (or the poster itself)."""
    with Image.open(backdrop or poster) as bg_src, Image.open(poster) as fg_src:
        bg = _cover(bg_src.convert("RGB"), w, h).filter(ImageFilter.GaussianBlur(28))
        bg = Image.eval(bg, lambda v: int(v * 0.45))
        fg = fg_src.convert("RGB")
        th = int(h * 0.82)
        fg = fg.resize((int(fg.width * th / fg.height), th), Image.LANCZOS)
        bg.paste(fg, ((w - fg.width) // 2, (h - th) // 2))
    out.parent.mkdir(parents=True, exist_ok=True)
    bg.save(out, quality=92)
    return out


def _centered(draw, text, font, cy, w, fill, maxw):
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if cur and draw.textlength(trial, font=font) > maxw:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    lines.append(cur)
    lh = font.size * 1.25
    y = cy - lh * len(lines) / 2
    for line in lines:
        draw.text(((w - draw.textlength(line, font=font)) / 2, y), line, font=font, fill=fill)
        y += lh


def title_card(out: Path, w: int, h: int, style: Style, lines: list[tuple[str, int, str]],
               backdrop: Path | None = None) -> Path:
    """lines: (text, size, 'fg' or 'accent'). Latin text only (card fonts are Latin)."""
    if backdrop and backdrop.is_file():
        with Image.open(backdrop) as src:
            img = _cover(src.convert("RGB"), w, h).filter(ImageFilter.GaussianBlur(10))
            img = Image.blend(img, Image.new("RGB", (w, h), style.card_bg), 0.72)
    else:
        img = Image.new("RGB", (w, h), style.card_bg)
    d = ImageDraw.Draw(img)
    k = h / 1080                              # sizes are designed for 1080p
    lines = [(t, max(8, round(sz * k)), tone) for t, sz, tone in lines]
    total = sum(s * 1.4 for _, s, _ in lines)
    y = h / 2 - total / 2
    for text, size, tone in lines:
        f = _font("latin", size)
        _centered(d, text, f, y + size * 0.7, w, style.accent if tone == "accent" else style.card_fg, w * 0.8)
        y += size * 1.4
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=94)
    return out
