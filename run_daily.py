"""Headless daily orchestrator for the clipping automation."""
import argparse
import datetime
import json
import math
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
TMP = HERE / ".tmp"
CONFIG = HERE / "config" / "channels.json"
HISTORY = HERE / "state" / "clipped_history.json"
DELIVERY_COOLDOWNS = HERE / "state" / "delivery_cooldowns.json"

sys.path.insert(0, str(TOOLS))
from _common import log_ig_post  # noqa: E402
# Crash-safe local ledger: written the instant a source is picked, BEFORE any heavy
# download/transcribe/render/upload. Untracked + gitignored for local runs, while the
# GitHub workflow makes a tracked reservation commit before processing starts. Both ledgers
# are permanent: once a source is selected, the same long-form video is never re-clipped.
LEDGER = HERE / "state" / "attempted_local.json"

try:
    from dotenv import load_dotenv
    load_dotenv(HERE / "API.env")
except ImportError:
    pass

# Manual dispatches keep finished Shorts in the Actions artifact so the actual
# encoded files can be reviewed. Scheduled runs still clean up after delivery.
KEEP_RENDERED_CLIPS = os.environ.get("KEEP_RENDERED_CLIPS") == "1"

# Hashtags improve discovery but are not a delivery prerequisite. Keep a deterministic
# fallback so a slow or unavailable LLM provider cannot discard an otherwise-rendered clip.
FALLBACK_HASHTAGS = [
    "shorts", "youtubeshorts", "shortsfeed", "shortsvideo", "viral", "viralshorts",
    "trending", "trendingshorts", "fyp", "foryou", "foryoupage", "mrbeast",
    "mrbeastshorts", "beast", "challenge", "money", "funny", "entertainment",
]

# --- Zernio secret keys (passed as env vars by the workflow; in API.env for local runs) ---
IG_ENABLED = bool(os.environ.get("ZERNIO_API")) and bool(os.environ.get("ZERNIO_INSTAGRAM_ID"))
TIKTOK_ENABLED = (
    bool(os.environ.get("TIKTOK_ACCESS_TOKEN")) or
    all(bool(os.environ.get(key)) for key in
        ("TIKTOK_REFRESH_TOKEN", "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"))
)

def log(*a):
    print("[run_daily]", *a, file=sys.stderr, flush=True)

def purge_files(*paths):
    """Delete temp files once they're finally used (best-effort; never fails the run)."""
    for p in paths:
        if not p:
            continue
        try:
            os.remove(p)
        except OSError:
            pass

def _extract_json(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except Exception:
            return None
    return None


class ToolError(RuntimeError):
    """A child tool returned a structured JSON error."""

    def __init__(self, script, data):
        self.script = script
        self.data = data if isinstance(data, dict) else {}
        super().__init__(f"{script}: {self.data.get('error') or 'tool failed'}")


def _error_data(exc):
    return exc.data if isinstance(exc, ToolError) else {}


def _parse_utc(value):
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)

