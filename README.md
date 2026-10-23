# Cine Kahani

A local pipeline that makes original YouTube videos, Shorts and Reels about movies and series, narrated in
your own cloned voice. Personal tool, built for a MacBook Air M4 (16 GB). **Nothing is published without your
approval, and nothing is published without a schedule or an explicit `--now`.**

| Phase | What | Command |
|---|---|---|
| 1 | TMDB metadata + scripts written by `claude -p` | `meta`, `script`, `approve` |
| 2 | Narration in your voice (VoiceStudio) | `voice setup`, `narrate` |
| 3 | Long 16:9 video (artwork, subtitles, music, optional snippets) | `music add`, `build` |
| 4 | Shorts/Reels (OpenShorts) | `shorts make` |
| 5 | Titles, description, tags, chapters, thumbnail | `package` |
| 6 | Review dashboard + upload | `review serve`, `publish run` |

Read `docs/owner-verification.md` first: it lists what has **not** been tried on a real Mac yet.

## Setup on your Mac

Needs Python 3.11+, `ffmpeg` (`brew install ffmpeg`), the Claude Code CLI (run `claude` once to log in),
Docker Desktop (Phase 4) and the VoiceStudio app (Phase 2).

```bash
git clone https://github.com/tech-manthan/movie-hub && cd movie-hub
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # fill keys as you reach each phase; .env is git-ignored
cinekahani doctor             # tells you what is ready and what is missing
pytest -q                     # ~100 tests, about 35 s
```

Never put keys in the YAML files or in chat. Settings live in `config/studio.yaml`; your personal overrides
(for example your voice profile id) go in `config/studio.local.yaml`, which is git-ignored.

## The whole flow, step by step

```bash
# Phase 1: facts and script  (use --sample until you have a TMDB key)
cinekahani meta "Inception" --year 2010            # or: --sample
cinekahani suggest                                 # which format to make next, so videos stay varied
cinekahani script inception-2010 -f ending_explained --lang both
#   -> projects/inception-2010/script/ending_explained.en.md  and  .hi.md   (open and rewrite freely)
cinekahani approve inception-2010 -f ending_explained -l en

# Phase 2: your voice (once), then narration
cinekahani voice setup --wav my-voice.wav --text "exactly what I said in the recording"
cinekahani narrate inception-2010 -f ending_explained -l en

# Phase 3: video
cinekahani music add ~/Downloads/track.mp3 --license cc0 --source https://... --mood tense   # see docs/music-sources.md
cinekahani build inception-2010 -f ending_explained -l en            # add --no-trailer for artwork only

# Phase 4: Shorts (starts and stops Docker for you)
bash scripts/setup_openshorts.sh                   # once; clones OpenShorts WITHOUT its cloud/ folder
cinekahani shorts make inception-2010 -f ending_explained -l en

# Phase 5: packaging
cinekahani package inception-2010 -f ending_explained -l en

# Phase 6: review, schedule, upload
cinekahani review serve                            # http://127.0.0.1:8765 : watch, edit, approve, schedule
cinekahani youtube auth                            # once
cinekahani publish run                             # uploads approved + scheduled items

cinekahani status                                  # where every video stands
```

`cinekahani status` shows `script > approved > narration > video > shorts > package > reviewed > published`
for each video with a `next:` hint.

## Phase details

### 1. Script
Six formats (`cinekahani formats`): review, ending explained, things you missed, cast facts, should you watch,
comparison (`--with "Second Title"`). Claude is told to be original, give real opinions and use only the TMDB facts.
- **`[VERIFY: claim]`** marks any fact Claude added from memory. You cannot approve a script until each tag is checked and removed.
- **`[VISUAL: poster]`** / `[VISUAL: trailer moment, ...]` are picture cues for Phase 3 and are never spoken.
- Your edits are safe: if inputs change later, `script` refuses to overwrite a hand-edited file unless you pass `--force`.

### 2. Narration
Calls VoiceStudio's local API (it stays a separate app so you can update it). Text is spoken in cached chunks,
so after a script edit only the changed sentences are re-spoken. A cooldown pause (and a check of the Mac's
throttle state) follows each chunk that was really generated.
**Voice engine:** use VoxCPM2 (Apache-2.0). VoiceStudio's default OmniVoice has non-commercial weights, so
`voice check` warns if it is selected.

