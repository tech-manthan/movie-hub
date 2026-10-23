"""Phase 2: turn an approved script into narration audio plus a timeline.

Work happens in chunks. Each chunk is cached by content hash, so a re-run after a
script edit only speaks the changed sentences, and the Mac gets a cooldown pause
after every chunk that was really generated.
"""
from __future__ import annotations

import json
import time
import wave
from dataclasses import dataclass
from typing import Callable

from ..core import wavutil
from ..core.cache import Decision
from ..core.config import Settings
from ..core.hashing import hash_file, hash_json, sha
from ..core.logs import get_logger
from ..core.project import Project
from ..core.thermal import cool_down, cpu_speed_limit
from ..script import parse
from ..script.generate import require_approved
from .chunker import chunk_text, split_sentences
from .client import VoiceError

log = get_logger("narrate")
VERSION = "1"


@dataclass
class NarrationResult:
    wav: object
    timeline: object
    decision: Decision
    duration: float = 0.0
    generated: int = 0
    cached: int = 0


def audio_paths(project: Project, fmt: str, lang: str):
    d = project.dir("audio")
    return d / f"narration.{fmt}.{lang}.wav", d / f"timeline.{fmt}.{lang}.json"


def _profile(settings: Settings, lang: str) -> str:
    pid = settings.voice.profile.get(lang) or settings.voice.profile.get("default")
    if not pid:
        raise VoiceError(f"No voice profile for '{lang}'. Record 10-15 s of your voice and run "
                         "`cinekahani voice setup --wav ref.wav --text \"what you said\"`.")
    return pid


