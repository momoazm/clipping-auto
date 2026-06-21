"""Pick today's MrBeast source video to clip — via yt-dlp, so it costs ZERO YouTube
Data API quota (the API quota is reserved for the uploads).

Selection logic (mirrors the user's spec):
  1. Walk the channels in `config/channels.json` in SUBSCRIBER-RANK order. For each,
     take its newest upload. The first channel whose latest video is NOT already in
     history wins -> "the newest video from the biggest channel that has a new one."
     (If MrBeast's latest is already clipped, fall through to MrBeast Gaming, etc.)
  2. If no channel has a brand-new upload, use the curated `popular_fallback` list
     (first id not yet clipped) -> "if none have a new video, use a popular one."
  3. If that's exhausted too, scan deeper into each channel's recent uploads and take
     the first unclipped one (so the job still produces something).
  4. If everything is already clipped, fail cleanly -> the day is skipped (no repeats).

Usage:
    python tools/find_source_video.py [--config config/channels.json] \
        [--history state/clipped_history.json]

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
    """Accept either a bare list or {"clipped": [...]} of records/ids."""
    records = state.get("clipped", []) if isinstance(state, dict) else (state or [])
    ids = set()
    for rec in records:
        sid = rec.get("source_id") if isinstance(rec, dict) else rec
        if sid:
            ids.add(sid)
    return ids


def channel_latest(url, depth):
    """Flat-extract a channel's newest uploads (newest first). No download, no API key."""
    from yt_dlp import YoutubeDL

    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "extract_flat": "in_playlist", "skip_download": True,
        "playlist_items": f"1:{max(1, depth)}",
    }
    cookie_file = os.environ.get("YT_COOKIES_FILE") or str(REPO_ROOT / "cookies.txt")
    if os.path.isfile(cookie_file):
        opts["cookiefile"] = cookie_file
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
                    help="Comma-separated video IDs to also skip (e.g. sources already "
                         "tried this run), on top of the permanent history file.")
    args = ap.parse_args()

    cfg_path = args.config or str(REPO_ROOT / "config" / "channels.json")
    hist_path = args.history or str(REPO_ROOT / "state" / "clipped_history.json")

    cfg = load_json(cfg_path, None)
    if not cfg:
        fail(f"channels config not found/invalid: {cfg_path}")
        return

    done = history_ids(load_json(hist_path, {"clipped": []}))
    done |= {v.strip() for v in (args.exclude or "").split(",") if v.strip()}
    channels = cfg.get("channels", [])
    depth = int(cfg.get("scan_depth", 25))
    errors = {}
    cache = {}

    # Phase 1: newest upload per channel, in priority order.
    for ch in channels:
        try:
            entries = channel_latest(ch["url"], depth)
            cache[ch["name"]] = entries
        except Exception as e:
            errors[ch["name"]] = str(e)
            cache[ch["name"]] = []
            continue
        if entries and entries[0]["id"] not in done:
            v = entries[0]
            emit({"video_id": v["id"], "url": v["url"], "title": v["title"],
                  "channel": ch["name"], "reason": "newest upload (new)"})
            return

    # Phase 2: curated popular fallback.
    for vid in cfg.get("popular_fallback", []):
        if vid not in done:
            emit({"video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
                  "title": None, "channel": "popular_fallback",
                  "reason": "no new uploads anywhere; curated popular video"})
            return

    # Phase 3: deeper recent backlog per channel.
    for ch in channels:
        for v in cache.get(ch["name"], []):
            if v["id"] not in done:
                emit({"video_id": v["id"], "url": v["url"], "title": v["title"],
                      "channel": ch["name"],
                      "reason": "recent backlog (no brand-new uploads)"})
                return

    fail("No unclipped source video found (channels + popular + backlog all exhausted).",
         channel_errors=errors, history_count=len(done))


if __name__ == "__main__":
    main()