### 3. Video
Artwork from TMDB with Ken Burns motion and poster cards, an intro and outro card, burned-in subtitles
(script text with chunk-exact timing), and music that ducks under your voice, normalized to -16 LUFS.
- **Variety:** four visual styles (noir, pop, clean, warm) rotate so consecutive videos differ; `cinekahani suggest`
  proposes the least recently used format.
- **Trailer snippets** are optional and off until you run `cinekahani trailer fetch SLUG --yes`
  (read the notice it prints). Limits live in `guardrails.trailer`: 3 s per snippet, 20 % of runtime,
  nothing continuous, narration at least 80 %. A plan that breaks them is refused. Exact moments:
  `projects/<slug>/visuals/trailer_moments.yaml` with `moments: [12.5, 40]`.
- **Chunked rendering:** each shot is rendered and cached on its own, with a cooldown after every `batch_size` shots.
- The render plan is `renders/plan.<fmt>.<lang>.json`; edit it by hand if you like, guardrails are re-checked.

### 4. Shorts and Reels
`scripts/setup_openshorts.sh` clones OpenShorts into `services/openshorts` with a sparse checkout that leaves out
`cloud/` (separate commercial license); the code refuses to run if `cloud/` exists. `shorts make` starts the
container, uploads your finished video, waits for the clips and stops Docker. Needs `GEMINI_API_KEY` in `.env`.
Shorts that are mostly trailer footage (over `shorts.max_trailer_share`) are rejected.
OpenShorts' ElevenLabs dubbing is not used: your narration is already made in Phase 2.

### 5. Packaging
`package.json` per video holds title options (each ends with `(English)` or `(Hindi)`), description (hook,
chapters, AI-voice note, music credit, TMDB credit line, hashtags), tags (including "सिने कहानी"), Shorts copy and
thumbnail text. The 1280x720 thumbnail uses Latin text (works everywhere); `cinekahani thumbnail` redraws it
after you edit `thumbnail_text`. Edits are protected like scripts.

### 6. Review and publish
- `review serve` opens a local-only dashboard (checks, player, editable text, approve/reject, schedule).
  Every change needs a per-run token and a local origin.
- An approval is tied to the exact video, thumbnail and text. **Editing anything afterwards withdraws it.**
- Approval is blocked while a check fails (script not approved, `[VERIFY]` left, guardrails, music license, title too long...).
- `publish schedule SLUG ITEM --at "2026-11-03 18:00" --tz Asia/Kolkata` then `publish run`: the video is uploaded
  now and the platform holds it until then. `publish now ... --yes` uploads immediately and private unless `--public`.
- Long videos go to YouTube directly (resumable upload, thumbnail, scheduled `publishAt`). Shorts/Reels go through
  Upload-Post by default, because Instagram's API needs a public video URL that a laptop does not have.
- One YouTube upload costs 1,600 of the 10,000 daily API quota units, so about 6 uploads a day.
- A Google Cloud project that has not passed Google's audit can only upload **private** videos.

## Project layout
```
config/studio.yaml        options and guardrail limits         .env   secrets (git-ignored)
src/cinekahani/
  core/      config, cache manifest, retry, thermal pauses, variety, guardrails, WAV helpers
  meta/      TMDB client and metadata step
  script/    formats, prompts, claude -p runner, parser, generate/approve
  voice/     VoiceStudio client, chunker, narration
  visuals/   styles, artwork, trailer snippets, shot planner
  assemble/  music library, subtitles, ffmpeg renderer, build step
  shorts/    OpenShorts client, Docker service, Shorts step
  package/   titles/description/tags/chapters and thumbnails
  review/    approval queue + local dashboard
  publish/   YouTube, Upload-Post, schedule state, publish gate
projects/<slug>/  metadata script audio visuals renders clips package manifest.json review.json publish.json
```
Each step is its own module and CLI command, runs alone, retries on API failures, logs to the terminal and
`logs/cinekahani.log` (`-v` for detail), and skips work whose inputs did not change.

## Rules we follow
- Original scripts with real opinions; your narration is the main content; trailer footage only supports it.
- Voice models must allow commercial use. Music only from the license log (no NC/ND).
- Credit lines (TMDB, channel) are added to every description automatically.
- Check YouTube's current rules on AI-generated voice and on repetitive/mass-produced content before uploading.
- TMDB's free API is for non-commercial use; read their terms before monetizing.
