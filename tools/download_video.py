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
    parser.add_argument("--min-height", type=int, default=1080,
                        help="Refuse to download below this. A soft-bot-detected YouTube response "
                             "withholds the HD ladder and offers only <=360p -- better NO clip "
                             "(loud failure, surfaces the PO-token problem) than a soft low-res one.")
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

    h, lo = args.max_height, args.min_height
    base_opts = {
        # BEST QUALITY between `lo` (default 1080) and `h` (default 1440), 2026-07-09: the source
        # is 16:9 but the Short is a 9:16 vertical CROP of it, so a higher-res source = a sharper
        # crop (a 1080p source crops to a ~600px column -> upscaled -> soft; 1440p -> ~810px).
        # `format_sort` picks the HIGHEST resolution first, then H.264 (avc1) ONLY as a same-res
        # tie-break: avc1 tops out at 1080, so <=1080 stays light avc1 while 1440 comes as VP9/AV1
        # (fine on the cloud runner's RAM). The [height>={lo}] FLOOR is the fix for the real bug:
        # when the PO token/client auth doesn't engage, YouTube serves a degraded ladder (only
        # <=360p) and the old selector silently grabbed 360p -> a soft Short. Requiring >=1080
        # makes that case raise "Requested format is not available" so the attempt fails loudly
        # and the next client/route gets a shot. m4a audio avoids Opus-in-mp4.
        "format": (f"bv*[height<={h}][height>={lo}]+ba/b[height<={h}][height>={lo}]/"
                   f"bv*[height>={lo}]+ba/b[height>={lo}]"),
        "format_sort": ["res", "vcodec:h264", "acodec:m4a"],
        "merge_output_format": "mp4",
        "outtmpl": out_base + ".%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ffmpeg_location": ffmpeg_dir,
        "overwrites": True,
    }

    # On cloud/datacenter IPs (e.g. GitHub Actions) YouTube demands "confirm you're not
    # a bot". Authenticated cookies fix it: drop a Netscape cookies.txt at the project
    # root (or point YT_COOKIES_FILE at one). Absent locally -> normal residential use.
    cookie_file = os.environ.get("YT_COOKIES_FILE") or str(REPO_ROOT / "cookies.txt")
    if os.path.isfile(cookie_file):
        base_opts["cookiefile"] = cookie_file

    proxy = os.environ.get("YTDLP_PROXY")

    # Client/route chain, tried in order until one serves a >= lo ladder (2026-07-10).
    # TV FIRST: the BgUtils PO-token provider mints tokens for BOTH `tv` and `web`, but only
    # `web` was ever tried -- and on 07-09 YouTube started serving the web client a degraded
    # <=360p ladder from the WARP egress even WITH a valid POT (both scheduled runs died on
    # the floor, 5 sources each). `tv` is the least bot-walled POT-covered client (yt-dlp's
    # own default). The no-proxy leg is BgUtils' primary design case -- POT straight from the
    # datacenter IP -- for when the WARP range itself is what's flagged. `android` can't use
    # a POT at all; it's a last-ditch lottery ticket.
    attempts = [
        # DEFAULT FIRST (2026-07-10): on a residential IP, yt-dlp's own client mix gets the
        # full 1080p ladder while FORCED clients fail (`tv` alone returns no formats at all
        # here, `android` caps at 360p) -- the hybrid laptop job died on exactly that. On a
        # bot-walled datacenter IP the default fails fast and the POT-covered chain below
        # still gets its shot, so this costs the cloud path nothing.
        (None, False),
        (["tv"], True),
        (["web"], True),
        (["tv", "web"], False),
        (["android"], True),
    ]

    info, final_path, degraded, last_err = None, None, False, None
    for clients, use_proxy in attempts:
        opts = dict(base_opts)
        if clients:
            opts["extractor_args"] = {"youtube": {"player_client": clients}}
        if proxy and use_proxy:
            # Routing yt-dlp -- and only yt-dlp -- through WARP's local SOCKS (probed
            # 2026-07-03). Native downloader only: ffmpeg can't speak SOCKS.
            opts["proxy"] = proxy
        route = f"{'+'.join(clients) if clients else 'default'}{' via proxy' if (proxy and use_proxy) else ' direct'}"
        try:
            with YoutubeDL(opts) as ydl:
                inf = ydl.extract_info(args.url, download=True)
                path = ydl.prepare_filename(inf)
                # merge_output_format may have changed the extension to .mp4
                if not os.path.isfile(path):
                    cand = out_base + ".mp4"
                    path = cand if os.path.isfile(cand) else path
        except Exception as e:
            msg = str(e)
            if "Requested format is not available" in msg:
                degraded = True     # nothing >= lo offered on this route -> bot-degraded ladder
            last_err = f"[{route}] {msg}"
            continue
        # Belt-and-suspenders floor check (a muxed fallback could still slip a low one through).
        got_h = inf.get("height") or 0
        if got_h and got_h < lo:
            degraded = True
            last_err = f"[{route}] served only {got_h}p"
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        info, final_path = inf, path
        break

    if info is None:
        if degraded:
            fail(f"no format >= {lo}p offered on ANY client/route -- YouTube served a "
                 f"degraded/bot-limited ladder; refusing to download a soft low-res source "
                 f"(last: {last_err})", url=args.url, degraded=True)
        else:
            fail(f"yt-dlp download failed: {last_err}", url=args.url)
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
