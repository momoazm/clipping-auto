"""Pick today's MrBeast source video to clip — via yt-dlp, so it costs ZERO YouTube
Data API quota (the API quota is reserved for the uploads).

Selection logic (mirrors the user's spec):
  1. Walk the channels in `config/channels.json` strictly in SUBSCRIBER-RANK order
     (largest -> smallest). Within each channel scan its uploads newest -> older and
     return the FIRST one that hasn't been used yet. So we fully exhaust the biggest
     channel's recent uploads before dropping to the next channel down.
  2. If every channel is exhausted, use the curated `popular_fallback` list (first id
     not yet used).
  3. If everything is already used, fail cleanly -> the day is skipped (no repeats).

"Used" = already clipped OR already attempted (run_daily records every source it
picks, success or fail), plus anything passed via --exclude this run. That's what
guarantees the job never hands back the same video twice.

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
    """Source ids we must never re-pick: both successfully `clipped` and previously
    `attempted` (a failed run still records its source so it isn't retried forever).
    Accepts either a bare list or {"clipped":[...], "attempted":[...]}."""
    if isinstance(state, dict):
        records = (state.get("clipped") or []) + (state.get("attempted") or [])
    else:
        records = state or []
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
                    help="Comma-separated video ids to also skip (already tried this run)")
    args = ap.parse_args()

    cfg_path = args.config or str(REPO_ROOT / "config" / "channels.json")
    hist_path = args.history or str(REPO_ROOT / "state" / "clipped_history.json")

    cfg = load_json(cfg_path, None)
    if not cfg:
        fail(f"channels config not found/invalid: {cfg_path}")
        return

    skip = history_ids(load_json(hist_path, {"clipped": []}))
    skip |= {x.strip() for x in (args.exclude or "").split(",") if x.strip()}
    channels = cfg.get("channels", [])
    depth = int(cfg.get("scan_depth", 25))
    errors = {}

    # Walk channels strictly largest -> smallest; within each, newest -> older. Return
    # the first upload not already used. This both honours the subscriber-rank sequence
    # (exhaust the biggest channel before dropping down) and never returns a repeat.
    for ch in channels:
        try:
            entries = channel_latest(ch["url"], depth)
        except Exception as e:
            errors[ch["name"]] = str(e)
            continue
        newest_id = entries[0]["id"] if entries else None
        for v in entries:  # newest first
            if v["id"] not in skip:
                emit({"video_id": v["id"], "url": v["url"], "title": v["title"],
                      "channel": ch["name"],
                      "reason": ("newest upload" if v["id"] == newest_id
                                 else "recent backlog (newer ones already used)")})
                return

    # Last resort: curated popular fallback.
    for vid in cfg.get("popular_fallback", []):
        if vid not in skip:
            emit({"video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
                  "title": None, "channel": "popular_fallback",
                  "reason": "no unclipped channel uploads; curated popular video"})
            return

    fail("No unclipped source video found (channels + popular all exhausted).",
         channel_errors=errors, skip_count=len(skip))


if __name__ == "__main__":
    main()
