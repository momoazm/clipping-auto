"""Assemble the final, upload-ready Short from a reframed clip.

Pipeline role: the last creative step before upload. Takes the 9:16 reframed clip
(from reframe_crop.py) and burns in the animated captions, applies a uniform speed
ramp, adds the sound-design + zoom-punch "retention editing" layers, mixes a ducked
music bed, normalizes loudness for social, and enforces the exact delivery container
(1080x1920, H.264/yuv420p + AAC, faststart).

Sound design + effects (from plan_effects.py cues, passed via --cues):
  - SFX (whoosh on caption/scene changes, riser into the hook, pop on emphasis) are
    each added as an extra input, delayed to their cue time, gained to sit UNDER the
    voice, and amixed in (normalize=0 + a final limiter so nothing clips).
  - Punch-ins: brief center zooms on emphasis moments, implemented as `sendcmd`-driven
    `crop` keyframes (math done in Python) applied AFTER the caption burn, then scaled
    back to 1080x1920 -- the same bare-filename + cwd pattern the reframe/ass filters use.

Sync note: captions are burned and punch-zoom keyframes are applied BEFORE the speed
change; the speed factor is then applied uniformly to video (setpts) and audio (atempo),
and SFX are mixed at their original-time delays BEFORE atempo, so everything -- captions,
SFX, punches -- stays in sync at any speed. Non-uniform silence-removal cuts are handled
upstream (silence_jumpcut.py + a regenerated caption timeline), not here.

Usage:
    python tools/render_clip.py --in .tmp/reframed_01.mp4 [--captions .tmp/caps_01.ass] \
        [--cues .tmp/cues_01.json] [--out .tmp/short_01.mp4] [--speed 1.1] \
        [--music auto|config/music/bed.mp3] [--music-volume 0.18] [--max-secs 60]

Prints JSON: {"path","duration","width","height","byte_size","speed","captions",
              "music","sfx_count","punch_ins"}
"""
import argparse
import json
import math
import os

from _common import (load_env, emit, fail, run, ffmpeg_bin, ffprobe_json,
                     tmp_path, TMP_DIR, REPO_ROOT, FFmpegMissing)

OUT_W, OUT_H = 1080, 1920
CMD_FPS = 30.0            # punch-zoom keyframe density (matches output fps -> smooth)
PUNCH_AMP = 0.12          # peak zoom (1.0 -> 1.12x)
PUNCH_SIGMA = 0.16        # gaussian half-width in seconds (~0.4s visible bump)
PUNCH_MAX = 1.14          # hard cap on zoom factor
SFX_GAIN = {"whoosh": 0.5, "riser": 0.45, "pop": 0.6, "ding": 0.5}


def has_audio(path):
    try:
        info = ffprobe_json(path)
        return any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    except Exception:
        return False


def probe_duration(path):
    try:
        return float(ffprobe_json(path).get("format", {}).get("duration") or 0)
    except Exception:
        return 0.0


