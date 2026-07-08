"""Download a source video from a URL (YouTube, etc.) for the clipping pipeline.

Pipeline role: the front door when the user gives a link instead of a local file.
Fetches the best <=1440p MP4 into .tmp/ (higher res = a sharper 9:16 vertical crop) so
probe_video.py and the rest of the pipeline can treat it like any local source.

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
    parser.add_argument("--max-height", type=int, default=1440)
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
        # BEST QUALITY up to `h` (default 1440), 2026-07-09: the source is 16:9 but the Short is
        # a 9:16 vertical CROP of it, so a higher-res source = a sharper crop (a 1080p source
        # crops to only a ~600px-wide column -> upscaled -> soft; 1440p -> ~810px -> sharp).
        # `format_sort` picks the HIGHEST resolution first, then prefers H.264 (avc1) ONLY as a
        # same-resolution tie-break: avc1 tops out at 1080 on YouTube, so <=1080 stays light avc1
        # (HW-decodable) while 1440 comes as VP9/AV1 -- fine on the GitHub-hosted cloud runner's
        # RAM (the old ~4GB self-hosted-laptop OOM constraint no longer applies). m4a audio avoids
        # the Opus-in-mp4 edge case.
        "format": f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b",
        "format_sort": ["res", "vcodec:h264", "acodec:m4a"],
        "merge_output_format": "mp4",
        "outtmpl": out_base + ".%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ffmpeg_location": ffmpeg_dir,
        "overwrites": True,
        # WEB CLIENT FIRST (2026-07-08): the BgUtils PO-token provider only supplies tokens
        # for the `web` client -- that's what beats YouTube's datacenter bot-wall. `android`
        # is kept as a last-ditch fallback (it can't use a PO token, but occasionally works).
        # Without this, yt-dlp's default client set may skip web and the wall wins.
        "extractor_args": {"youtube": {"player_client": ["web", "android"]}},
    }

    # On cloud/datacenter IPs (e.g. GitHub Actions) YouTube demands "confirm you're not
    # a bot". Authenticated cookies fix it: drop a Netscape cookies.txt at the project
    # root (or point YT_COOKIES_FILE at one). Absent locally -> normal residential use.
    cookie_file = os.environ.get("YT_COOKIES_FILE") or str(REPO_ROOT / "cookies.txt")
    if os.path.isfile(cookie_file):
        ydl_opts["cookiefile"] = cookie_file

    # Cloud runners (GitHub-hosted) have datacenter IPs that YouTube bot-checks. Routing
    # yt-dlp -- and only yt-dlp -- through a proxy fixes it (probed 2026-07-03: WARP's
    # local SOCKS passes the bot-check where plain IP and PO-token both fail). Native
    # downloader only: ffmpeg can't speak SOCKS, so no --download-sections through this.
    proxy = os.environ.get("YTDLP_PROXY")
    if proxy:
        ydl_opts["proxy"] = proxy

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
