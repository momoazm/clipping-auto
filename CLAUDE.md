# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in the
`clipping/` project. It follows the **WAT framework** (Workflows, Agents, Tools)
used across this repo: probabilistic AI reasons and orchestrates; deterministic
Python tools execute. Read the root `CLAUDE.md` for the shared WAT philosophy.

## This project: the `clipping/` pipeline

> **Run every tool with `clipping/` as the working directory.** `tools/_common.py`
> resolves `REPO_ROOT` as `tools/`'s parent, so `brand/`, `config/`, and `.tmp/`
> resolve from there, and API keys load from the shared **`API.env` at the repo root**
> (one level up — `_common` loads `../API.env`).

A single end-to-end pipeline: turn "clip this video into Shorts" into a set of
vertical, branded, captioned YouTube Shorts and upload them after explicit human
approval. The governing SOP is
[workflows/clipping_automation.md](workflows/clipping_automation.md) — **read it
before running the pipeline**; it owns the canonical tool-call sequence, edge cases,
and a living "Lessons learned" log.

> Sibling WAT project in this repo: `newsletter/` (same tool conventions, same shared
> root `API.env`). Don't edit it from here.

### Environment setup
```bash
cd clipping
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt
winget install Gyan.FFmpeg          # system ffmpeg+ffprobe (full build = libass for captions)
python tools/youtube_auth_setup.py  # one-time OAuth -> token.json (needs credentials.json)
```
Drop a bold caption font in `brand/fonts/` (see its README) and install it system-wide.

**Credentials:** search/LLM/transcription keys come from the shared **`API.env` at the
repo root** — don't make a per-project `.env`. The only project-local credentials are
the YouTube OAuth pair **`clipping/credentials.json`** (you provide it from Google Cloud
with the YouTube Data API v3 enabled) and **`clipping/token.json`** (created by
`youtube_auth_setup.py`). `credentials.json`, `token.json`, `.tmp/`, `.venv/`, and media
files are gitignored.

### Why this stack (important constraints)
- **Python 3.14 → API-first, no heavy local ML.** torch / mediapipe / faster-whisper /
  YOLO have no 3.14 wheels, so: transcription = **Groq Whisper API** (`whisper-large-v3`),
  all video = **ffmpeg**, face tracking = **OpenCV YuNet DNN** (`config/models/`, Haar
  fallback) with single-subject temporal tracking + zero-phase smoothing. Don't add
  torch/mediapipe without re-checking 3.14 wheels first.
- **ffmpeg must include libass** (caption burning) — the winget Gyan full build does.

### Tool conventions (every script in `tools/`)
- A standalone CLI, run as `python tools/<name>.py --flag ...` from `clipping/`. Tools
  import `_common` as a sibling (no `tools.` prefix), so they only work run this way.
- **Always prints exactly one JSON object to stdout.** Success → exit 0; failure → a
  JSON object with an `"error"` key and exit 1 (`_common.emit`/`fail`). Parse stdout
  either way. The `emit()` Windows `UnicodeEncodeError` ASCII fallback is intentional —
  don't remove it.
- ffmpeg/ffprobe are located via `_common.ffmpeg_bin()`/`ffprobe_bin()` (PATH, then an
  optional pip-bundled fallback). Subprocesses go through `_common.run()`, which raises
  with captured stderr on nonzero exit.
- External providers use **automatic fallback chains**: transcription = Groq (only
  provider wired); clip-selection LLM = Groq→Cerebras→Gemini→Mistral→OpenRouter. Surface
  a *whole-chain* failure before retrying; don't loop silently.

### Pipeline data flow
1. `probe_video.py` → validate the source (has audio, duration, resolution).
2. `transcribe_video.py` → Groq Whisper word timestamps → `.tmp/transcript.json`
   (auto-chunks audio over Groq's size limit).
3. `select_clips.py` → LLM virality scoring → `.tmp/clips.json` (spans snapped to word
   boundaries, clamped < 60 s, overlaps removed).
4. Per chosen clip: `reframe_crop.py` (OpenCV speaker-tracking pan → 9:16) →
   `build_captions.py` (word-by-word highlight `.ass`, clip-relative timing) →
   `render_clip.py` (burn captions, uniform speed, ducked music, loudnorm, final format).
5. `upload_youtube.py` — the **only irreversible step**. Dry-run preview without
   `--confirm`; publishes only with `--confirm`.
6. `upload_instagram.py` (optional, auto-enabled) — when `IG_ACCESS_TOKEN` +
   `IG_USER_ID` are present in `API.env`, `run_daily.py` also hosts the rendered clip
   at a public URL (`host_public.py`) and publishes it as an Instagram Reel after each
   real YouTube upload. No flag needed — detected at runtime so the GitHub Actions
   workflow file never needs editing (the `API_ENV` secret already lands in `API.env`
   verbatim). An IG failure is logged but never undoes the YouTube upload.

### Weekly IG style experiment (2026-07-12)
Clip `01` of whichever daily run happens first in a new ISO week TRIES a rotated caption style
(`tools/pick_weekly_style.py`: `hormozi` → `brand` → `clean`, `config/caption_styles.json`); the
slot is only actually claimed once that clip's Instagram post succeeds (see
`attempt_instagram_upload()` in `run_daily.py`), so a failed upload doesn't burn the week's only
experiment. Every successful IG post is logged to `state/ig_post_log.json`
(`_common.log_ig_post`). A separate Monday cron (`.github/workflows/style_experiment.yml` →
`tools/check_style_experiment.py`) resolves any experiment post ≥4 days old against a baseline of
recent normal posts (via `tools/ig_fetch_analytics.py`, Zernio's analytics API) and WhatsApps
Moemen (`tools/send_whatsapp.py`, CallMeBot — see `.claude/skills/send-whatsapp/SKILL.md`) if it
clearly won. **This never changes the live default style automatically** — a win is a
notification; Moemen decides whether to update `run_daily.py`'s default.

### Hard rules specific to this project
- **Branding is not optional.** The `brand` caption style and the `theme_gold` highlight
  pull from `brand/theme.json`; never re-derive brand colors per clip.
- **Never publish without explicit user confirmation.** `upload_youtube.py` is gated:
  always run it first as a dry run (no `--confirm`) to show the resolved **channel + title
  + privacy**, get approval, then re-run with `--confirm`.
- **Keep clip suffixes stable** across reframe → captions → render → upload for a given
  clip (e.g. `reframed_01.mp4` / `caps_01.ass` / `short_01.mp4`) so files don't collide.
- **ffmpeg filter files need bare filenames + `cwd`.** The repo path has a space and a
  drive colon that break in-filter `sendcmd`/`ass` paths; tools write those helpers into
  `.tmp/` and set the subprocess `cwd`. Preserve this.
- **Captions and speed only (v1).** No non-uniform silence-removal jump-cuts yet — they
  desync burned captions. Uniform `--speed` is sync-safe (applied to video + audio). See
  the SOP "Lessons learned" for the planned cut-map fix.
- **Audio rights.** Only use royalty-free music/SFX beds; copyrighted audio can get the
  Short muted or struck.

> Note: per the user's "edit only `clipping/`" constraint, the **root** `CLAUDE.md` is not
> updated to list `clipping/` as a sibling project. Add that line later if the constraint
> is lifted.
