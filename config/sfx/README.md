# Sound effects (SFX)

The default pack here is **synthesized offline by `tools/build_sfx.py`** with ffmpeg's
signal generators — royalty-free *by construction* (nothing to license or strike).
Regenerate any time with:

```bash
python tools/build_sfx.py
```

Files produced (48 kHz stereo WAV):

- `whoosh.wav` — transition accent on caption-screen changes / scene cuts
- `riser.wav` — tension build into the hook (first ~1.2 s)
- `pop.wav` — short click on emphasis words
- `ding.wav` — bright bell for a payoff/emphasis beat

`plan_effects.py` decides where each one lands (clip-relative cue times) and
`render_clip.py --cues <file>` mixes them **under** the voice (per-cue gain + the
existing loudnorm/duck path), so they punctuate without burying speech.

You can also drop your own short royalty-free SFX here under the same names to
override the synthesized ones. Keep them < ~1.5 s. WAV files in this folder are
gitignored (they're regenerable). **No copyrighted audio** — honors the project's
audio-rights rule.
