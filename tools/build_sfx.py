"""Synthesize a small royalty-free SFX pack with ffmpeg (no external assets).

Pipeline role: one-time generator for the sound-design layer. Top clipping
accounts lean on a few transition/emphasis sounds -- a whoosh on scene/caption
changes, a riser into the hook, a pop/ding on emphasis. Synthesizing them from
ffmpeg's signal generators (sine / anoisesrc / aevalsrc) keeps the pipeline
API-first and 100% royalty-free *by construction* -- there is nothing to license
or strike (honors the project's audio-rights rule). render_clip.py mixes these
under the voice via plan_effects.py cues.

Run once (re-run to regenerate):
    python tools/build_sfx.py

Prints JSON: {"dir": "<config/sfx>", "created": [{"name","path","bytes"}, ...]}
"""
import os

from _common import emit, fail, run, ffmpeg_bin, REPO_ROOT, FFmpegMissing

SR = 48000

# name -> (lavfi source string, audio-filter chain). Each renders a short stereo
# wav. Levels are kept modest here; render_clip applies a further per-cue gain so
# the effects sit UNDER the voice rather than on top of it.
SFX = {
    # Punchy click for emphasis words -- short sine with a fast exponential decay.
    "pop": (
        f"sine=frequency=660:duration=0.09:sample_rate={SR}",
        "afade=t=in:d=0.004,afade=t=out:st=0.01:d=0.08:curve=exp,volume=0.7",
    ),
    # Bright bell for a payoff/emphasis beat -- higher sine, longer decaying tail.
    "ding": (
        f"sine=frequency=1320:duration=0.7:sample_rate={SR}",
        "afade=t=in:d=0.004,afade=t=out:st=0.05:d=0.65:curve=exp,volume=0.5",
    ),
    # Transition whoosh -- band-limited brown noise that swells up then away
    # (fade-in immediately followed by fade-out is what reads as a "whoosh").
    "whoosh": (
        f"anoisesrc=color=brown:duration=0.5:sample_rate={SR}:amplitude=0.7",
        "highpass=f=250,lowpass=f=5000,afade=t=in:d=0.25:curve=tri,"
        "afade=t=out:st=0.25:d=0.25,volume=1.2",
    ),
    # Tension riser into the hook -- linear chirp 200Hz->~920Hz over 1.2s.
    # Instantaneous phase of a linear sweep f0+k*t is 2*pi*(f0*t + 0.5*k*t^2).
    "riser": (
        f"aevalsrc='0.4*sin(2*PI*(200*t + 0.5*600*t*t))':duration=1.2:sample_rate={SR}",
        "afade=t=in:d=0.15,afade=t=out:st=1.0:d=0.2,volume=0.6",
    ),
}


def main():
    try:
        ffmpeg = ffmpeg_bin()
    except FFmpegMissing as e:
        fail(str(e))
        return

    out_dir = REPO_ROOT / "config" / "sfx"
    out_dir.mkdir(parents=True, exist_ok=True)

    created = []
    for name, (src, af) in SFX.items():
        out_path = out_dir / f"{name}.wav"
        cmd = [
            ffmpeg, "-y", "-f", "lavfi", "-i", src,
            "-af", af, "-ac", "2", "-ar", str(SR),
            "-c:a", "pcm_s16le", str(out_path),
        ]
        try:
            run(cmd)
        except Exception as e:
            fail(f"Failed to synthesize SFX '{name}': {e}")
            return
        created.append({
            "name": name,
            "path": str(out_path),
            "bytes": os.path.getsize(out_path),
        })

    emit({"dir": str(out_dir), "created": created})


if __name__ == "__main__":
    main()
