# Cine Kahani

A local pipeline that makes original YouTube videos, Shorts and Reels about movies and web series,
narrated in your own voice. Personal tool, runs on a MacBook Air M4. **Nothing is published without your approval.**

**Status: Phase 1 of 6.** TMDB fetch and script writing work. Voice, video, Shorts, packaging and publishing come next.

| Phase | What | State |
|---|---|---|
| 1 | TMDB metadata + scripts (`claude -p`) | done, ready to test |
| 2 | Narration with VoiceStudio (your cloned voice) | next |
| 3 | Long video assembly (FFmpeg) | |
| 4 | Shorts/Reels via OpenShorts | |
| 5 | Titles, descriptions, thumbnails | |
| 6 | Review dashboard + publishing | |

## Setup on your Mac

You need Python 3.11+ and the Claude Code CLI (log in once by running `claude` in a terminal).

```bash
git clone https://github.com/tech-manthan/movie-hub && cd movie-hub
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cinekahani doctor            # tells you what is missing
pytest -q                    # 35 tests, under a second
```

TMDB comes later. Until then use `--sample`, which uses built-in data for *Inception*.
When you have a key: `cp .env.example .env` and fill in `TMDB_API_KEY` (or `TMDB_READ_TOKEN`).
`.env` is git-ignored. Never put keys in the YAML file or in chat.

> TMDB's free API is meant for non-commercial use. Read their terms before the channel is monetized.

## Run Phase 1

```bash
cinekahani meta "Inception" --sample          # real titles: cinekahani meta "Inception" --year 2010
cinekahani formats                            # the six script formats
cinekahani script inception-2010 --format review --lang both
```

This writes `projects/inception-2010/script/review.en.md` and `review.hi.md`. Open them and rewrite freely.

```bash
cinekahani status                             # words, status, unchecked claims per script
cinekahani approve inception-2010 --format review --lang en
```

Comparison needs a second title: `cinekahani script inception-2010 -f comparison --with "Interstellar"`.

### What a script file looks like

```
---
title: Inception
format: review
language: en
status: draft          # becomes "approved" with `cinekahani approve`
words: 1180
---
## Hook
[VISUAL: poster]       <- a picture cue for Phase 3, never read aloud
Narration text, written to be spoken...
```

- **`[VERIFY: claim]`** marks a fact Claude added from its own memory instead of from TMDB. You cannot approve
  a script until every tag is checked and removed (keep the claim or delete it). This is how invented trivia is stopped.
- **Your edits are safe.** If you edit a script and the inputs later change, `cinekahani script` refuses to overwrite it
  unless you pass `--force`. If nothing changed, it does not call Claude again.
- Length is a target, not a rule. "Too short" triggers one automatic retry; "too long" is only a warning.

## How it works

```
config/studio.yaml   options and guardrail limits      .env   secrets (git-ignored)
src/cinekahani/
  core/    config, cache manifest, retry, logging, project folders, guardrails
  meta/    TMDB client and metadata step
  script/  formats, prompt, claude -p runner, parser, generate/approve
projects/<movie-slug>/  metadata/ script/ audio/ visuals/ renders/ clips/ manifest.json
```

- **Caching:** `manifest.json` stores a hash of each step's inputs and output. Re-running skips unchanged steps.
- **Retries:** TMDB calls retry with backoff on network errors, 429 and 5xx. `claude -p` retries on failure.
- **Logs:** printed to the terminal and appended to `logs/cinekahani.log`. Add `-v` for detail.
- **Guardrails** (`guardrails:` in the YAML): trailer clips max 3 s, max 20% of runtime, no continuous section over 3 s,
  narration at least 80%. `core/guardrails.py` checks a plan against these; Phase 3 will refuse to render a plan that fails.
- **Channel name and credit line** come from the YAML. Every description will carry the TMDB credit.
- **Hindi script:** Devanagari by default. Set `hindi_script: hinglish` for Latin letters.

## Rules we follow

- Scripts must be original, with real opinions. Claude is told to avoid copying reviews or Wikipedia.
- Narration is the main content. Trailer footage only supports it (Phase 3, off if `guardrails.trailer.enabled: false`).
- Voice models must allow commercial use. Plan: VoxCPM2 (Apache-2.0). Do not use OmniVoice, the VoiceStudio default,
  because its weights are non-commercial. The license text still needs a read before Phase 2 goes live.
- Music: only CC0 / royalty-free tracks, each logged with source and license. Avoid non-commercial licenses.
- Check YouTube's rules on synthetic voice disclosure and on repetitive or templated content before you upload.
