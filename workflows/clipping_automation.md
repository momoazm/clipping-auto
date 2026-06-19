# Workflow: Clip a video into viral YouTube Shorts

## Objective

Turn one long horizontal video the user provides into a small set of vertical
(9:16) YouTube Shorts that look native to the platform — auto-selected best
moments, speaker-tracking reframe, animated word-by-word captions, a ducked music
bed, and loudness-normalized audio — then upload them to YouTube after the user
approves at the gate. Modeled on the techniques of top clipping accounts (Crayo /
Opus style): strong 3-second hook, big legible captions, on-brand highlight color.

## Required inputs
- **Source video** path (`--in`). Any ffmpeg-readable file with an audio track.
- **How many clips** (`--count`, default 3).
- **Caption style** (`--style`): `hormozi` (default), `brand`, or `clean`.
- **Target / max length** (`--target-secs` 45, `--max-secs` 59). Never exceed 59 s.
- **Speed** ramp (optional, `--speed`, e.g. 1.1). Default 1.0.
- **Music bed** (optional): a file in `config/music/`.
- **Privacy**: `public` (user's choice). Title/description hints from the user or the
  LLM's `suggested_title`.

## Branding (always apply — not optional)
Captions pull the highlight color from `brand/theme.json` when the `brand` style is
used, and the `theme_gold` token resolves to `colors.gold`. Never hard-code brand
colors per clip — they come from the theme file. The caption font lives in
`brand/fonts/` (see its README; install the .ttf system-wide so libass finds it).

## Tool-call sequence
Run everything from `clipping/` as the working directory. Every tool prints one
JSON object to stdout — parse it before moving on. Intermediates land in `.tmp/`.

1. **Probe** the source so you fail fast on bad files:
   `python tools/probe_video.py --in <video>`
   → confirm `has_audio: true` and sane `duration`/`width`/`height`.

2. **Transcribe** to word timestamps (Groq Whisper):
   `python tools/transcribe_video.py --in <video>`
   → writes `.tmp/transcript.json`. Long videos are auto-chunked.

3. **Select clips** (LLM virality scoring):
   `python tools/select_clips.py --count 3 --target-secs 45`
   → writes `.tmp/clips.json` with `[{start,end,hook,reason,virality_score,
   suggested_title,emphasis_words}]`, snapped to word boundaries and clamped < 60 s.
   Show the user the proposed clips (hook + score) before rendering all of them.

4. **For each chosen clip** (use a unique suffix per clip, e.g. `_01`):
   a. **Reframe** to vertical, tracking the speaker:
      `python tools/reframe_crop.py --in <video> --start S --end E --out .tmp/reframed_01.mp4`
      → `mode: track` ideally; `letterbox` if no face / narrow source.
   b. **Captions** for the same span (clip-relative timing):
      `python tools/build_captions.py --start S --end E --style hormozi --out .tmp/caps_01.ass`
   c. **Render** the final Short:
      `python tools/render_clip.py --in .tmp/reframed_01.mp4 --captions .tmp/caps_01.ass \
       --out .tmp/short_01.mp4 [--speed 1.1] [--music config/music/<bed>.mp3]`
      → 1080×1920, ≤59 s, H.264/AAC, faststart.

5. **Preview + confirmation gate (mandatory).** Show the user the rendered file
   path(s) and the resolved upload details by running the uploader as a DRY RUN
   (no `--confirm`):
   `python tools/upload_youtube.py --video .tmp/short_01.mp4 --title "<title>" --privacy public`
   → echoes `channel_title`, `title`, `privacy`. **Echo the resolved channel to the
   user and get explicit approval before publishing.**

6. **Upload** (irreversible) only after approval, adding `--confirm`:
   `python tools/upload_youtube.py --video .tmp/short_01.mp4 --title "<title>" \
    --description "<desc>" --tags shorts,<...> --privacy public --confirm`
   → returns the `url`. Mind the ~6 uploads/day quota.

## Edge cases
- **No audio track** → `transcribe_video` fails clearly; can't caption. Ask the user
  for a different source.
- **ffmpeg/ffprobe missing** → every video tool fails with the winget hint. Install
  `Gyan.FFmpeg` (full build, has libass) and retry.
- **Groq audio size limit** → `transcribe_video` auto-chunks by file size and shifts
  timestamps; no action needed, but very long videos take longer.
- **LLM returns a span > 59 s or off-boundary** → `select_clips` snaps to words and
  hard-clamps to `--max-secs`. Overlapping clips are de-duplicated (higher score wins).
- **No face detected / source already vertical** → `reframe_crop` falls back to a
  blurred letterbox (see its `note`); the clip still renders.
- **Caption font not installed** → libass falls back to a default font; captions still
  burn, just not in the intended typeface. Install the `.ttf` from `brand/fonts/`.
- **YouTube quota exceeded** (~6 uploads/day) → upload fails; wait for the midnight
  Pacific reset or use a second Cloud project.
- **Token expired/revoked** → uploader auto-refreshes; if that fails, re-run
  `youtube_auth_setup.py`.
- **Music you don't own** → can get the Short muted/struck. Only use royalty-free beds.

## Lessons learned (update this section as you go)
- **Python 3.14 / dependency choice (2026-06-18):** heavy local-ML wheels (torch,
  mediapipe, faster-whisper/ctranslate2, YOLO) had no 3.14 builds, so the pipeline is
  deliberately API-first: **Groq Whisper** for transcription, **ffmpeg** for all video,
  **OpenCV (Haar)** for face tracking. Don't reintroduce torch/mediapipe without
  re-checking 3.14 wheels.
- **ffmpeg filter path escaping (2026-06-18):** the repo path contains a space and a
  drive colon, which break the `sendcmd`/`ass` filter file arguments. Workaround:
  write those helper files into `.tmp/` and run ffmpeg with `cwd` set so the filter
  references a bare filename (no colon/space). `reframe_crop.py` and `render_clip.py`
  both rely on this — preserve it.
- **Caption ↔ speed sync (2026-06-18):** captions are burned BEFORE `setpts`, and the
  speed factor is applied uniformly to video (`setpts`) and audio (`atempo`), so sync
  holds at any speed. **Silence-removal jump-cuts are intentionally NOT done in v1**
  because they shift word timing non-uniformly and would desync burned captions. The
  planned fix: have a silence-removal step emit an old→new time cut-map and regenerate
  captions against the new timeline before burning. Until then, only uniform `--speed`.
- **Upload safety (2026-06-18):** `upload_youtube.py` refuses to publish without
  `--confirm` and otherwise prints a dry-run preview. Use the dry run as the gate gesture.
- **render_clip truncation gotcha (2026-06-18):** `render_clip.py` defaults to
  `--max-secs 59` and silently trims longer clips. When clips may exceed 59 s (user
  wanted ≤2 min), pass `--max-secs 120`. The reframe + captions are full length; only
  the final mux truncates, so re-rendering with the right `--max-secs` (reusing the
  existing `reframed_*.mp4`) is cheap.
- **Reframe lag → zero-phase smoothing (2026-06-18):** first run's pan felt laggy. A
  causal EMA always trails the subject. Fixed by running the EMA forward+backward
  (zero phase) in `reframe_crop.build_pan_commands`, raising `CMD_FPS` to 15 and
  `EMA_ALPHA` to 0.25.
- **Reframe v2: detection + tracking + per-frame pan (2026-06-18):** user still saw
  some lag and clip 4 "didn't recognize the speaker at the start" (Haar misses
  small/profile/off-center faces). Upgrades: (a) **YuNet DNN detector**
  (`config/models/face_detection_yunet_2023mar.onnx`, Haar fallback) — locks onto the
  speaker from frame 0; validated 47/48 sampled frames on clip 4's opening. (b)
  **single-subject tracking** — follow the face nearest the previous pick (reseed to
  the most prominent face on a big jump/scene cut) instead of "largest face per frame",
  which is what made the crop hop between people. (c) `CMD_FPS` → 30 (per-frame
  keyframes, no staircase stutter), `DETECT_FPS` → 6, `DETECT_WIDTH` → 640. Still
  hardest on fast-cut multi-person content; best on talking-head/podcast.
- **Transcription accuracy (2026-06-18):** captions were weak on noisy/crowd audio.
  Switched default model to `whisper-large-v3` (was `-turbo`) and added a light audio
  pre-clean (`highpass=f=90,dynaudnorm`) in `transcribe_video.extract_audio`. Override
  the model with `--model` / `GROQ_WHISPER_MODEL`.
- **URL input (2026-06-18):** added `download_video.py` (yt-dlp). It points yt-dlp at
  our ffmpeg via `ffmpeg_location` and uses `quiet/noprogress` to avoid flooding stdout.
- **Email delivery (2026-06-18):** user wants finished clips emailed (not uploaded).
  Full-res 1080×1920 clips are far over Gmail's 25 MB limit, so compress to ~720p
  (`scale=-2:1280`, CRF 30) and batch attachments under ~22 MB/message (multiple emails
  as needed). Reuse the `competitor/` Gmail sender (its `token.json` has `gmail.send`;
  clipping has no Gmail OAuth). Gmail uploads on a slow uplink time out — set a long
  `socket` timeout and retry. Keep the originals in `.tmp/` for posting/upload.