def run_tool(script, *args):
    cmd = [sys.executable, str(TOOLS / script), *map(str, args)]
    log("->", script, *[a for a in args])
    # A blocked source must not consume the whole daily slot. Keep transcription/rendering
    # generous, but bound each network/media child so the source-attempt loop can advance.
    tool_timeouts = {
        "find_source_video.py": 120,
        "download_video.py": 300,
        "transcribe_video.py": 600,
        # The selector may try the configured Gemini/Groq fallbacks, but each provider call is
        # bounded inside select_clips.py. Leave enough room for the whole configured chain while
        # still returning to the source-attempt loop well before the 60-minute Actions timeout.
        "select_clips.py": 360,
        "reframe_crop.py": 300,
        "plan_effects.py": 120,
        "build_captions.py": 120,
        "render_clip.py": 300,
        "generate_hashtags.py": 120,
        "build_sfx.py": 120,
        "host_public.py": 240,
        "upload_youtube.py": 300,
        "upload_instagram.py": 300,
        "upload_tiktok.py": 300,
    }
    timeout = tool_timeouts.get(script, 900)
    try:
        proc = subprocess.run(cmd, cwd=str(HERE), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{script}: timed out after {timeout}s")
    data = _extract_json(proc.stdout)
    if data is None:
        raise RuntimeError(f"{script}: no JSON output (exit {proc.returncode}). stderr: {(proc.stderr or '')[-400:]}")
    if isinstance(data, dict) and data.get("error"):
        raise ToolError(script, data)
    return data

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def source_history_record(history, source_id):
    """Return the first durable record for a source video, if one exists."""
    records = history.get("clipped", []) if isinstance(history, dict) else []
    for record in records or []:
        if isinstance(record, dict) and record.get("source_id") == source_id:
            return record
    return None


def source_clip_windows(record):
    """Normalize previously published source spans for overlap-safe continuation runs."""
    windows = []
    raw = record.get("clip_windows", []) if isinstance(record, dict) else []
    for item in raw or []:
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (AttributeError, TypeError, ValueError):
            continue
        if end > start:
            window = {"start": start, "end": end}
            if isinstance(item, dict) and item.get("video_id"):
                window["video_id"] = item["video_id"]
            windows.append(window)
    return windows


def duration_target_count(duration, cfg):
    """Return the total desired clips for a source under the configured duration rule."""
    mode = str(cfg.get("clip_count_mode", "fixed")).strip().lower()
    if mode != "duration":
        return int(cfg.get("clips_per_day", 6))
    try:
        interval = max(1.0, float(cfg.get("clip_every_secs", 300)))
        seconds = max(1.0, float(duration or 0))
    except (TypeError, ValueError):
        return int(cfg.get("clips_per_day", 6))
    minimum = max(1, int(cfg.get("min_clips_per_run", 2)))
    return max(minimum, int(math.ceil(seconds / interval)))


def delivery_capacity(required_platforms, cfg, now=None):
    """Return the number of posts still safe for every required platform in this window."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    history = load_json(HISTORY, {"clipped": []})
    counts = recent_delivery_counts(history, now=now)
    limit = max(1, int(cfg.get("max_platform_posts_per_24h", 6)))
    capacities = []
    for platform in sorted(required_platforms):
        if active_delivery_cooldown(platform, now=now):
            return 0
        capacities.append(max(0, limit - counts.get(platform, 0)))
    return min(capacities) if capacities else limit


def _atomic_write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_delivery_cooldowns():
    data = load_json(DELIVERY_COOLDOWNS, {})
    return data if isinstance(data, dict) else {}


def save_delivery_cooldown(platform, metadata):
    """Remember a provider account cooldown so the next run does not retry blindly."""
    now = datetime.datetime.now(datetime.timezone.utc)
    blocked_until = _parse_utc(metadata.get("rate_limited_until"))
    if blocked_until is None:
        # A 429 without a provider timestamp is still unsafe to hammer. Keep a
        # conservative one-day local cooldown until the provider is checked again.
        blocked_until = now + datetime.timedelta(hours=24)
    state = load_delivery_cooldowns()
    state[platform] = {
        "blocked_until": blocked_until.isoformat(),
        "reason": str(metadata.get("error") or "provider rate limit")[:240],
        "recorded_at": now.isoformat(),
    }
    _atomic_write_json(DELIVERY_COOLDOWNS, state)


def active_delivery_cooldown(platform, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    record = load_delivery_cooldowns().get(platform)
    if not isinstance(record, dict):
        return None
    until = _parse_utc(record.get("blocked_until"))
    if until and until > now:
        return record
    return None


def note_rate_limit(summary_dict, platform, exc):
    """Persist and expose a structured Zernio account cooldown."""
    metadata = _error_data(exc)
    if not metadata.get("rate_limited"):
        return
    blocked_until = metadata.get("rate_limited_until")
    summary_dict.setdefault("rate_limited_platforms", {})[platform] = {
        "blocked_until": blocked_until,
        "status_code": metadata.get("status_code"),
        "reason": str(metadata.get("error") or str(exc))[:240],
    }
    save_delivery_cooldown(platform, metadata)


def recent_delivery_counts(history, now=None):
    """Count known platform posts from the last rolling 24 hours.

    Legacy history only has a date, so same-day records are conservatively treated
    as recent. New records include published_at and platform-specific id lists.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(hours=24)
    counts = {"youtube": 0, "instagram": 0, "tiktok": 0}
    records = history.get("clipped", []) if isinstance(history, dict) else []
    for record in records:
        if not isinstance(record, dict):
            continue
        published_at = _parse_utc(record.get("published_at"))
        if published_at is not None:
            if published_at < cutoff:
                continue
        else:
            # Date-only legacy records cannot prove their time. Keep today's and
            # yesterday's posts in the budget until the rolling window is clear.
            try:
                record_date = datetime.date.fromisoformat(str(record.get("date", "")))
            except ValueError:
                continue
            if record_date < (now.date() - datetime.timedelta(days=1)):
                continue

        youtube_ids = record.get("youtube_post_ids")
        if youtube_ids is None:
            youtube_ids = record.get("clip_video_ids", [])
        instagram_ids = record.get("instagram_post_ids")
        if instagram_ids is None:
            instagram_ids = record.get("platform_post_ids", [])
        platform_ids = {
            "youtube": youtube_ids,
            "instagram": instagram_ids,
            "tiktok": record.get("tiktok_post_ids", []),
        }
        for platform, ids in platform_ids.items():
            counts[platform] += sum(1 for post_id in (ids or []) if post_id)
    return counts


def delivery_budget_guard(required_platforms, requested, cfg, now=None):
    """Reserve a full run before rendering so a quota cannot create partial uploads."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    history = load_json(HISTORY, {"clipped": []})
    counts = recent_delivery_counts(history, now=now)
    limit = max(1, int(cfg.get("max_platform_posts_per_24h", 6)))
    blocked = {}
    for platform in sorted(required_platforms):
        cooldown = active_delivery_cooldown(platform, now=now)
        if cooldown:
            blocked[platform] = {
                "reason": "active_provider_cooldown",
                "blocked_until": cooldown.get("blocked_until"),
            }
        elif counts.get(platform, 0) + requested > limit:
            blocked[platform] = {
                "reason": "rolling_24h_post_budget",
                "blocked_until": None,
            }
    return {
        "max_posts_per_24h": limit,
        "requested": requested,
        "recent_posts": {platform: counts.get(platform, 0) for platform in sorted(required_platforms)},
        "blocked": blocked,
    }

def load_ledger_ids():
    """Source ids picked by any prior local run.

    A local reservation is permanent, matching the tracked history used by Actions. This is
    intentional: retrying a broken source later is how duplicate Shorts re-enter the feed.
    """
    data = load_json(LEDGER, {"attempted": []})
    records = (data.get("attempted") if isinstance(data, dict) else data) or []
    ids = set()
    for r in records:
        sid = r.get("source_id") if isinstance(r, dict) else r
        if not sid:
            continue
        ids.add(sid)
    return ids


def mark_ledger(sid):
    """Persist a just-picked source id to the crash-safe local ledger IMMEDIATELY,
    before any heavy processing. Atomic write (fsync + os.replace) so a power cut can't
    leave the file half-written."""
    if not sid:
        return
    data = load_json(LEDGER, {"attempted": []})
    if isinstance(data, list):
        data = {"attempted": data}
    seen = {r.get("source_id") if isinstance(r, dict) else r for r in data.get("attempted", [])}
    if sid in seen:
        return
    data.setdefault("attempted", []).append(
        {"source_id": sid, "date": datetime.date.today().isoformat()})
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_name(LEDGER.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LEDGER)  # atomic on the same filesystem
    log(f"ledger: recorded picked source {sid} (crash-safe, pre-processing)")


def record_attempts(attempted_ids, dry_run):
    """Persist every source id picked this run (success OR fail) as a permanent reservation.

    The workflow commits this file even when processing fails. Dry runs deliberately do not
    change the reservation history.
    """
    if dry_run or not attempted_ids:
        return
    hist = load_json(HISTORY, {"clipped": []})
    if isinstance(hist, list):
        hist = {"clipped": hist}
    seen = set()
    for key in ("clipped", "attempted"):
        for record in hist.get(key, []) or []:
            sid = record.get("source_id") if isinstance(record, dict) else record
            if sid:
                seen.add(sid)
    today = datetime.date.today().isoformat()
    changed = False
    for sid in attempted_ids:
        if sid and sid not in seen:
            hist.setdefault("attempted", []).append({"source_id": sid, "date": today})
            seen.add(sid)
            changed = True
    if changed:
        _atomic_write_json(HISTORY, hist)
        log(f"recorded {len(attempted_ids)} attempted source(s) to no-repeat memory")

def ensure_sfx():
    sfx_dir = HERE / "config" / "sfx"
    if not list(sfx_dir.glob("*.wav")):
        try:
            run_tool("build_sfx.py")
        except Exception as e:
            log("build_sfx failed:", e)

def attempt_instagram_upload(short_path, caption, clip_num, summary_dict, entry_dict,
                              public_url=None, style=None, experiment=False):
    """Publish to the pinned Instagram account and return whether it was confirmed uploaded."""
    if not IG_ENABLED:
        detail = "ZERNIO_API/ZERNIO_INSTAGRAM_ID not configured"
        log(f"clip {clip_num}: Instagram upload skipped ({detail})")
        entry_dict["instagram_error"] = detail
        summary_dict.setdefault("instagram_errors", []).append({"clip": clip_num, "error": detail})
        return False

    try:
        # Zernio needs a PUBLIC url, not a local file path. Reuse the URL already accepted by
        # YouTube when available: hosting the same MP4 a second time can switch providers and
        # hand Instagram a URL that Zernio cannot fetch (for example tmpfiles' /dl/ fallback).
        if public_url is None:
            host = run_tool("host_public.py", "--video", short_path)
            public_url = host["url"]
        ig = run_tool("upload_instagram.py", "--video-url", public_url, "--caption", caption, "--confirm")
        media_id = ig.get("post_id") or ig.get("media_id")
        entry_dict["instagram_media_id"] = media_id
        if ig.get("duplicate"):
            entry_dict["instagram_duplicate"] = True
        log(f"clip {clip_num}: Instagram -> {media_id}")
        if media_id:
            # Only NOW claim the weekly experiment slot -- the clip already rendered with
            # this style, but a failed IG post shouldn't burn the week's only experiment.
            if experiment:
                try:
                    claim = run_tool("pick_weekly_style.py", "--consume")
                    experiment = bool(claim.get("consumed"))
                except Exception as e:
                    log("pick_weekly_style --consume failed:", e)
                    experiment = False
            log_ig_post(media_id, style=style, experiment=experiment,
                        context={"clip": clip_num, "hook": caption.split("\n", 1)[0][:120]})
        return bool(media_id)
    except Exception as e:
        log(f"clip {clip_num}: Instagram FAILED: {e}")
        note_rate_limit(summary_dict, "instagram", e)
        entry_dict["instagram_error"] = str(e)
        summary_dict.setdefault("instagram_errors", []).append({"clip": clip_num, "error": str(e)})
        return False


def attempt_tiktok_upload(short_path, caption, clip_num, summary_dict, entry_dict, privacy):
    """Publish the same finished MP4 directly to TikTok when the Content Posting credentials exist."""
    if not TIKTOK_ENABLED:
        entry_dict["tiktok_status"] = "not_configured"
        log(f"clip {clip_num}: TikTok upload skipped (credentials not configured)")
        return False
    try:
        tt = run_tool("upload_tiktok.py", "--video", short_path, "--title", caption,
                      "--privacy", privacy, "--confirm")
        entry_dict["tiktok_publish_id"] = tt.get("publish_id")
        log(f"clip {clip_num}: TikTok -> {tt.get('publish_id')}")
        return tt.get("status") == "uploaded"
    except Exception as e:
        log(f"clip {clip_num}: TikTok FAILED: {e}")
        summary_dict.setdefault("tiktok_errors", []).append({"clip": clip_num, "error": str(e)})
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--privacy", default="public", choices=["public", "unlisted", "private"])
    # Hybrid mode (2026-07-10): YouTube bot-walls datacenter IPs, so the DOWNLOAD half
    # runs on the self-hosted laptop runner (residential IP) with --download-only, and
    # the cloud job resumes from the artifact with --source/--source-meta.
    ap.add_argument("--download-only", action="store_true",
                    help="Find + download the source, write .tmp/source_meta.json, then stop")
    ap.add_argument("--reserve-source", action="store_true",
                    help="Reserve one unused source in tracked history before processing")
    ap.add_argument("--source", default=None,
                    help="Process this pre-downloaded source file (skips find+download)")
    ap.add_argument("--source-id", default=None,
                    help="Process this exact YouTube source id, even if it has prior clips")
    ap.add_argument("--extra-clips", action="store_true",
                    help="For --source-id, select only new non-overlapping spans")
    ap.add_argument("--source-meta", default=None,
                    help="Manifest written by --download-only (source info + attempted ids)")
    ap.add_argument("--source-reservation", default=None,
                    help="Process the source selected by --reserve-source")
    ap.add_argument("--required-platforms",
                    default=os.environ.get("REQUIRED_PLATFORMS", "youtube,instagram"),
                    help="Comma-separated destinations that must publish for a zero exit.")
    args = ap.parse_args()
    # Scheduled quality-gated runs and explicit dry-run diagnostics may have no usable source on
    # a bot-limited YouTube day. That is an intentional no-post outcome. Real publishing runs
    # remain strict so an operator cannot mistake a missing source for a successful delivery.
    no_source_ok = os.environ.get("NO_SOURCE_OK") == "1"

    if args.reserve_source and (args.dry_run or args.source or args.source_id or args.source_reservation):
        raise RuntimeError("--reserve-source cannot be combined with --dry-run, --source, or --source-reservation")
    if args.source and (args.source_id or args.source_reservation):
        raise RuntimeError("--source, --source-id, and --source-reservation are mutually exclusive")
    if args.source_id and args.source_reservation:
        raise RuntimeError("--source-id and --source-reservation are mutually exclusive")
    if args.extra_clips and not (args.source_id or args.source):
        raise RuntimeError("--extra-clips requires --source-id or --source")

    required_platforms = {p.strip().lower() for p in args.required_platforms.split(",") if p.strip()}

    TMP.mkdir(parents=True, exist_ok=True)
    cfg = load_json(CONFIG, {})
    clips_per_day = int(cfg.get("clips_per_day", 6))
    duration_mode = str(cfg.get("clip_count_mode", "fixed")).strip().lower() == "duration"
    min_clips_per_run = int(cfg.get("min_clips_per_run", 2))
    if min_clips_per_run < 2:
        raise RuntimeError("config min_clips_per_run must be at least 2")
    if args.limit is not None:
        if args.limit < min_clips_per_run:
            raise RuntimeError(
                f"--limit {args.limit} would violate the minimum of {min_clips_per_run} clips per run"
            )
        clips_per_day = min(clips_per_day, args.limit)
    if clips_per_day < min_clips_per_run:
        raise RuntimeError(
            f"config clips_per_day ({clips_per_day}) is below the minimum of {min_clips_per_run} clips per run"
        )
    target = int(cfg.get("target_secs", 35))
    maxs = int(cfg.get("max_secs", 60))
    max_video_attempts = int(cfg.get("max_video_attempts", 5))
    history = load_json(HISTORY, {"clipped": []})
    requested_record = source_history_record(history, args.source_id) if args.source_id else None
    requested_windows = source_clip_windows(requested_record)
    if args.source_id and requested_record and not args.extra_clips:
        raise RuntimeError("--source-id already exists in history; add --extra-clips to avoid duplicate spans")
    if args.source_id and args.extra_clips and requested_record:
        known_target = requested_record.get("target_clip_count")
        if known_target is not None and len(requested_windows) >= int(known_target):
            summary = {"date": datetime.date.today().isoformat(), "dry_run": args.dry_run,
                       "status": "already_complete", "source_id": args.source_id,
                       "target_clip_count": int(known_target),
                       "existing_clip_count": len(requested_windows), "uploaded": [],
                       "errors": [], "warnings": []}
            print(json.dumps(summary, indent=2))
            return

    summary = {"date": datetime.date.today().isoformat(), "dry_run": args.dry_run,
               "clips_requested": clips_per_day,
               "target_clips_per_source_video": clips_per_day,
               "clip_count_mode": "duration" if duration_mode else "fixed",
               "clip_every_secs": int(cfg.get("clip_every_secs", 300)) if duration_mode else None,
               "minimum_clips_required": min_clips_per_run,
               "uploaded": [], "errors": [], "warnings": [],
               "required_platforms": sorted(required_platforms)}
    summary["delivery_budget"] = delivery_budget_guard(required_platforms, clips_per_day, cfg)
    # Duration-based runs do not know the exact source length until download metadata arrives.
    # Defer the strict guard in that mode; the post-download guard below uses the real target
    # and caps a long source to the remaining safe platform capacity.
    defer_budget_guard = duration_mode and args.limit is None
    if not args.dry_run and summary["delivery_budget"]["blocked"] and not defer_budget_guard:
        summary["status"] = "quota_guarded"
        summary["required_delivery_failures"] = sorted(summary["delivery_budget"]["blocked"])
        log("delivery budget guard stopped this run before rendering:",
            json.dumps(summary["delivery_budget"], sort_keys=True))
        print(json.dumps(summary, indent=2))
        # A full rolling quota is an intentional no-op, not a failed upload. Returning a
        # successful process status keeps scheduled Actions green while preserving the
        # structured reason in the log and summary above.
        return
    if args.dry_run and summary["delivery_budget"]["blocked"]:
        summary["delivery_budget"]["guard_bypassed"] = True
    # Crash-safe skip list: sources a prior interrupted run already picked on this machine.
    ledger_ids = load_ledger_ids()
    if ledger_ids:
        log(f"ledger: skipping {len(ledger_ids)} source(s) picked by earlier run(s)")

    # Actions reserves the source in a separate step and commits it before any expensive or
    # cancellable work starts. The reservation is then consumed here explicitly instead of
    # asking the finder again (which would see the reservation as already used).
    reserved_src = None
    reserved_attempts = []
    if args.source_reservation:
        reservation = load_json(args.source_reservation, {})
        reserved_src = reservation.get("src") if isinstance(reservation, dict) else None
        reserved_attempts = list(reservation.get("attempted") or []) if isinstance(reservation, dict) else []
        if not isinstance(reserved_src, dict) or not reserved_src.get("video_id") or not reserved_src.get("url"):
            if no_source_ok:
                payload = {"status": "no_source",
                           "detail": f"No source reservation was created; no video was published.",
                           "quality_floor": "1080p", "attempted_sources": reserved_attempts}
                print(json.dumps(payload, indent=2))
                return
            raise RuntimeError(f"invalid or missing source reservation: {args.source_reservation}")
        if reserved_src["video_id"] not in reserved_attempts:
            reserved_attempts.append(reserved_src["video_id"])

    if args.source_id:
        reserved_src = {
            "video_id": args.source_id,
            "url": f"https://www.youtube.com/watch?v={args.source_id}",
            "title": (requested_record or {}).get("source_title", ""),
            "channel": (requested_record or {}).get("channel", "MrBeast"),
            "reason": "explicit source id",
        }
        reserved_attempts = [args.source_id]

    if args.reserve_source:
        exclude = ",".join(sorted(ledger_ids))
        try:
            reserved_src = run_tool("find_source_video.py", "--exclude", exclude)
        except Exception as e:
            log("no source video to reserve:", e)
            payload = {"status": "no_source", "detail": str(e),
                       "quality_floor": "1080p", "attempted_sources": reserved_attempts}
            print(json.dumps(payload, indent=2))
            if no_source_ok:
                return
            sys.exit(1)
        reserved_attempts = [reserved_src["video_id"]]
        mark_ledger(reserved_src["video_id"])
        # This write is the payload committed by the workflow's reservation step. If the
        # later render/upload job is cancelled, the source is still permanently skipped.
        record_attempts(reserved_attempts, dry_run=False)
        reservation_path = TMP / "source_reservation.json"
        _atomic_write_json(reservation_path, {
            "src": reserved_src,
            "attempted": reserved_attempts,
            "reserved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        print(json.dumps({"status": "reserved", "video_id": reserved_src["video_id"],
                          "title": reserved_src.get("title"),
                          "reservation": str(reservation_path)}, indent=2))
        return

    video_attempts, attempted_videos = 0, list(reserved_attempts)
    clips = []
    src_path = ""
    src_title = ""
    src = {}
    active_source_record = requested_record
    active_existing_windows = list(requested_windows)
    source_duration = None
    existing_windows_path = None

    if args.source:
        # Hybrid cloud half: the laptop job already downloaded the source on a
        # residential IP -- resume from transcription. No retry loop here: if this
        # source can't be processed, the attempt is recorded below and the run ends.
        manifest = load_json(args.source_meta, {}) if args.source_meta else {}
        src = manifest.get("src") or {}
        attempted_videos = list(manifest.get("attempted") or [])
        if src.get("video_id") and src["video_id"] not in attempted_videos:
            attempted_videos.append(src["video_id"])
        clipped_history = load_json(HISTORY, {"clipped": []})
        clipped_records = clipped_history.get("clipped", []) if isinstance(clipped_history, dict) else []
        clipped_ids = {
            record.get("source_id") if isinstance(record, dict) else record
            for record in clipped_records or []
        }
        if src.get("video_id") in clipped_ids and not args.extra_clips:
            raise RuntimeError(
                f"refusing to process already-clipped source {src['video_id']} without --extra-clips"
            )
        src_path = args.source
        src_title = src.get("title") or "Video"
        source_duration = manifest.get("source_duration") or manifest.get("duration")
        active_source_record = source_history_record(history, src.get("video_id"))
        active_existing_windows = source_clip_windows(active_source_record)
        existing_windows_path = None
        if active_existing_windows:
            existing_windows_path = TMP / "existing_clip_windows.json"
            _atomic_write_json(existing_windows_path, {"windows": active_existing_windows})
        try:
            ensure_sfx()
            run_tool("transcribe_video.py", "--in", src_path)
            selection_args = ["--count", clips_per_day, "--target-secs", target, "--max-secs", maxs]
            if duration_mode and source_duration:
                target_total = duration_target_count(source_duration, cfg)
                continuing = bool(active_existing_windows) or bool(args.extra_clips)
                desired_count = max(0, target_total - len(active_existing_windows)) if continuing else target_total
                if args.limit is not None:
                    desired_count = min(desired_count, args.limit)
                summary.update({"source_duration_secs": source_duration,
                                "duration_target_clips": target_total,
                                "existing_clip_count": len(active_existing_windows),
                                "remaining_target_clips": desired_count})
                if desired_count <= 0:
                    summary.update({"status": "already_complete", "source_id": src.get("video_id"),
                                    "uploaded": [], "errors": [], "warnings": []})
                    print(json.dumps(summary, indent=2))
                    return
                if not args.dry_run:
                    capacity = delivery_capacity(required_platforms, cfg)
                    if capacity < min_clips_per_run:
                        raise RuntimeError("duration-based source has fewer than the minimum safe platform slots")
                    clips_per_day = min(desired_count, capacity)
                else:
                    clips_per_day = desired_count
                summary["clips_requested"] = clips_per_day
                summary["target_clips_per_source_video"] = target_total
                selection_args[1] = clips_per_day
            if existing_windows_path:
                selection_args += ["--exclude-windows", existing_windows_path]
            sel = run_tool("select_clips.py", *selection_args)
            clips = sel.get("clips", [])
            if len(clips) < min_clips_per_run:
                raise RuntimeError(
                    f"clip selector returned {len(clips)} clip(s); need at least {min_clips_per_run}"
                )
        except Exception as e:
            log(f"pre-downloaded source {src.get('video_id')} failed:", e)

    while not args.source and video_attempts < max_video_attempts:
        video_attempts += 1
        try:
            if reserved_src is not None:
                # The source was already reserved and pushed by the workflow preflight.
                src = reserved_src
                reserved_src = None
            else:
                # Tell the finder which sources to skip: ones we already tried this run PLUS
                # the crash-safe ledger (sources an earlier interrupted run already picked),
                # so it advances to the next video/channel instead of handing back a repeat.
                exclude = ",".join(attempted_videos + sorted(ledger_ids - set(attempted_videos)))
                src = run_tool("find_source_video.py", "--exclude", exclude)
        except Exception as e:
            log("no source video to clip today:", e)
            record_attempts(attempted_videos, args.dry_run)
            print(json.dumps({"status": "no_source", "detail": str(e),
                              "quality_floor": "1080p",
                              "attempted_sources": attempted_videos}, indent=2))
            return

        if src["video_id"] not in attempted_videos:
            attempted_videos.append(src["video_id"])
        # Flush to the crash-safe ledger NOW, before any download/render/upload. If the
        # local runner dies at any point below, this source is already recorded on disk and
        # the next run skips it -> no repeated video/clips. Dry runs are tests, so skip.
        if not args.dry_run:
            mark_ledger(src["video_id"])

        try:
            src_path = str(TMP / "source.mp4")
            # Preserve the highest useful source detail before the 1080x1920 crop. MrBeast's
            # wide challenge videos often expose a 1440p/2160p ladder; silently capping at 1080p
            # makes the vertical crop visibly soft.
            dl = run_tool("download_video.py", "--url", src["url"], "--out", src_path,
                          "--max-height", "2160", "--min-height", "1080")
            src_path = dl.get("path", src_path)
            log(f"downloaded source at {dl.get('width')}x{dl.get('height')}")  # visible res check
            src_title = src.get("title") or "Video"
            source_duration = dl.get("duration")
            active_source_record = source_history_record(history, src.get("video_id"))
            active_existing_windows = source_clip_windows(active_source_record)
            existing_windows_path = None
            if active_existing_windows:
                existing_windows_path = TMP / "existing_clip_windows.json"
                _atomic_write_json(existing_windows_path, {"windows": active_existing_windows})

            if duration_mode:
                target_total = duration_target_count(source_duration, cfg)
                continuing = bool(active_existing_windows) or bool(args.extra_clips)
                desired_count = max(0, target_total - len(active_existing_windows)) if continuing else target_total
                if args.limit is not None:
                    desired_count = min(desired_count, args.limit)
                summary.update({"source_duration_secs": source_duration,
                                "duration_target_clips": target_total,
                                "existing_clip_count": len(active_existing_windows),
                                "remaining_target_clips": desired_count})
                if desired_count <= 0:
                    summary.update({"status": "already_complete", "source_id": src.get("video_id"),
                                    "uploaded": [], "errors": [], "warnings": []})
                    print(json.dumps(summary, indent=2))
                    return
                if not args.dry_run:
                    capacity = delivery_capacity(required_platforms, cfg)
                    summary["delivery_budget"]["duration_target"] = target_total
                    summary["delivery_budget"]["existing_clip_count"] = len(active_existing_windows)
                    summary["delivery_budget"]["available_capacity"] = capacity
                    if capacity < min_clips_per_run:
                        summary["status"] = "quota_guarded"
                        summary["required_delivery_failures"] = sorted(required_platforms)
                        log("duration-based run stopped by provider budget/cooldown:",
                            json.dumps(summary["delivery_budget"], sort_keys=True))
                        print(json.dumps(summary, indent=2))
                        return
                    clips_per_day = min(desired_count, capacity)
                    summary["delivery_budget"]["capped_by_platform_budget"] = clips_per_day < desired_count
                else:
                    clips_per_day = desired_count
                summary["clips_requested"] = clips_per_day
                summary["target_clips_per_source_video"] = target_total
            selection_args = ["--count", clips_per_day, "--target-secs", target, "--max-secs", maxs]
            if existing_windows_path:
                selection_args += ["--exclude-windows", existing_windows_path]
            if args.download_only:
                # Hybrid laptop half: hand the source + everything the cloud job needs
                # to resume (src info, this run's failed picks) to the artifact and stop.
                manifest = {"src": src, "path": os.path.basename(src_path),
                            "width": dl.get("width"), "height": dl.get("height"),
                            "attempted": attempted_videos}
                with open(TMP / "source_meta.json", "w", encoding="utf-8") as f:
                    json.dump(manifest, f, ensure_ascii=False, indent=2)
                record_attempts(attempted_videos, args.dry_run)
                print(json.dumps({"status": "downloaded", "video_id": src["video_id"],
                                  "title": src_title, "path": src_path,
                                  "width": dl.get("width"), "height": dl.get("height"),
                                  "attempts": video_attempts}, indent=2))
                return
            ensure_sfx()
            run_tool("transcribe_video.py", "--in", src_path)
            sel = run_tool("select_clips.py", *selection_args)
            clips = sel.get("clips", [])
            if len(clips) < min_clips_per_run:
                raise RuntimeError(
                    f"clip selector returned {len(clips)} clip(s); need at least {min_clips_per_run}"
                )
            break
        except Exception as e:
            log(f"source {src.get('video_id')} failed (attempt {video_attempts}/{max_video_attempts}):", e)
            if args.source_id:
                break
            continue

    if not clips:
        detail = ("No source produced the required high-resolution clips; no video was published."
                  if attempted_videos else "No source was available; no video was published.")
        log(detail)
        record_attempts(attempted_videos, args.dry_run)
        summary.update({"status": "no_source", "detail": detail,
                        "quality_floor": "1080p", "attempted_sources": attempted_videos})
        print(json.dumps(summary, indent=2))
        if no_source_ok:
            return
        sys.exit(1)
    summary["clips_selected"] = len(clips)
    if len(clips) < clips_per_day:
        log(f"clip selector returned {len(clips)} of target {clips_per_day}; "
            f"continuing only because the minimum is {min_clips_per_run}")

    uploaded_ids = []
    for idx, clip in enumerate(clips, start=1):
        n = f"{idx:02d}"
        hook = clip.get("suggested_title") or clip.get("hook") or "Clip"
        short = str(TMP / f"short_{n}.mp4")

        # Weekly style experiment (2026-07-12): the FIRST clip of whichever daily run
        # happens first in a new ISO week TRIES that week's rotated caption style; the
        # slot is only actually claimed (see attempt_instagram_upload below) once that
        # clip's Instagram post succeeds, so a failed upload doesn't burn the week's
        # only experiment. Every other clip stays on the normal "hormozi" default. See
        # tools/pick_weekly_style.py.
        clip_style, is_experiment = "hormozi", False
        if idx == 1 and not args.dry_run:
            try:
                weekly = run_tool("pick_weekly_style.py")  # peek only, don't claim yet
                if weekly.get("style") and not weekly.get("used"):
                    clip_style, is_experiment = weekly["style"], True
                    log(f"clip {n}: trying weekly style experiment -> {clip_style}")
            except Exception as e:
                log("pick_weekly_style failed (falling back to hormozi):", e)

        # 1. Process the video (if this fails, skip to next clip)
        try:
            reframed = str(TMP/f"reframed_{n}.mp4")
            cues = str(TMP/f"cues_{n}.json")
            caps = str(TMP/f"caps_{n}.ass")

            run_tool("reframe_crop.py", "--in", src_path, "--start", clip["start"], "--end", clip["end"], "--out", reframed)
            run_tool("plan_effects.py", "--start", clip["start"], "--end", clip["end"], "--emphasis", ",".join(clip.get("emphasis_words", [])), "--out", cues)
            run_tool("build_captions.py", "--start", clip["start"], "--end", clip["end"], "--style", clip_style, "--hook", hook, "--out", caps)
            run_tool("render_clip.py", "--in", reframed, "--captions", caps, "--cues", cues, "--out", short, "--max-secs", maxs)
        except Exception as e:
            log(f"clip {n} RENDER FAILED:", e)
            summary["errors"].append({"clip": n, "stage": "render", "error": str(e)})
            continue
            
        try:
            tags = run_tool("generate_hashtags.py", "--title", src_title, "--hook", hook, "--snippet", hook)
        except Exception as e:
            # Hashtag generation is deliberately best-effort. The provider chain can spend
            # its full timeout budget when free LLM routes are degraded; the rendered MP4 is
            # still valid and should continue through the configured delivery paths.
            log(f"clip {n} HASHTAG GENERATION FAILED (using fallback tags):", e)
            summary["warnings"].append({"clip": n, "stage": "hashtags",
                                         "error": str(e), "fallback": "base"})
            tags = {"hashtags": FALLBACK_HASHTAGS, "provider": None,
                    "note": "LLM hashtags unavailable; used base tags."}
        entry = {"clip": n, "source_start": clip["start"], "source_end": clip["end"]}
        if tags.get("provider") is None:
            entry["hashtags_fallback"] = True

        # Richer metadata than a bare hook: "#Shorts" in the title (kept under YouTube's
        # 100-char limit), hashtags in the description where YouTube surfaces them, and a
        # source credit (standard practice for clip channels).
        tag_list = tags.get("hashtags", [])
        yt_title = hook if len(hook) > 92 else f"{hook} #Shorts"
        hashtag_line = " ".join(f"#{t}" for t in tag_list[:10])
        description = f"{hook}\n\n{hashtag_line}\n\nCredit: {src.get('channel') or 'MrBeast'}"

        # 2. Try YouTube (If this fails, log it but keep going!) -- needs a PUBLIC url,
        # not the local path, since it now publishes via Zernio instead of OAuth.
        yt_ok = False
        public_url = None
        if "youtube" in summary.get("rate_limited_platforms", {}):
            entry["youtube_error"] = "skipped after Zernio account rate limit"
        else:
            try:
                host = run_tool("host_public.py", "--video", short)
                public_url = host.get("url")
                up_args = ["upload_youtube.py", "--video-url", public_url, "--title", yt_title,
                           "--description", description, "--tags", ",".join(tag_list),
                           "--privacy", args.privacy]
                if not args.dry_run:
                    up_args.append("--confirm")
                up = run_tool(*up_args)
                if not args.dry_run:
                    yt_id = up.get("post_id")
                    uploaded_ids.append(yt_id)
                    entry["video_id"] = yt_id
                    yt_ok = bool(yt_id)
            except Exception as e:
                log(f"clip {n} YOUTUBE FAILED:", e)
                note_rate_limit(summary, "youtube", e)
                entry["youtube_error"] = str(e)
        entry["youtube_uploaded"] = yt_ok
            
        if args.dry_run:
            summary["uploaded"].append({"clip": n, "preview": True})
            continue
            
        # 3. Try Instagram (This will now run even if YouTube fails)
        caption = f"{hook}\n\n{hashtag_line}"
        if "instagram" in summary.get("rate_limited_platforms", {}):
            entry["instagram_error"] = "skipped after Zernio account rate limit"
            ig_ok = False
        else:
            ig_ok = attempt_instagram_upload(short, caption, n, summary, entry,
                                              public_url=public_url,
                                              style=clip_style, experiment=is_experiment)
        entry["instagram_uploaded"] = ig_ok
        tiktok_ok = attempt_tiktok_upload(short, caption, n, summary, entry,
                                           os.environ.get("TIKTOK_PRIVACY", "PUBLIC_TO_EVERYONE"))
        entry["tiktok_uploaded"] = tiktok_ok

        summary["uploaded"].append(entry)

        # 4. This clip is finally used (uploaded/hosted) -> delete its intermediates so .tmp/
        # doesn't accumulate across runs (2026-07-09, Moemen's request). Dry runs keep them for
        # inspection. The shared source.mp4 is removed once, after the whole clip loop.
        if not args.dry_run and not KEEP_RENDERED_CLIPS:
            purge_files(short, reframed, caps, cues)

    successful_entries = [e for e in summary["uploaded"]
                          if e.get("video_id") or e.get("instagram_media_id") or e.get("tiktok_publish_id")]
    summary["clips_with_any_upload"] = len(successful_entries)
    if not args.dry_run and successful_entries:
        hist = load_json(HISTORY, {"clipped": []})
        if isinstance(hist, list):
            hist = {"clipped": hist}
        source_id = src.get("video_id", "unknown")
        record = source_history_record(hist, source_id)
        if record is None:
            record = {"source_id": source_id, "clip_video_ids": [],
                      "youtube_post_ids": [], "instagram_post_ids": [],
                      "tiktok_post_ids": [], "platform_post_ids": [], "clip_windows": []}
            hist.setdefault("clipped", []).append(record)

        def extend_unique(field, values):
            current = list(record.get(field) or [])
            for value in values:
                if value and value not in current:
                    current.append(value)
            record[field] = current

        extend_unique("clip_video_ids", [e.get("video_id") for e in successful_entries])
        extend_unique("youtube_post_ids", [e.get("video_id") for e in successful_entries])
        extend_unique("instagram_post_ids", [e.get("instagram_media_id") for e in successful_entries])
        extend_unique("tiktok_post_ids", [e.get("tiktok_publish_id") for e in successful_entries])
        # Preserve the original combined field for older reporting scripts.
        extend_unique("platform_post_ids", [x for e in successful_entries
                                             for x in (e.get("instagram_media_id"),
                                                       e.get("tiktok_publish_id")) if x])

        known_windows = source_clip_windows(record)
        for entry in successful_entries:
            try:
                start, end = float(entry["source_start"]), float(entry["source_end"])
            except (KeyError, TypeError, ValueError):
                continue
            if not any(abs(w["start"] - start) < 0.01 and abs(w["end"] - end) < 0.01
                       for w in known_windows):
                known_windows.append({"start": start, "end": end,
                                      "video_id": entry.get("video_id")})

        record.update({
            "source_title": src_title,
            "channel": src.get("channel") or record.get("channel") or "MrBeast",
            "date": summary["date"],
            "published_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "clip_windows": known_windows,
        })
        if source_duration:
            record["source_duration"] = source_duration
        target_count = summary.get("duration_target_clips") or record.get("target_clip_count")
        if target_count is not None:
            record["target_clip_count"] = int(target_count)
            record["remaining_clips"] = max(0, int(target_count) - len(known_windows))
        with open(HISTORY, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
        log(f"history updated: source posted on {len(successful_entries)} clip(s)")

    # Remember every source we touched this run (incl. any earlier failed attempts)
    # so none of them come back next run.
    record_attempts(attempted_videos, args.dry_run)

    # The source video + transcript are now finally used (all clips built) -> delete them, plus
    # any stray intermediates from clips that failed mid-render (2026-07-09, Moemen's request).
    if not args.dry_run:
        purge_files(src_path, str(TMP / "transcript.json"))
        patterns = ("reframed_*.mp4", "caps_*.ass", "cues_*.json", "existing_clip_windows.json")
        if not KEEP_RENDERED_CLIPS:
            patterns += ("short_*.mp4",)
        for pat in patterns:
            for p in TMP.glob(pat):
                purge_files(str(p))

    required_failures = []
    if not args.dry_run:
        posted_entries = summary["uploaded"]
        if "youtube" in required_platforms and (
                len(posted_entries) < len(clips) or
                any(e.get("youtube_uploaded") is not True for e in posted_entries)):
            required_failures.append("youtube")
        if "instagram" in required_platforms and (
                len(posted_entries) < len(clips) or
                any(e.get("instagram_uploaded") is not True for e in posted_entries)):
            required_failures.append("instagram")
        if summary["errors"] and "render" not in required_failures:
            required_failures.append("pipeline")
        if required_failures:
            summary["required_delivery_failures"] = required_failures
            summary["status"] = "delivery_failed"
        elif successful_entries:
            summary["status"] = "uploaded"
        else:
            summary["status"] = "built_no_delivery"

    print(json.dumps(summary, indent=2))
    if required_failures:
        sys.exit(1)

if __name__ == "__main__":
    main()
