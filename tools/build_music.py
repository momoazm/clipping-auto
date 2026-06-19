"""Synthesize a couple of subtle royalty-free music beds with ffmpeg.

Pipeline role: provides the optional background-music layer for render_clip's
`--music auto`. There is no music API key in API.env and hotlinking CC0 tracks is
unreliable, so -- like build_sfx.py -- we synthesize simple, low, non-distracting
beds from ffmpeg oscillators. Royalty-free by construction; nothing to license or
strike (honors the audio-rights rule).

These are deliberately understated under-beds (drone + slow swell), not foreground
music. For loud/energetic source audio you'll often leave music off; for quieter
talking-head clips a bed adds polish. Drop your own CC0 tracks in config/music to
override.

Run once (re-run to regenerate):
    python tools/build_music.py

Prints JSON: {"dir": "<config/music>", "created": [{"name","path","bytes"}, ...]}
"""
import os

from _common import emit, fail, run, ffmpeg_bin, REPO_ROOT, FFmpegMissing

SR = 48000
DUR = 120  # longer than any clip so it never needs looping

# name -> list of sine frequencies (a chord/drone) + a filter tail. Each sine is
# an -f lavfi input; we mix them, shape, and slowly swell the amplitude.
BEDS = {
    # Low, dramatic minor drone -- pairs with tense/high-stakes content.
    "cinematic_tension": {
        "freqs": [55.0, 110.0, 164.81, 261.63],   # A1, A2, E3, C4 (A-minor-ish)
        "vols": [0.5, 0.4, 0.28, 0.20],
        "tail": "lowpass=f=2200,highpass=f=35,apulsator=hz=0.08,"
                "aecho=0.8:0.7:120:0.3",
    },
    # Gentler, brighter pad -- for calmer talking-head clips.
    "ambient_glow": {
        "freqs": [130.81, 196.0, 329.63],          # C3, G3, E4 (C-major-ish)
        "vols": [0.4, 0.3, 0.22],
        "tail": "lowpass=f=3000,highpass=f=60,apulsator=hz=0.05,"
                "aecho=0.8:0.6:200:0.25",
    },
}


def main():
    try:
        ffmpeg = ffmpeg_bin()
    except FFmpegMissing as e:
        fail(str(e))
        return

    out_dir = REPO_ROOT / "config" / "music"
    out_dir.mkdir(parents=True, exist_ok=True)

    created = []
    for name, spec in BEDS.items():
        out_path = out_dir / f"{name}.mp3"
        cmd = [ffmpeg, "-y"]
        for f in spec["freqs"]:
            cmd += ["-f", "lavfi", "-i",
                    f"sine=frequency={f}:duration={DUR}:sample_rate={SR}"]
        parts = []
        labels = []
        for i, v in enumerate(spec["vols"]):
            parts.append(f"[{i}]volume={v}[a{i}]")
            labels.append(f"[a{i}]")
        n = len(spec["vols"])
        parts.append("".join(labels) + f"amix=inputs={n}:normalize=0[mix]")
        parts.append(
            f"[mix]{spec['tail']},afade=t=in:d=3,afade=t=out:st={DUR-4}:d=4,"
            "volume=0.5[out]")
        cmd += [
            "-filter_complex", ";".join(parts), "-map", "[out]",
            "-ac", "2", "-ar", str(SR), "-c:a", "libmp3lame", "-b:a", "160k",
            str(out_path),
        ]
        try:
            run(cmd)
        except Exception as e:
            fail(f"Failed to synthesize music bed '{name}': {e}")
            return
        created.append({
            "name": name, "path": str(out_path), "bytes": os.path.getsize(out_path),
        })

    emit({"dir": str(out_dir), "created": created})


if __name__ == "__main__":
    main()
