"""Probe a source video with ffprobe before spending any paid API calls on it.

Validates the file is readable and reports the facts the rest of the pipeline
needs: duration, resolution, fps, whether it has an audio track, and codecs.

Usage:
    python tools/probe_video.py --in path/to/video.mp4

Prints JSON: {"path","duration","width","height","fps","has_audio","v_codec","a_codec"}
"""
import argparse
import os

from _common import load_env, emit, fail, ffprobe_json, FFmpegMissing


def _fps(stream):
    raw = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/0"
    try:
        num, den = raw.split("/")
        den = float(den)
        return round(float(num) / den, 3) if den else None
    except (ValueError, ZeroDivisionError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="inp", required=True, help="Path to the source video")
    args = parser.parse_args()

    load_env()

    if not os.path.isfile(args.inp):
        fail(f"Input not found: {args.inp}")
        return

    try:
        info = ffprobe_json(args.inp)
    except FFmpegMissing as e:
        fail(str(e))
        return
    except Exception as e:
        fail(f"ffprobe could not read this file: {e}", path=args.inp)
        return

    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if not video:
        fail("No video stream found in this file.", path=args.inp)
        return

    fmt = info.get("format", {})
    duration = float(fmt.get("duration") or video.get("duration") or 0) or None

    emit({
        "path": args.inp,
        "duration": duration,
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": _fps(video),
        "has_audio": audio is not None,
        "v_codec": video.get("codec_name"),
        "a_codec": audio.get("codec_name") if audio else None,
        "size_bytes": int(fmt.get("size")) if fmt.get("size") else None,
    })


if __name__ == "__main__":
    main()
