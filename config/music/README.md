# Background music beds

The default beds here are **synthesized offline by `tools/build_music.py`** (ffmpeg
oscillators) — royalty-free by construction, no licensing/striking risk. Regenerate with:

```bash
python tools/build_music.py
```

Files produced (subtle, low under-beds — *not* foreground music):

- `cinematic_tension.mp3` — low dramatic minor drone, for tense/high-stakes clips
- `ambient_glow.mp3` — gentler bright pad, for calmer talking-head clips

`render_clip.py --music auto` picks one (or pass `--music config/music/<file>`); it's
ducked beneath speech via sidechain compression and loudness-normalized so the voice
stays on top. For loud/energetic source audio (e.g. crowd/commentary), music is best left
**off** — the SFX layer carries the sound design instead.

Drop your own CC0 / royalty-free tracks here to override (Pixabay Music, Mixkit, YouTube
Audio Library). Keep them longer than your clips so they aren't looped. Audio files here
are gitignored. **No copyrighted audio** — honors the project's audio-rights rule.
