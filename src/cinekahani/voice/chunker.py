from __future__ import annotations

import re

SENTENCE_END = re.compile(r"(?<=[.!?।॥…])\s+")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_END.split(" ".join(text.split())) if s.strip()]


def _hard_split(sentence: str, limit: int) -> list[str]:
    """A single over-long sentence: break at commas, then at spaces."""
    parts, cur = [], ""
    for piece in re.split(r"(?<=[,;:،])\s+", sentence):
        for word in piece.split(" "):
            if cur and len(cur) + 1 + len(word) > limit:
                parts.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}".strip()
    if cur:
        parts.append(cur)
    return parts


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Pack whole sentences into chunks of at most max_chars (long sentences are split)."""
    chunks: list[str] = []
    cur = ""
    for sentence in split_sentences(text):
        pieces = _hard_split(sentence, max_chars) if len(sentence) > max_chars else [sentence]
        for piece in pieces:
            if cur and len(cur) + 1 + len(piece) > max_chars:
                chunks.append(cur)
                cur = piece
            else:
                cur = f"{cur} {piece}".strip()
    if cur:
        chunks.append(cur)
    return chunks
