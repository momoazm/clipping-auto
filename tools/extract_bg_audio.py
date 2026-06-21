"""Extract the audio track from a source video URL and save it as a music bed.

Pipeline role: provides the **background sound used on every clip** when the user
wants a specific track (e.g. the audio from a reference Short) instead of the
synthesized beds from build_music.py. yt-dlp grabs the best audio stream, ffmpeg
converts it to a clean looping-friendly MP3 in config/music/ (default name `bg`),
which render_clip then mixes UNDER the voice (ducked) via `--music`.

The bed is gitignored (config/music/*.mp3) and regenerated at runtime -- on the
self-hosted/residential runner the download works (cloud IPs are blocked for YT
downloads). run_daily.ensure_music() calls this for every run.

NOTE on rights: the extracted audio is whatever is in the source video. If that is
copyrighted, YouTube may mute or strike Shorts that use it -- this honors the user's
explicit request but breaks the project's "royalty-free beds only" rule, so surface
that to the user.

Usage:
    python tools/extract_bg_audio.py --url "https://youtube.com/shorts/<id>" \
        [--out config/music/bg.mp3] [--volume 1.0]

Prints JSON: {"path","bytes","duration","source_url","title"}
"""
import argparse
import os

from _common import (load_env, emit, fail, run, ffmpeg_bin, ffprobe_json,
                     REPO_ROOT, FFmpegMissing)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Video/Short URL to pull audio from")
    parser.add_argument("--out", default=None,
                        help="Output MP3 (default config/music/bg.mp3)")
    parser.add_argument("--volume", type=float, default=1.0,
                        help="Static gain applied while encoding (render also ducks it)")
    args = parser.parse_args()

    load_env()
    out_path = args.out or str(REPO_ROOT / "config" / "music" / "bg.mp3")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    out_base = os.path.splitext(out_path)[0]

    try:
        ffmpeg = ffmpeg_bin()
        ffmpeg_dir = os.path.dirname(ffmpeg)
    except FFmpegMissing as e:
        fail(str(e))
        return

    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        fail("yt-dlp not installed. Run: .venv/Scripts/python -m pip install yt-dlp")
        return

    # bestaudio -> MP3 via yt-dlp's ExtractAudio postprocessor (uses our ffmpeg).
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_base + ".%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ffmpeg_location": ffmpeg_dir,
        "overwrites": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    # Same cookie convention as download_video.py (residential runner needs none).
    cookie_file = os.environ.get("YT_COOKIES_FILE") or str(REPO_ROOT / "cookies.txt")
    if os.path.isfile(cookie_file):
        ydl_opts["cookiefile"] = cookie_file

    title = None
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(args.url, download=True)
            title = info.get("title")
    except Exception as e:
        fail(f"yt-dlp audio extract failed: {e}", url=args.url)
        return

    produced = out_base + ".mp3"
    if not os.path.isfile(produced):
        fail("Audio extract reported success but no MP3 was found.", url=args.url)
        return

    # If a non-default gain was asked for, re-encode through ffmpeg with the volume
    # filter; otherwise the ExtractAudio output IS the final file.
    if abs(args.volume - 1.0) > 1e-3:
        tmp_gain = out_base + ".gain.mp3"
        try:
            run([ffmpeg, "-y", "-i", produced, "-filter:a", f"volume={args.volume}",
                 "-c:a", "libmp3lame", "-b:a", "192k", tmp_gain])
            os.replace(tmp_gain, produced)
        except Exception as e:
            fail(f"volume re-encode failed: {e}")
            return

    if produced != out_path:
        os.replace(produced, out_path)

    duration = None
    try:
        duration = float(ffprobe_json(out_path).get("format", {}).get("duration") or 0)
    except Exception:
        pass

    emit({
        "path": out_path,
        "bytes": os.path.getsize(out_path),
        "duration": round(duration, 3) if duration else None,
        "source_url": args.url,
        "title": title,
    })


if __name__ == "__main__":
    main()
