"""Download a source video from a URL (YouTube, etc.) for the clipping pipeline.

Pipeline role: the front door when the user gives a link instead of a local file.
Fetches the best <=1440p MP4 into .tmp/ (higher res = a sharper 9:16 vertical crop) so
probe_video.py and the rest of the pipeline can treat it like any local source.

Uses yt-dlp's Python API and points it at our ffmpeg (for stream merging) via
_common.ffmpeg_bin(), so it works even when ffmpeg isn't on PATH.

Usage:
    python tools/download_video.py --url "https://youtu.be/..." \
        [--out .tmp/source.mp4] [--max-height 2160]

Prints JSON: {"path","title","duration","width","height","url","id"}
"""
import argparse
import json
import os
import subprocess
import sys
import time

from _common import load_env, emit, fail, ffmpeg_bin, tmp_path, FFmpegMissing, REPO_ROOT

DOWNLOAD_DEADLINE_SEC = 240.0
DOWNLOAD_ATTEMPT_TIMEOUT_SEC = 25.0


def _download_attempt(url, out_base, clients, use_proxy, fmt, ffmpeg_dir, cookie_file):
    """Run one route in a killable yt-dlp child and return its metadata plus output path."""
    cmd = [sys.executable, "-m", "yt_dlp", "--format", fmt,
           "--format-sort", "res,vcodec:h264,acodec:m4a", "--merge-output-format", "mp4",
           "--output", out_base + ".%(ext)s", "--no-playlist", "--quiet", "--no-warnings",
           "--no-progress", "--force-overwrites", "--print-json", "--ffmpeg-location", ffmpeg_dir,
           "--socket-timeout", "15", "--retries", "0", "--fragment-retries", "0",
           "--extractor-retries", "0", "--file-access-retries", "0", "--concurrent-fragments", "4"]
    if clients:
        cmd += ["--extractor-args", "youtube:player_client=" + ",".join(clients)]
    if cookie_file and os.path.isfile(cookie_file):
        cmd += ["--cookies", cookie_file]
    proxy = os.environ.get("YTDLP_PROXY")
    if proxy and use_proxy:
        cmd += ["--proxy", proxy]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=DOWNLOAD_ATTEMPT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"yt-dlp route timed out after {DOWNLOAD_ATTEMPT_TIMEOUT_SEC:.0f}s") from e
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-600:]
        raise RuntimeError(tail or f"yt-dlp exited {proc.returncode}")
    info = None
    for line in reversed((proc.stdout or "").splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and (candidate.get("id") or candidate.get("title")):
            info = candidate
            break
    path = next((p for p in (out_base + ".mp4", out_base + ".mkv", out_base + ".webm")
                 if os.path.isfile(p)), None)
    if not path:
        raise RuntimeError("yt-dlp reported success but produced no media file")
    return info or {}, path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", default=None, help="Output path (default .tmp/source.mp4)")
    parser.add_argument("--max-height", type=int, default=2160,
                        help="Highest source height to fetch before the 1080x1920 vertical render")
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
        # BEST QUALITY between `lo` (default 1080) and `h` (default 2160): the source
        # is 16:9 but the Short is a 9:16 vertical CROP of it, so a higher-res source = a sharper
        # crop (a 1080p source crops to a ~600px column -> upscaled -> soft; 2160p gives a much
        # sharper vertical crop when MrBeast's wide challenge footage is available at that size).
        # `format_sort` picks the HIGHEST resolution first, then H.264 (avc1) ONLY as a same-res
        # tie-break: avc1 tops out at 1080, so <=1080 stays light avc1 while 1440 comes as VP9/AV1
        # (fine on the cloud runner's RAM). The [height>={lo}] FLOOR is the fix for the real bug:
        # when the PO token/client auth doesn't engage, YouTube serves a degraded ladder (only
        # <=360p) and the old selector silently grabbed 360p -> a soft Short. Requiring >=1080
        # makes that case raise "Requested format is not available" so the attempt fails loudly
        # and the next client/route gets a shot. m4a audio avoids Opus-in-mp4.
        # (the "format" selector itself is set per-pass inside try_chain -- the floor varies)
        "format_sort": ["res", "vcodec:h264", "acodec:m4a"],
        "merge_output_format": "mp4",
        "outtmpl": out_base + ".%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ffmpeg_location": ffmpeg_dir,
        "overwrites": True,
        # A blocked source must move on to the next candidate instead of holding a scheduled
        # run through every yt-dlp retry/fragment retry. The route chain already provides the
        # fallback diversity; retries here only turn a fast failure into a long stall.
        "socket_timeout": 15,
        "retries": 0,
        "fragment_retries": 0,
        "extractor_retries": 0,
        "file_access_retries": 0,
        "concurrent_fragment_downloads": 4,
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

    deadline = time.monotonic() + DOWNLOAD_DEADLINE_SEC

    def try_chain(floor):
        """Run the whole client/route chain demanding >= floor. Returns
        (info, path, degraded, last_err) -- degraded means at least one route
        answered but only with a ladder below the floor."""
        info, final_path, degraded, last_err = None, None, False, None
        for clients, use_proxy in attempts:
            if time.monotonic() >= deadline:
                break
            opts = dict(base_opts)
            opts["format"] = (f"bv*[height<={h}][height>={floor}]+ba/b[height<={h}][height>={floor}]/"
                              f"bv*[height>={floor}]+ba/b[height>={floor}]")
            if clients:
                opts["extractor_args"] = {"youtube": {"player_client": clients}}
            if proxy and use_proxy:
                # Routing yt-dlp -- and only yt-dlp -- through WARP's local SOCKS (probed
                # 2026-07-03). Native downloader only: ffmpeg can't speak SOCKS.
                opts["proxy"] = proxy
            route = f"{'+'.join(clients) if clients else 'default'}{' via proxy' if (proxy and use_proxy) else ' direct'}"
            try:
                inf, path = _download_attempt(args.url, out_base, clients, use_proxy,
                                              opts["format"], ffmpeg_dir, cookie_file)
            except Exception as e:
                msg = str(e)
                if "Requested format is not available" in msg:
                    degraded = True     # nothing >= floor offered on this route -> bot-degraded ladder
                last_err = f"[{route}] {msg}"
                continue
            # Belt-and-suspenders floor check (a muxed fallback could still slip a low one through).
            got_h = inf.get("height") or 0
            if got_h and got_h < floor:
                degraded = True
                last_err = f"[{route}] served only {got_h}p"
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            info, final_path = inf, path
            break
        return info, final_path, degraded, last_err

    info, final_path, degraded, last_err = try_chain(lo)

    # Cloud runners since 2026-07-09: YouTube can wall EVERY datacenter route down to a
    # <=360p ladder. YTDLP_DEGRADED_MIN=<height> opts in to a SECOND pass that accepts the
    # best available >= that height instead of failing the run -- a soft Short beats a
    # silent channel, and format_sort still grabs the highest ladder on healthy days.
    took_degraded = False
    degraded_min = int(os.environ.get("YTDLP_DEGRADED_MIN", "0") or 0)
    if info is None and degraded and 0 < degraded_min < lo:
        info, final_path, _, last_err2 = try_chain(degraded_min)
        if info is not None:
            took_degraded = True
        else:
            last_err = last_err2 or last_err

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
        "degraded": took_degraded,
    })


if __name__ == "__main__":
    main()
