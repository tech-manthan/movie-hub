import io
import math
import struct
import wave

import httpx


def tone(seconds=1.0, rate=24000, freq=440, channels=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(rate * seconds)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate))) * channels
                               for i in range(n)))
    return buf.getvalue()


class FakeVoiceStudio:
    """Stands in for VoiceStudio: seconds of audio = chars / 20."""

    def __init__(self, rate=24000):
        self.calls = []
        self.rate = rate
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, req):
        if req.url.path == "/profiles" and req.method == "GET":
            return httpx.Response(200, json=[{"id": "p1", "name": "me"}])
        if req.url.path == "/profiles" and req.method == "POST":
            assert b"ref_audio" in req.content
            return httpx.Response(200, json={"id": "p-new"})
        if req.url.path == "/generate":
            form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
            from urllib.parse import unquote_plus
            text = unquote_plus(form["text"])
            self.calls.append({k: unquote_plus(v) for k, v in form.items()})
            return httpx.Response(200, content=tone(max(len(text), 20) / 20, self.rate),
                                  headers={"X-Audio-Id": "a1", "X-Audio-Duration": "1.0"})
        return httpx.Response(404)
