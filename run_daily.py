"""Headless daily orchestrator for the clipping automation."""
import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
TMP = HERE / ".tmp"
CONFIG = HERE / "config" / "channels.json"
HISTORY = HERE / "state" / "clipped_history.json"
# Crash-safe local ledger: written the instant a source is picked, BEFORE any heavy
# download/transcribe/render/upload. Untracked + gitignored, so `actions/checkout`
# (clean: false) keeps it across runs on the same laptop -- unlike the tracked history
# file, which checkout force-resets to the last PUSHED version. If the machine powers
# off mid-run, the source is already recorded here, so the next run skips it and never
# re-clips the same video. This is what survives a hard shutdown; clipped_history.json
# (committed at the end) only survives a clean finish.
LEDGER = HERE / "state" / "attempted_local.json"

try:
    from dotenv import load_dotenv
    load_dotenv(HERE / "API.env")
except ImportError:
    pass

# --- Zernio secret keys (passed as env vars by the workflow; in API.env for local runs) ---
IG_ENABLED = bool(os.environ.get("ZERNIO_API")) and bool(os.environ.get("ZERNIO_INSTAGRAM_ID"))

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

def run_tool(script, *args):
    cmd = [sys.executable, str(TOOLS / script), *map(str, args)]
    log("->", script, *[a for a in args])
    proc = subprocess.run(cmd, cwd=str(HERE), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    data = _extract_json(proc.stdout)
    if data is None:
        raise RuntimeError(f"{script}: no JSON output (exit {proc.returncode}). stderr: {(proc.stderr or '')[-400:]}")
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"{script}: {data['error']}")
    return data

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def load_ledger_ids():
    """Source ids picked by a prior (possibly interrupted) run on this machine."""
    data = load_json(LEDGER, {"attempted": []})
    records = (data.get("attempted") if isinstance(data, dict) else data) or []
    ids = set()
    for r in records:
        sid = r.get("source_id") if isinstance(r, dict) else r
        if sid:
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
    """No-repeat memory: persist every source id we picked this run (success OR fail)
    so a future run never re-selects it. The workflow commits this file back even when
    the run fails, so a video that breaks once won't be retried forever. Dry runs don't
    record (they're just tests)."""
    if dry_run or not attempted_ids:
        return
    hist = load_json(HISTORY, {"clipped": []})
    if isinstance(hist, list):
        hist = {"clipped": hist}
    seen = {r.get("source_id") if isinstance(r, dict) else r for r in hist.get("attempted", [])}
    today = datetime.date.today().isoformat()
    changed = False
    for sid in attempted_ids:
        if sid and sid not in seen:
            hist.setdefault("attempted", []).append({"source_id": sid, "date": today})
            seen.add(sid)
            changed = True
    if changed:
        with open(HISTORY, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
        log(f"recorded {len(attempted_ids)} attempted source(s) to no-repeat memory")

def ensure_sfx():
    sfx_dir = HERE / "config" / "sfx"
    if not list(sfx_dir.glob("*.wav")):
        try:
            run_tool("build_sfx.py")
        except Exception as e:
            log("build_sfx failed:", e)

def attempt_instagram_upload(short_path, caption, clip_num, summary_dict, entry_dict):
    """Isolated Instagram logic that won't crash the main pipeline."""
    if not IG_ENABLED:
        log(f"clip {clip_num}: Instagram upload skipped (ZERNIO_API/ZERNIO_INSTAGRAM_ID not configured)")
        return

    try:
        # Zernio needs a PUBLIC url, not a local file path -- host it first.
        host = run_tool("host_public.py", "--video", short_path)
        ig = run_tool("upload_instagram.py", "--video-url", host["url"], "--caption", caption, "--confirm")
        entry_dict["instagram_media_id"] = ig.get("post_id") or ig.get("media_id")
        log(f"clip {clip_num}: Instagram -> {entry_dict['instagram_media_id']}")
    except Exception as e:
        log(f"clip {clip_num}: Instagram FAILED: {e}")
        summary_dict.setdefault("instagram_errors", []).append({"clip": clip_num, "error": str(e)})


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
    ap.add_argument("--source", default=None,
                    help="Process this pre-downloaded source file (skips find+download)")
    ap.add_argument("--source-meta", default=None,
                    help="Manifest written by --download-only (source info + attempted ids)")
    args = ap.parse_args()

    TMP.mkdir(parents=True, exist_ok=True)
    cfg = load_json(CONFIG, {})
    clips_per_day = int(cfg.get("clips_per_day", 6))
    if args.limit:
        clips_per_day = min(clips_per_day, args.limit)
    target = int(cfg.get("target_secs", 35))
    maxs = int(cfg.get("max_secs", 60))
    max_video_attempts = int(cfg.get("max_video_attempts", 5))

    summary = {"date": datetime.date.today().isoformat(), "dry_run": args.dry_run, "uploaded": [], "errors": []}
    # Crash-safe skip list: sources a prior interrupted run already picked on this machine.
    ledger_ids = load_ledger_ids()
    if ledger_ids:
        log(f"ledger: skipping {len(ledger_ids)} source(s) picked by earlier run(s)")
    video_attempts, attempted_videos = 0, []
    clips = []
    src_path = ""
    src_title = ""
    src = {}

    if args.source:
        # Hybrid cloud half: the laptop job already downloaded the source on a
        # residential IP -- resume from transcription. No retry loop here: if this
        # source can't be processed, the attempt is recorded below and the run ends.
        manifest = load_json(args.source_meta, {}) if args.source_meta else {}
        src = manifest.get("src") or {}
        attempted_videos = list(manifest.get("attempted") or [])
        if src.get("video_id") and src["video_id"] not in attempted_videos:
            attempted_videos.append(src["video_id"])
        src_path = args.source
        src_title = src.get("title") or "Video"
        try:
            ensure_sfx()
            run_tool("transcribe_video.py", "--in", src_path)
            sel = run_tool("select_clips.py", "--count", clips_per_day, "--target-secs", target, "--max-secs", maxs)
            clips = sel.get("clips", [])
        except Exception as e:
            log(f"pre-downloaded source {src.get('video_id')} failed:", e)

    while not args.source and video_attempts < max_video_attempts:
        video_attempts += 1
        try:
            # Tell the finder which sources to skip: ones we already tried this run PLUS
            # the crash-safe ledger (sources an earlier interrupted run already picked),
            # so it advances to the next video/channel instead of handing back a repeat.
            exclude = ",".join(attempted_videos + sorted(ledger_ids - set(attempted_videos)))
            src = run_tool("find_source_video.py", "--exclude", exclude)
        except Exception as e:
            log("no source video to clip today:", e)
            record_attempts(attempted_videos, args.dry_run)
            print(json.dumps({"status": "no_source", "detail": str(e)}))
            return

        attempted_videos.append(src["video_id"])
        # Flush to the crash-safe ledger NOW, before any download/render/upload. If the
        # laptop dies at any point below, this source is already recorded on disk and the
        # next run skips it -> no repeated video/clips. Dry runs are tests, so skip.
        if not args.dry_run:
            mark_ledger(src["video_id"])

        try:
            src_path = str(TMP / "source.mp4")
            dl = run_tool("download_video.py", "--url", src["url"], "--out", src_path)
            src_path = dl.get("path", src_path)
            log(f"downloaded source at {dl.get('width')}x{dl.get('height')}")  # visible res check
            src_title = src.get("title") or "Video"
            if args.download_only:
                # Hybrid laptop half: hand the source + everything the cloud job needs
                # to resume (src info, this run's failed picks) to the artifact and stop.
                manifest = {"src": src, "path": os.path.basename(src_path),
                            "width": dl.get("width"), "height": dl.get("height"),
                            "attempted": attempted_videos}
                with open(TMP / "source_meta.json", "w", encoding="utf-8") as f:
                    json.dump(manifest, f, ensure_ascii=False, indent=2)
                print(json.dumps({"status": "downloaded", "video_id": src["video_id"],
                                  "title": src_title, "path": src_path,
                                  "width": dl.get("width"), "height": dl.get("height"),
                                  "attempts": video_attempts}, indent=2))
                return
            ensure_sfx()
            run_tool("transcribe_video.py", "--in", src_path)
            sel = run_tool("select_clips.py", "--count", clips_per_day, "--target-secs", target, "--max-secs", maxs)
            clips = sel.get("clips", [])
            break
        except Exception as e:
            log(f"source {src.get('video_id')} failed (attempt {video_attempts}/{max_video_attempts}):", e)
            continue

    if not clips:
        log("Failed to find or process clips.")
        record_attempts(attempted_videos, args.dry_run)
        sys.exit(1)

    uploaded_ids = []
    for idx, clip in enumerate(clips, start=1):
        n = f"{idx:02d}"
        hook = clip.get("suggested_title") or clip.get("hook") or "Clip"
        short = str(TMP / f"short_{n}.mp4")
        
        # 1. Process the video (if this fails, skip to next clip)
        try:
            reframed = str(TMP/f"reframed_{n}.mp4")
            cues = str(TMP/f"cues_{n}.json")
            caps = str(TMP/f"caps_{n}.ass")

            run_tool("reframe_crop.py", "--in", src_path, "--start", clip["start"], "--end", clip["end"], "--out", reframed)
            run_tool("plan_effects.py", "--start", clip["start"], "--end", clip["end"], "--emphasis", ",".join(clip.get("emphasis_words", [])), "--out", cues)
            run_tool("build_captions.py", "--start", clip["start"], "--end", clip["end"], "--style", "hormozi", "--hook", hook, "--out", caps)
            run_tool("render_clip.py", "--in", reframed, "--captions", caps, "--cues", cues, "--out", short, "--max-secs", maxs)
        except Exception as e:
            log(f"clip {n} RENDER FAILED:", e)
            continue
            
        tags = run_tool("generate_hashtags.py", "--title", src_title, "--hook", hook, "--snippet", hook)
        entry = {"clip": n}

        # Richer metadata than a bare hook: "#Shorts" in the title (kept under YouTube's
        # 100-char limit), hashtags in the description where YouTube surfaces them, and a
        # source credit (standard practice for clip channels).
        tag_list = tags.get("hashtags", [])
        yt_title = hook if len(hook) > 92 else f"{hook} #Shorts"
        hashtag_line = " ".join(f"#{t}" for t in tag_list[:10])
        description = f"{hook}\n\n{hashtag_line}\n\nCredit: {src.get('channel') or 'MrBeast'}"

        # 2. Try YouTube (If this fails, log it but keep going!) -- needs a PUBLIC url,
        # not the local path, since it now publishes via Zernio instead of OAuth.
        try:
            host = run_tool("host_public.py", "--video", short)
            up_args = ["upload_youtube.py", "--video-url", host["url"], "--title", yt_title,
                       "--description", description, "--tags", ",".join(tag_list),
                       "--privacy", args.privacy]
            if not args.dry_run:
                up_args.append("--confirm")
            up = run_tool(*up_args)
            if not args.dry_run:
                yt_id = up.get("post_id")
                uploaded_ids.append(yt_id)
                entry["video_id"] = yt_id
        except Exception as e:
            log(f"clip {n} YOUTUBE FAILED:", e)
            entry["youtube_error"] = str(e)
            
        if args.dry_run:
            summary["uploaded"].append({"clip": n, "preview": True})
            continue
            
        # 3. Try Instagram (This will now run even if YouTube fails)
        attempt_instagram_upload(short, f"{hook}\n\n{hashtag_line}", n, summary, entry)

        summary["uploaded"].append(entry)

        # 4. This clip is finally used (uploaded/hosted) -> delete its intermediates so .tmp/
        # doesn't accumulate across runs (2026-07-09, Moemen's request). Dry runs keep them for
        # inspection. The shared source.mp4 is removed once, after the whole clip loop.
        if not args.dry_run:
            purge_files(short, reframed, caps, cues)

    if not args.dry_run and uploaded_ids:
        hist = load_json(HISTORY, {"clipped": []})
        if isinstance(hist, list):
            hist = {"clipped": hist}
        hist.setdefault("clipped", []).append({
            "source_id": src.get("video_id", "unknown"), 
            "source_title": src_title,
            "date": summary["date"],
            "clip_video_ids": uploaded_ids,
        })
        with open(HISTORY, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
        log(f"history updated: +{len(uploaded_ids)} clips")

    # Remember every source we touched this run (incl. any earlier failed attempts)
    # so none of them come back next run.
    record_attempts(attempted_videos, args.dry_run)

    # The source video + transcript are now finally used (all clips built) -> delete them, plus
    # any stray intermediates from clips that failed mid-render (2026-07-09, Moemen's request).
    if not args.dry_run:
        purge_files(src_path, str(TMP / "transcript.json"))
        for pat in ("reframed_*.mp4", "caps_*.ass", "cues_*.json", "short_*.mp4"):
            for p in TMP.glob(pat):
                purge_files(str(p))

    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
