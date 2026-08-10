"""Pick today's MrBeast source video to clip — via yt-dlp, so it costs ZERO YouTube
Data API quota (the API quota is reserved for the uploads).

Selection logic (mirrors the user's spec):
  1. Walk the channels in `config/channels.json` in SUBSCRIBER-RANK order. For each,
     look at its NEWEST upload only. The first channel whose newest video isn't already
     used wins -> "the newest video from the biggest channel that has a new one."
     (If MrBeast's newest is already used, drop to MrBeast Gaming's newest, etc.)
  2. If no channel has an unused newest upload, use the curated `popular_fallback` list
     (first id not yet used).
  3. If that's exhausted too, scan deeper into each channel's recent uploads (still in
     rank order) and take the first unused one, so the job still produces something.
  4. If everything is already used, fail cleanly -> the day is skipped (no repeats).

"Used" = every source already clipped or reserved by a prior run (run_daily records every
source it picks), plus anything passed via --exclude this run. A source reservation is permanent,
so a transient failure cannot make the same long-form video re-enter the feed later.

Usage:
    python tools/find_source_video.py [--config config/channels.json] \
        [--history state/clipped_history.json] [--exclude id1,id2]

Prints JSON: {"video_id","url","title","channel","reason"}  (or {"error": ...}).
"""
import argparse
import json
import os

from _common import emit, fail, REPO_ROOT

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def history_ids(state):
    """Return every source id that has ever been clipped or reserved.
    Selecting a source is a permanent reservation: even a failed or partial run must not
    make the same long-form video eligible again.
    Accepts either a bare list or {"clipped":[...], "attempted":[...]}."""
    if isinstance(state, dict):
        clipped = state.get("clipped") or []
        attempted = state.get("attempted") or []
    else:
        clipped, attempted = state or [], []
    ids = set()
    for rec in clipped:
        sid = rec.get("source_id") if isinstance(rec, dict) else rec
        if sid:
            ids.add(sid)
    for rec in attempted:
        sid = rec.get("source_id") if isinstance(rec, dict) else rec
        if not sid:
            continue
        ids.add(sid)
    return ids


def incomplete_sources(state):
    """Return source records whose duration-based clip plan is not finished yet."""
    records = state.get("clipped", []) if isinstance(state, dict) else []
    out = []
    for record in records or []:
        if not isinstance(record, dict) or not record.get("source_id"):
            continue
        windows = record.get("clip_windows") or []
        target = record.get("target_clip_count")
        remaining = record.get("remaining_clips")
        try:
            target = int(target) if target is not None else None
            remaining = int(remaining) if remaining is not None else None
        except (TypeError, ValueError):
            continue
        if (remaining is not None and remaining > 0) or (
                target is not None and target > len(windows)):
            out.append(record)
    return out


def channel_latest(url, depth):
    """Flat-extract a channel's newest uploads (newest first). No download, no API key."""
    from yt_dlp import YoutubeDL

    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "extract_flat": "in_playlist", "skip_download": True,
        "playlist_items": f"1:{max(1, depth)}",
        "socket_timeout": 15, "extractor_retries": 0,
    }
    cookie_file = os.environ.get("YT_COOKIES_FILE") or str(REPO_ROOT / "cookies.txt")
    if os.path.isfile(cookie_file):
        opts["cookiefile"] = cookie_file
    proxy = os.environ.get("YTDLP_PROXY")   # datacenter-IP runners: route via WARP/residential proxy
    if proxy:
        opts["proxy"] = proxy
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    out = []
    for e in (info.get("entries") or []):
        vid = e.get("id")
        if not vid:
            continue
        out.append({
            "id": vid,
            "title": e.get("title") or "",
            "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--history", default=None)
    ap.add_argument("--exclude", default="",
                    help="Comma-separated video ids to also skip (already tried this run)")
    args = ap.parse_args()

    cfg_path = args.config or str(REPO_ROOT / "config" / "channels.json")
    hist_path = args.history or str(REPO_ROOT / "state" / "clipped_history.json")

    cfg = load_json(cfg_path, None)
    if not cfg:
        fail(f"channels config not found/invalid: {cfg_path}")
        return

    state = load_json(hist_path, {"clipped": []})
    incomplete = incomplete_sources(state)
    incomplete_ids = {record["source_id"] for record in incomplete}
    # A source with a remaining duration-based plan is intentionally eligible again. All
    # completed/attempted sources remain permanently skipped, as before.
    skip = history_ids(state) - incomplete_ids
    skip |= {x.strip() for x in (args.exclude or "").split(",") if x.strip()}
    channels = cfg.get("channels", [])
    depth = int(cfg.get("scan_depth", 25))
    errors = {}
    cache = {}

    # Finish an unfinished long-video plan before selecting a new source. This lets a 55-minute
    # video produce six safe uploads per day under the provider budget, then continue with the
    # remaining non-overlapping spans on the next run.
    channel_order = {ch.get("name"): i for i, ch in enumerate(channels)}
    for record in sorted(incomplete, key=lambda item: channel_order.get(item.get("channel"), 999)):
        sid = record.get("source_id")
        if sid in skip:
            continue
        emit({"video_id": sid, "url": f"https://www.youtube.com/watch?v={sid}",
              "title": record.get("source_title") or "", "channel": record.get("channel", ""),
              "reason": "continue duration-based clip plan",
              "source_duration": record.get("source_duration"),
              "target_clip_count": record.get("target_clip_count"),
              "remaining_clips": record.get("remaining_clips")})
        return

    # Phase 1: each channel's NEWEST upload only, in subscriber-rank order. First channel
    # whose newest isn't already used wins; if it is used, drop to the next big channel.
    for ch in channels:
        try:
            entries = channel_latest(ch["url"], depth)
            cache[ch["name"]] = entries
        except Exception as e:
            errors[ch["name"]] = str(e)
            cache[ch["name"]] = []
            continue
        if entries and entries[0]["id"] not in skip:
            v = entries[0]
            emit({"video_id": v["id"], "url": v["url"], "title": v["title"],
                  "channel": ch["name"], "reason": "newest upload"})
            return

    # Phase 2: curated popular fallback.
    for vid in cfg.get("popular_fallback", []):
        if vid not in skip:
            emit({"video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
                  "title": None, "channel": "popular_fallback",
                  "reason": "no new uploads anywhere; curated popular video"})
            return

    # Phase 3: deeper recent backlog per channel (rank order), last resort.
    for ch in channels:
        for v in cache.get(ch["name"], []):
            if v["id"] not in skip:
                emit({"video_id": v["id"], "url": v["url"], "title": v["title"],
                      "channel": ch["name"],
                      "reason": "recent backlog (no brand-new uploads)"})
                return

    fail("No unclipped source video found (channels + popular + backlog all exhausted).",
         channel_errors=errors, skip_count=len(skip))


if __name__ == "__main__":
    main()
