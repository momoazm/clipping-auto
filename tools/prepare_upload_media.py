"""Create and verify the exact MP4 contract used by Instagram Reels."""
import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

MAX_DURATION = 59.5


def emit(data):
    print(json.dumps(data, ensure_ascii=True, indent=2))


def fail(message, **extra):
    emit({"error": message, **extra})
    raise SystemExit(1)


def binary(name):
    try:
        from _common import ffmpeg_bin, ffprobe_bin
        return ffmpeg_bin() if name == "ffmpeg" else ffprobe_bin()
    except Exception:
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(f"{name} not found; install ffmpeg (which includes ffprobe)")


def probe(path):
    ffprobe = binary("ffprobe")
    proc = subprocess.run([ffprobe, "-v", "error", "-print_format", "json",
                           "-show_streams", "-show_format", str(path)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "ffprobe failed")[-1200:])
    return json.loads(proc.stdout)


def faststart(path):
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return False
    moov, mdat = data.find(b"moov"), data.find(b"mdat")
    return moov >= 0 and (mdat < 0 or moov < mdat)


def contract(path):
    info = probe(path)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    try:
        duration = float((info.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    result = {"width": video.get("width"), "height": video.get("height"),
              "video_codec": str(video.get("codec_name") or "").lower(),
              "video_profile": str(video.get("profile") or "").lower(),
              "pixel_format": str(video.get("pix_fmt") or "").lower(),
              "audio_codec": str(audio.get("codec_name") or "").lower(),
              "audio_sample_rate": str(audio.get("sample_rate") or ""),
              "audio_channels": audio.get("channels"),
              "duration_sec": round(duration, 3), "faststart": faststart(path)}
    errors = []
    if (result["width"], result["height"]) != (1080, 1920):
        errors.append(f"expected 1080x1920, got {result['width']}x{result['height']}")
    if result["video_codec"] != "h264":
        errors.append(f"expected H.264 video, got {result['video_codec'] or 'missing'}")
    if result["pixel_format"] != "yuv420p":
        errors.append(f"expected yuv420p, got {result['pixel_format'] or 'missing'}")
    if result["audio_codec"] != "aac":
        errors.append(f"expected AAC audio, got {result['audio_codec'] or 'missing'}")
    if result["audio_sample_rate"] not in {"44100", "48000"}:
        errors.append(f"expected 44.1/48 kHz audio, got {result['audio_sample_rate'] or 'missing'}")
    if result["audio_channels"] not in {1, 2}:
        errors.append(f"expected mono/stereo audio, got {result['audio_channels'] or 'missing'}")
    if not (0 < duration < 60):
        errors.append(f"duration must be between 0 and 60 seconds, got {duration:.3f}")
    if not result["faststart"]:
        errors.append("MP4 moov atom is not fast-start")
    result["errors"], result["valid"] = errors, not errors
    return result


def normalize(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(f".{output.stem}.upload-ready-{os.getpid()}.mp4")
    if temp.exists():
        temp.unlink()
    cmd = [binary("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-i", str(source),
           "-map", "0:v:0", "-map", "0:a:0",
           "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,"
                  "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p",
           "-t", f"{MAX_DURATION:.1f}", "-r", "30",
           "-c:v", "libx264", "-profile:v", "high", "-level:v", "4.1",
           "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k",
           "-movflags", "+faststart", "-avoid_negative_ts", "make_zero", str(temp)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL, check=False)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "ffmpeg failed")[-1400:])
        os.replace(temp, output)
    finally:
        if temp.exists():
            temp.unlink()
    return contract(output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    source = Path(args.input)
    if not source.is_file():
        fail(f"media input not found: {source}")
    try:
        before = contract(source)
        if args.validate_only:
            if not before["valid"]:
                fail("media contract failed", contract=before)
            emit({"status": "media_contract_ok", "path": str(source), "contract": before})
            return
        after = normalize(source, args.output)
        if not after["valid"]:
            fail("normalized media still fails the upload contract", contract=after)
        emit({"status": "media_ready", "path": str(args.output), "normalized": True,
              "before": before, "contract": after})
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
