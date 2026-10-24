# What still needs YOU to check (not verified on a real Mac)

Everything here was built and tested on Linux with fakes for the outside services, plus real `ffmpeg`, real
`claude -p`, a real browser for the dashboard and a real clone of OpenShorts. These parts have **not** run
against the real thing yet. Do them in this order; each takes minutes.

| # | Check | How | If it fails |
|---|---|---|---|
| 1 | `doctor` is clean | `cinekahani doctor` | Install what it names |
| 2 | **VoiceStudio API** matches | Open VoiceStudio, `cinekahani voice check`, then `voice setup --wav ref.wav` | The client was written from VoiceStudio's source (`POST /generate`, `POST /profiles`), never run against it. Send me the error text. |
| 3 | **VoxCPM2 in VoiceStudio** | Install it per `docs/engines/voxcpm2.md` in the VoiceStudio repo, select it, narrate a Hindi script, **listen** | Hindi quality is unknown. Fallback engine: GPT-SoVITS. Read the model license text yourself before monetizing. |
| 4 | Hardware video encoder | `cinekahani build ...` on the Mac; check `encoder: auto` picked `h264_videotoolbox` | Set `video.encoder: libx264` |
| 5 | Hindi subtitles render | Watch a Hindi video's subtitles (conjuncts, matras) | Change `video.subtitles.font.hi` to a font you have |
| 6 | OpenShorts on Docker | `bash scripts/setup_openshorts.sh`, put `GEMINI_API_KEY` in `.env`, `cinekahani shorts make ...` | The Docker build was not run. The clip `start`/`end` fields are used to check trailer share; if OpenShorts omits them the Short is flagged "unverified". |
| 7 | TMDB real calls | Add `TMDB_API_KEY`, `cinekahani meta "Dune" --year 2021` | Mocked only. Also read TMDB's commercial-use terms. |
| 8 | YouTube upload | Create an OAuth *Desktop* client, put id/secret in `.env`, `youtube auth`, approve + schedule a throwaway video | A Google project that has not passed Google's audit can only upload **private** videos. `containsSyntheticMedia` is sent from memory of the API; if YouTube rejects it set `publish.youtube.synthetic_media_flag: false` and tick the box in YouTube Studio. |
| 9 | Instagram Reels | Do the README's *Instagram setup*, `cinekahani instagram whoami`, then approve a Short and `publish now ... --public` on a test account | Ported from reel-engine's working code, but never run from this project. Reels get re-encoded first; send me the error text if processing fails. |
| 10 | Timed publisher | Install the launchd job from the README; schedule a Short ~30 min ahead; check `logs/publish.log` | Ask me |
| 11 | Thermal pause | Long job on the Air; watch `pmset -g therm` | Raise `cooldown.between_chunks_s` |

## Decisions that are yours
- **Trailer footage** (`trailer fetch`) means downloading from YouTube, which its terms forbid, and studio
  trailers are copyrighted. Snippets are capped at 3 s / 20 %, but the risk is yours. `--no-trailer` avoids it.
- **Subtitles** use the script text with chunk-exact timing, so `faster-whisper` is not needed for your own
  videos (sentence boundaries *inside* a chunk are estimated).
- **Disclosure:** check YouTube's current rules on AI-generated voice and on repetitive or mass-produced content.
- The default description includes `package.ai_voice_note`. Edit or clear it in `config/studio.yaml`.