def narrate(settings: Settings, project: Project, fmt: str, lang: str, client, *, force: bool = False,
            sleep: Callable[[float], None] = time.sleep, probe=cpu_speed_limit) -> NarrationResult:
    v = settings.voice
    script = require_approved(project, fmt, lang, settings.script.require_approval)
    _, body = parse.read_script(script)
    secs = parse.sections(body)
    if not any(s.text for s in secs):
        raise VoiceError(f"{script.name} has no narration text under '## ' headings")
    profile = _profile(settings, lang)
    wav_path, tl_path = audio_paths(project, fmt, lang)

    cfg_part = {"engine": v.engine, "profile": profile, "speed": v.speed, "seed": v.seed,
                "chunk": v.max_chunk_chars, "gap": v.chunk_gap_ms, "pause": v.pause_ms, "lang": lang, "v": VERSION}
    input_hash = hash_json({"script": sha(body.strip()), **cfg_part})
    key = f"narration:{fmt}:{lang}"
    manifest = project.manifest
    cur = hash_file(wav_path) if wav_path.is_file() and tl_path.is_file() else None
    decision = manifest.decide(key, input_hash, cur, force=force)
    if decision in (Decision.CACHED, Decision.EDITED_KEPT):
        log.info("%s is up to date; skipping", wav_path.name)
        dur = json.loads(tl_path.read_text(encoding="utf-8"))["duration"]
        return NarrationResult(wav_path, tl_path, decision, dur)

    chunk_dir = project.dir("audio") / "chunks" / lang
    chunk_dir.mkdir(parents=True, exist_ok=True)
    plan: list[tuple[int, int, list[str]]] = []          # (section, paragraph, chunks)
    for si, sec in enumerate(secs):
        for pi, para in enumerate(p for p in sec.text.split("\n\n") if p.strip()):
            plan.append((si, pi, chunk_text(para, v.max_chunk_chars)))
    total = sum(len(c) for _, _, c in plan)
    todo = {hash_json({"t": t, **cfg_part})[:20] for _, _, cs in plan for t in cs
            if not (chunk_dir / f"{hash_json({'t': t, **cfg_part})[:20]}.wav").is_file()}
    generated = cached = 0
    paths: dict[tuple[int, int, int], object] = {}
    n = 0
    for si, pi, chunks in plan:
        for ci, text in enumerate(chunks):
            n += 1
            ckey = hash_json({"t": text, **cfg_part})[:20]
            path = chunk_dir / f"{ckey}.wav"
            if path.is_file():
                cached += 1
            else:
                log.info("speaking chunk %d/%d (%d chars)", n, total, len(text))
                speech = client.generate(text, language=lang, profile_id=profile)
                tmp = path.with_suffix(".part")
                tmp.write_bytes(speech.wav)
                try:
                    wavutil.duration(tmp)
                except (wave.Error, EOFError) as exc:
                    tmp.unlink(missing_ok=True)
                    raise VoiceError(f"VoiceStudio sent audio that is not a 16-bit WAV: {exc}") from exc
                tmp.replace(path)
                generated += 1
                todo.discard(ckey)
                if todo:
                    cool_down(settings.cooldown, sleep=sleep, probe=probe)
            paths[(si, pi, ci)] = path

    first = next(iter(paths.values()))
    fmt0, _ = wavutil.read(first)
    out = bytearray()
    sections_out = []
    for si, sec in enumerate(secs):
        sec_start = len(out)
        paras_out = []
        paragraphs = [p for p in sec.text.split("\n\n") if p.strip()]
        for pi, para in enumerate(paragraphs):
            chunks = chunk_text(para, v.max_chunk_chars)
            p_start = len(out)
            sentences = []
            for ci, text in enumerate(chunks):
                path = paths[(si, pi, ci)]
                f, data = wavutil.read(path)
                if f != fmt0:
                    f2, data = wavutil.read(wavutil.conform(path, fmt0, path.with_suffix(".conf.wav")))
                c_start = len(out)
                out += data
                c0, c1 = c_start, len(out)
                sentences += _apportion(text, c0, c1, fmt0)
                if ci < len(chunks) - 1:
                    out += wavutil.silence(v.chunk_gap_ms, fmt0)
            paras_out.append({"text": para, "start": _t(p_start, fmt0), "end": _t(len(out), fmt0),
                              "sentences": sentences})
            if pi < len(paragraphs) - 1:
                out += wavutil.silence(v.pause_ms.get("paragraph", 450), fmt0)
        sections_out.append({"heading": sec.heading, "cues": sec.cues, "start": _t(sec_start, fmt0),
                             "end": _t(len(out), fmt0), "paragraphs": paras_out})
        if si < len(secs) - 1:
            out += wavutil.silence(v.pause_ms.get("section", 900), fmt0)

    wavutil.write(wav_path, fmt0, bytes(out))
    duration = _t(len(out), fmt0)
    tl_path.write_text(json.dumps({"lang": lang, "format": fmt, "duration": duration, "rate": fmt0.rate,
                                   "sections": sections_out, "timing": "chunk-exact, sentence-estimated"},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    manifest.record(key, input_hash, hash_file(wav_path), [wav_path.name, tl_path.name],
                    generated=generated, cached=cached)
    log.info("narration %s: %.0fs (%d chunks spoken, %d reused)", wav_path.name, duration, generated, cached)
    return NarrationResult(wav_path, tl_path, Decision.RUN, duration, generated, cached)


def _t(nbytes: int, f: wavutil.Fmt) -> float:
    return round(nbytes / (f.channels * f.width * f.rate), 3)


def _apportion(text: str, b0: int, b1: int, f: wavutil.Fmt) -> list[dict]:
    """Spread one chunk's exact time span over its sentences by character count."""
    sents = split_sentences(text) or [text]
    total = sum(len(s) for s in sents) or 1
    t0, t1 = _t(b0, f), _t(b1, f)
    out, pos = [], t0
    for i, s in enumerate(sents):
        end = t1 if i == len(sents) - 1 else round(pos + (t1 - t0) * len(s) / total, 3)
        out.append({"text": s, "start": round(pos, 3), "end": end})
        pos = end
    return out


def load_timeline(project: Project, fmt: str, lang: str) -> dict:
    _, tl = audio_paths(project, fmt, lang)
    if not tl.is_file():
        raise FileNotFoundError(f"{tl.name} is missing. Run `cinekahani narrate {project.slug} "
                                f"--format {fmt} --lang {lang}` first.")
    return json.loads(tl.read_text(encoding="utf-8"))
