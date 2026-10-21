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

Unfinished duration plans are the exception (they stay eligible until the plan completes), so
they get an ATTEMPT CAP instead: every re-offer that made no progress counts as one attempt
(`max_source_attempts` in config/channels.json, default 5) and a source that reaches the cap is
quarantined in the `source_attempts` ledger of the history state file and never offered again.
The two poisoned sources from the 2026-10-04..10-11 runs are pre-seeded there as exhausted.

Usage:
    python tools/find_source_video.py [--config config/channels.json] \
        [--history state/clipped_history.json] [--exclude id1,id2]

Prints JSON: {"video_id","url","title","channel","reason"}  (or {"error": ...}).
"""
import argparse
import datetime
import json
import os

from _common import emit, fail, REPO_ROOT

# Per-source attempt budget for an unfinished plan (config: max_source_attempts).
DEFAULT_MAX_SOURCE_ATTEMPTS = 5


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def load_state(path, default):
    """Load the history state plus a flag saying a write-back is safe.

    An unreadable file (missing or corrupt) is NEVER rewritten: the default stub must not
    be able to replace the real no-repeat memory, which would let old sources repeat.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), True
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default, False


def save_state(path, state):
    """Atomic state write (fsync + os.replace), mirroring run_daily's ledger write."""
    path = str(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def max_source_attempts(cfg):
    """Configured per-source attempt cap (default 5)."""
    try:
        return max(1, int((cfg or {}).get("max_source_attempts", DEFAULT_MAX_SOURCE_ATTEMPTS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_SOURCE_ATTEMPTS


def attempt_ledger(state):
    """Per-source attempt bookkeeping stored beside the history it protects.

    Shape: {source_id: {"count": int, "windows": int, "exhausted": bool, ...}} where
    `count` is the number of no-progress attempts seen and `windows` is how many
    clip_windows the source had when it was last offered.
    """
    ledger = state.get("source_attempts") if isinstance(state, dict) else None
    return ledger if isinstance(ledger, dict) else {}


def exhausted_ids(ledger, cap):
    """Sources that must never be offered again: pre-seeded poisoned ids plus anything
    that already reached the attempt cap or was flagged exhausted by an operator."""
    out = set()
    for sid, entry in (ledger or {}).items():
        if not sid or not isinstance(entry, dict):
            continue
        if entry.get("exhausted") or _as_int(entry.get("count"), 0) >= cap:
            out.add(sid)
    return out


def track_source_offer(state, sid, windows_now, cap):
    """Record one attempt at finishing `sid`; return True when it must be quarantined.

    An unfinished source is only re-offered while its plan is incomplete, so re-offering it
    with the SAME clip_windows count means the previous run produced nothing. New windows
    (real progress) reset the counter so a legitimately continuing source is never
    quarantined mid-plan, and a source that completes keeps working as before.
    """
    if not isinstance(state, dict) or not sid:
        return False
    ledger = dict(attempt_ledger(state))
    entry = dict(ledger.get(sid) or {})
    if "windows" in entry and _as_int(entry.get("windows"), -1) == _as_int(windows_now, -2):
        count = _as_int(entry.get("count"), 0) + 1
    else:
        count = 1
    entry.update({"count": count, "windows": _as_int(windows_now, 0),
                  "exhausted": bool(entry.get("exhausted")) or count >= cap,
                  "updated": datetime.date.today().isoformat()})
    ledger[sid] = entry
    state["source_attempts"] = ledger
    return bool(entry["exhausted"])


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


def scan_report(cache, skip, depth):
    """How deep the search actually went (channels listed vs still unclipped).

    Emitted with the "all exhausted" failure: on 2026-10-16..10-20 the pool looked
    exhausted only because scan_depth (60) stopped one upload short of the first
    untouched video, and the payload gave no way to see that.
    """
    return {
        "depth": depth,
        "listed": {name: len(entries) for name, entries in cache.items()},
        "free": {name: sum(1 for e in entries if e.get("id") not in skip)
                 for name, entries in cache.items()},
    }


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

    state, writable = load_state(hist_path, {"clipped": []})
    # Only a dict loaded from an existing, parseable file may be written back.
    writable = writable and isinstance(state, dict)
    incomplete = incomplete_sources(state)
    incomplete_ids = {record["source_id"] for record in incomplete}
    attempts = max_source_attempts(cfg)
    ledger = attempt_ledger(state)
    # A source with a remaining duration-based plan is intentionally eligible again. All
    # completed/attempted sources remain permanently skipped, as before, and so is every
    # source that has burned its attempt budget (quarantined / exhausted in the ledger).
    skip = history_ids(state) - incomplete_ids
    skip |= exhausted_ids(ledger, attempts)
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
        # Count this offer as an attempt BEFORE handing the source out: an offer that makes
        # no progress quarantines the source once `max_source_attempts` is reached, so a
        # broken source stops cycling back into the feed every day.
        if track_source_offer(state, sid, len(record.get("clip_windows") or []), attempts):
            skip.add(sid)
            if writable:
                save_state(hist_path, state)
            continue
        if writable:
            save_state(hist_path, state)
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

    # Report HOW DEEP the scan went, not just that it came up empty: the per-channel
    # listed/free counts make a too-shallow scan obvious instead of reading like a drought.
    fail("No unclipped source video found (channels + popular + backlog all exhausted).",
         channel_errors=errors,
         scan=scan_report(cache, skip, depth),
         skip_count=len(skip),
         quarantined_count=len(exhausted_ids(attempt_ledger(state), attempts)))


if __name__ == "__main__":
    main()