def resolve_music(arg):
    """--music auto -> pick a bed from config/music (prefer cinematic_tension);
    an explicit path -> use as-is; falsy/none -> no music."""
    if not arg or arg.lower() == "none":
        return None
    if arg.lower() == "auto":
        mdir = REPO_ROOT / "config" / "music"
        if not mdir.is_dir():
            return None
        beds = sorted(p for p in mdir.iterdir()
                      if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg"))
        if not beds:
            return None
        for pref in ("cinematic_tension", "ambient_glow"):
            for b in beds:
                if b.stem == pref:
                    return str(b)
        return str(beds[0])
    return arg if os.path.isfile(arg) else None


def build_punch_cmds(punch_ins, dur):
    """sendcmd text driving the crop filter to zoom in briefly at each punch time.

    Z(t) = min(PUNCH_MAX, 1 + sum_i AMP*exp(-((t-p_i)/SIGMA)^2)). We crop a centered
    OUT_W/Z x OUT_H/Z window (even dims) and let the downstream scale blow it back up
    to OUT_W x OUT_H -- a center punch-in including the burned captions.
    """
    n = max(2, int(dur * CMD_FPS))
    lines = []
    for k in range(n + 1):
        t = min(dur, k / CMD_FPS)
        z = 1.0
        for p in punch_ins:
            z += PUNCH_AMP * math.exp(-(((t - p) / PUNCH_SIGMA) ** 2))
        z = min(PUNCH_MAX, z)
        w = int(round(OUT_W / z)) & ~1   # force even
        h = int(round(OUT_H / z)) & ~1
        x = (OUT_W - w) // 2
        y = (OUT_H - h) // 2
        lines.append(f"{t:.3f} crop w {w};")
        lines.append(f"{t:.3f} crop h {h};")
        lines.append(f"{t:.3f} crop x {x};")
        lines.append(f"{t:.3f} crop y {y};")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="inp", required=True, help="Reframed 9:16 clip")
    parser.add_argument("--captions", default=None, help="ASS subtitle file to burn")
    parser.add_argument("--cues", default=None, help="plan_effects.py JSON (sfx + punch_ins)")
    parser.add_argument("--out", default=None)
    parser.add_argument("--speed", type=float, default=1.0, help="Uniform speed (0.5-2.0)")
    parser.add_argument("--music", default=None, help='Music bed path, "auto", or "none"')
    parser.add_argument("--music-volume", type=float, default=0.18)
    parser.add_argument("--no-sfx", action="store_true", help="Ignore SFX cues even if --cues given")
    parser.add_argument("--no-punch", action="store_true", help="Ignore punch-ins even if --cues given")
    parser.add_argument("--max-secs", type=float, default=60.0)
    args = parser.parse_args()

    load_env()
    out_path = args.out or tmp_path("short_01.mp4")
    speed = max(0.5, min(2.0, args.speed))

    if not os.path.isfile(args.inp):
        fail(f"Input clip not found: {args.inp}")
        return
    if args.captions and not os.path.isfile(args.captions):
        fail(f"Captions file not found: {args.captions}")
        return

    # --- load effect cues ----------------------------------------------------
    sfx_cues, punch_ins = [], []
    if args.cues:
        if not os.path.isfile(args.cues):
            fail(f"Cues file not found: {args.cues}")
            return
        with open(args.cues, "r", encoding="utf-8") as f:
            cues = json.load(f)
        if not args.no_sfx:
            for c in cues.get("sfx", []):
                name, t = c.get("name"), c.get("t")
                path = REPO_ROOT / "config" / "sfx" / f"{name}.wav"
                if name is not None and t is not None and path.is_file():
                    sfx_cues.append((name, float(t), str(path)))
        if not args.no_punch:
            punch_ins = [float(t) for t in cues.get("punch_ins", [])]

    music_path = resolve_music(args.music)

    try:
        ffmpeg = ffmpeg_bin()
    except FFmpegMissing as e:
        fail(str(e))
        return

    audio_present = has_audio(args.inp)
    dur = probe_duration(args.inp)

    # --- video chain: captions -> punch zoom -> speed -> scale ---------------
    vchain = []
    cwd = None
    if args.captions:
        # Reference the .ass (and the punch cmds file) by bare filename and run from
        # that dir so ffmpeg's filtergraph parser never sees the drive colon / space.
        cwd = os.path.dirname(os.path.abspath(args.captions)) or None
        vchain.append(f"ass={os.path.basename(args.captions)}")
    if punch_ins and dur > 0:
        stem = os.path.splitext(os.path.basename(out_path))[0]
        punch_name = f"punch_{stem}.txt"
        (TMP_DIR / punch_name).write_text(build_punch_cmds(punch_ins, dur) + "\n",
                                          encoding="utf-8")
        # If there are no captions, cwd is unset; the punch file lives in .tmp, so
        # point cwd there and keep the bare-filename rule.
        if cwd is None:
            cwd = str(TMP_DIR)
        vchain.append(f"sendcmd=f={punch_name},crop=w={OUT_W}:h={OUT_H}:x=0:y=0")
    if speed != 1.0:
        vchain.append(f"setpts=PTS/{speed}")
    vchain.append(f"scale={OUT_W}:{OUT_H},setsar=1,format=yuv420p")
    chains = ["[0:v]" + ",".join(vchain) + "[v]"]

    # --- input list: 0=clip, then music, then one input per SFX cue ----------
    inputs = [os.path.abspath(args.inp)]
    music_idx = None
    if music_path:
        music_idx = len(inputs)
        inputs.append(os.path.abspath(music_path))
    sfx_start = len(inputs)
    for (_n, _t, path) in sfx_cues:
        inputs.append(os.path.abspath(path))

    # --- audio chain: loudnorm voice, duck music, mix SFX, match speed -------
    map_audio = False
    base = None
    if audio_present:
        map_audio = True
        chains.append("[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[voice]")
        if music_path:
            chains.append(f"[{music_idx}:a]volume={args.music_volume}[mraw]")
            chains.append("[mraw][voice]sidechaincompress=threshold=0.03:ratio=8:attack=5:release=250[ducked]")
            chains.append("[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0[base]")
            base = "[base]"
        else:
            base = "[voice]"
    elif music_path:
        map_audio = True
        base = f"[{music_idx}:a]"  # used directly below

    if base is not None:
        # Mix SFX on top of the base (normalize=0 so the base keeps its level; a
        # limiter after the speed change keeps the sum from clipping).
        if sfx_cues:
            sfx_labels = []
            for j, (name, t, _path) in enumerate(sfx_cues):
                in_idx = sfx_start + j
                ms = max(0, int(round(t * 1000)))
                g = SFX_GAIN.get(name, 0.5)
                chains.append(f"[{in_idx}:a]adelay={ms}|{ms},volume={g}[sfx{j}]")
                sfx_labels.append(f"[sfx{j}]")
            chains.append(
                base + "".join(sfx_labels)
                + f"amix=inputs={1 + len(sfx_labels)}:normalize=0:duration=first[premix]")
            premix = "[premix]"
        elif base == f"[{music_idx}:a]":
            # No-voice + no-SFX: pass the music through a relabel so [a] exists.
            chains.append(f"{base}anull[premix]")
            premix = "[premix]"
        else:
            premix = base

        tail = f"atempo={speed}," if speed != 1.0 else ""
        limiter = "alimiter=limit=0.95," if sfx_cues else ""
        chains.append(f"{premix}{tail}{limiter}anull[a]")

    # --- assemble + run ------------------------------------------------------
    # -nostdin: never read stdin. In CI (GitHub Actions) ffmpeg's interactive keyboard
    # reader blocks on the open-pipe stdin and the encode never starts -- that stall hung
    # this step for hours. Belt-and-suspenders with run()'s stdin=DEVNULL.
    cmd = [ffmpeg, "-nostdin", "-y"]
    for i, inp in enumerate(inputs):
        # Loop the music bed so it always covers the clip; amix(duration=first) trims it.
        if i == music_idx:
            cmd += ["-stream_loop", "-1"]
        cmd += ["-i", inp]
    cmd += ["-filter_complex", ";".join(chains), "-map", "[v]"]
    if map_audio:
        cmd += ["-map", "[a]"]
    cmd += [
        "-t", f"{args.max_secs:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", "30", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        os.path.abspath(out_path),
    ]

    def simple_cmd():
        """A minimal, crash-proof fallback: burned captions + 9:16 scale + loudnorm audio only.
        The full graph (sendcmd punch-zoom + amix SFX + sidechain music) can SIGSEGV ffmpeg on
        certain clips (exit -11); dropping those effects still yields a shippable, on-brand Short
        rather than losing the clip entirely."""
        v = []
        if args.captions:
            v.append(f"ass={os.path.basename(args.captions)}")
        v.append(f"scale={OUT_W}:{OUT_H},setsar=1,format=yuv420p")
        ch = ["[0:v]" + ",".join(v) + "[v]"]
        amap2 = False
        if audio_present:
            ch.append("[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]"); amap2 = True
        c = [ffmpeg, "-nostdin", "-y", "-i", os.path.abspath(args.inp),
             "-filter_complex", ";".join(ch), "-map", "[v]"]
        if amap2:
            c += ["-map", "[a]"]
        c += ["-t", f"{args.max_secs:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
              "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "192k",
              "-movflags", "+faststart", os.path.abspath(out_path)]
        return c

    effects = "full"
    try:
        # A 60s clip renders in well under a minute even on a 2-core CI runner; 15 min is
        # a generous ceiling that turns any future stall into a fast, legible failure
        # instead of a multi-hour job-timeout with no output.
        run(cmd, cwd=cwd, timeout=900)
    except Exception as e:
        # Full graph failed/crashed -> retry stripped-down so the clip still ships.
        print(f"[render_clip] full render failed ({str(e)[:120]}); retrying without punch/SFX",
              file=__import__("sys").stderr)
        try:
            run(simple_cmd(), cwd=cwd, timeout=900)
            effects = "simple"
        except Exception as e2:
            fail(f"render failed (full + simple): {e2}")
            return

    out_dur = probe_duration(out_path)
    emit({
        "path": out_path,
        "duration": round(out_dur, 3) if out_dur else None,
        "width": OUT_W,
        "height": OUT_H,
        "byte_size": os.path.getsize(out_path),
        "speed": speed,
        "captions": bool(args.captions),
        "music": os.path.basename(music_path) if music_path else None,
        "sfx_count": len(sfx_cues),
        "punch_ins": len(punch_ins),
        "effects": effects,
    })


if __name__ == "__main__":
    main()
