"""Download a source video from a URL (YouTube, etc.) for the clipping pipeline.

Pipeline role: the front door when the user gives a link instead of a local file.
Fetches the best <=1080p MP4 into .tmp/ so probe_video.py and the rest of the
pipeline can treat it like any local source.

Uses yt-dlp's Python API and points it at our ffmpeg (for stream merging) via
_common.ffmpeg_bin(), so it works even when ffmpeg isn't on PATH.

Usage:
    python tools/download_video.py --url "https://youtu.be/..." \
        [--out .tmp/source.mp4] [--max-height 1080]

Prints JSON: {"path","title","duration","width","height","url","id"}
"""
import argparse
import os

from _common import load_env, emit, fail, ffmpeg_bin, tmp_path, FFmpegMissing, REPO_ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", default=None, help="Output path (default .tmp/source.mp4)")
    parser.add_argument("--max-height", type=int, default=1080)
    args = parser.parse_args()

    load_env()
    out_path = args.out or tmp_path("source.mp4")
    out_base = os.path.splitext(out_path)[0]  # yt-dlp appends the real ext

    try:
        ffmpeg_dir = os.path.dirname(ffmpeg_bin())
    except FFmpegMissing as e:
        fail(str(e))
        return

    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        fail("yt-dlp not installed. Run: .venv/Scripts/python -m pip install yt-dlp")
        return

    h = args.max_height
    ydl_opts = {
        # Relaxed selector: best video<=h + best audio (any codec; merged to mp4), then
        # progressively looser fallbacks ending in plain "b" so it always resolves.
        "format": f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": out_base + ".%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ffmpeg_location": ffmpeg_dir,
        "overwrites": True,
        # Server-side fix: from datacenter IPs the default web client now returns NO
        # formats (YouTube "PO token" gating) -> "Requested format is not available".
        # The "tv" client still serves downloadable formats without a PO token.
        "extractor_args": {"youtube": {"player_client": ["tv", "web_safari", "default"]}},
    }

    # On cloud/datacenter IPs (e.g. GitHub Actions) YouTube demands "confirm you're not
    # a bot". Authenticated cookies fix it: drop a Netscape cookies.txt at the project
    # root (or point YT_COOKIES_FILE at one). Absent locally -> normal residential use.
    cookie_file = os.environ.get("YT_COOKIES_FILE") or str(REPO_ROOT / "cookies.txt")
    if os.path.isfile(cookie_file):
        ydl_opts["cookiefile"] = cookie_file

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(args.url, download=True)
            final_path = ydl.prepare_filename(info)
            # merge_output_format may have changed the extension to .mp4
            if not os.path.isfile(final_path):
                cand = out_base + ".mp4"
                final_path = cand if os.path.isfile(cand) else final_path
    except Exception as e:
        fail(f"yt-dlp download failed: {e}", url=args.url)
        return

    if not os.path.isfile(final_path):
        fail("Download reported success but no output file was found.", url=args.url)
        return

    emit({
        "path": final_path,
        "title": info.get("title"),
        "duration": info.get("duration"),
        "width": info.get("width"),
        "height": info.get("height"),
        "url": args.url,
        "id": info.get("id"),
    })


if __name__ == "__main__":
    main()
