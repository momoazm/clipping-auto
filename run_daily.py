"""Headless daily orchestrator for the clipping automation (no agent, no human).

Runs the whole pipeline end-to-end and publishes the clips:
  find_source_video -> download_video -> probe_video -> transcribe_video ->
  select_clips -> per clip [ reframe_crop -> plan_effects -> build_captions ->
  render_clip -> generate_hashtags -> upload_youtube ] -> append to clipped_history.

Built for GitHub Actions (cron) but runnable locally. Step logs go to STDERR; a single
JSON run-summary is printed to STDOUT.

Usage:
    python run_daily.py [--dry-run] [--limit N] [--privacy public|unlisted|private]
      --dry-run : do everything EXCEPT a real upload (upload runs as a gate preview) and
                  do NOT write history. Use for cheap local validation.
      --limit N : cap clips produced this run (testing).
"""
import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
TMP = HERE / ".tmp"
CONFIG = HERE / "config" / "channels.json"
HISTORY = HERE / "state" / "clipped_history.json"


def log(*a):
    print("[run_daily]", *a, file=sys.stderr, flush=True)


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
        raise RuntimeError(f"{script}: no JSON output (exit {proc.returncode}). "
                           f"stderr: {(proc.stderr or '')[-400:]}")
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"{script}: {data['error']}")
    return data


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def resolve_music_bed(cfg):
    """Ensure the background-music bed exists and return the --music value render uses.

    The user's chosen background sound: if config sets `background_music_url`, extract
    that track's audio into config/music/bg.mp3 and use it on EVERY clip. The bed is
    gitignored and regenerated each run (the download works on the residential self-
    hosted runner). Falls back to the synthesized beds (build_music.py + "auto") if no
    URL is set or extraction fails.
    """
    music_dir = HERE / "config" / "music"
    url = (cfg.get("background_music_url") or "").strip()
    if url:
        bed = music_dir / "bg.mp3"
        if not bed.is_file():
            try:
                run_tool("extract_bg_audio.py", "--url", url, "--out", str(bed))
            except Exception as e:
                log("extract_bg_audio failed; will try a synthesized bed:", e)
        if bed.is_file():
            return str(bed)
    # Fallback: synthesized beds (regenerated if missing).
    if not list(music_dir.glob("*.mp3")):
        try:
            run_tool("build_music.py")
        except Exception as e:
            log("build_music failed (clips will render without music):", e)
    return "auto"


def append_history(src, src_title, clip_video_ids, date):
    """Append one processed-source record to the permanent no-repeat history file."""
    hist = load_json(HISTORY, {"clipped": []})
    if isinstance(hist, list):
        hist = {"clipped": hist}
    hist.setdefault("clipped", []).append({
        "source_id": src["video_id"], "source_title": src_title,
        "channel": src.get("channel"), "date": date,
        "clip_video_ids": clip_video_ids,
    })
    with open(HISTORY, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)


def process_clip(clip, slot, src_path, src_title, music_arg, maxs, privacy, dry_run):
    """Render + upload a single clip. Returns the upload dict (with _title); raises on
    any step failure so the caller can skip just this clip."""
    n = f"{slot:02d}"
    start, end = clip["start"], clip["end"]
    emph = ",".join(clip.get("emphasis_words") or [])
    hook = clip.get("suggested_title") or clip.get("hook") or src_title
    reframed = str(TMP / f"reframed_{n}.mp4")
    cues = str(TMP / f"cues_{n}.json")
    caps = str(TMP / f"caps_{n}.ass")
    short = str(TMP / f"short_{n}.mp4")

    run_tool("reframe_crop.py", "--in", src_path, "--start", start, "--end", end, "--out", reframed)
    run_tool("plan_effects.py", "--start", start, "--end", end, "--emphasis", emph, "--out", cues)
    run_tool("build_captions.py", "--start", start, "--end", end,
             "--style", "hormozi", "--hook", hook, "--out", caps)
    # Every clip gets the chosen background-music bed; SFX are disabled (--no-sfx) per
    # user pref. Visual punch-ins from --cues are kept.
    run_tool("render_clip.py", "--in", reframed, "--captions", caps,
             "--cues", cues, "--out", short, "--max-secs", maxs,
             "--music", music_arg, "--no-sfx")
    tags = run_tool("generate_hashtags.py", "--title", src_title,
                    "--hook", clip.get("hook") or hook, "--snippet", clip.get("hook") or "")
    hashtags = tags.get("hashtags", [])
    # YouTube ignores ALL description hashtags if there are more than 15, so cap the
    # visible #tags at 15 while still passing the full enriched set to the tags metadata.
    desc_tags = hashtags[:15]
    desc = (clip.get("hook") or "") + "\n\n" + " ".join("#" + t for t in desc_tags)
    up_args = ["upload_youtube.py", "--video", short, "--title", hook,
               "--description", desc, "--tags", ",".join(hashtags) or "shorts",
               "--privacy", privacy]
    if not dry_run:
        up_args.append("--confirm")
    up = run_tool(*up_args)
    up["_title"] = hook
    return up


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--privacy", default="public",
                    choices=["public", "unlisted", "private"])
    args = ap.parse_args()

    TMP.mkdir(parents=True, exist_ok=True)
    cfg = load_json(CONFIG, {})
    target_clips = int(cfg.get("clips_per_day", 6))   # the "jobs" to finish each run
    if args.limit:
        target_clips = min(target_clips, args.limit)
    target = int(cfg.get("target_secs", 60))
    maxs = int(cfg.get("max_secs", 120))
    # Keep pulling fresh source videos until target_clips clips are actually uploaded.
    # One video may yield only a few usable clips (or some fail), so allow several.
    max_sources = int(cfg.get("max_source_attempts", max(target_clips, 8)))

    summary = {"date": datetime.date.today().isoformat(), "dry_run": args.dry_run,
               "target_clips": target_clips, "sources": [], "uploaded": [], "errors": []}

    music_arg = resolve_music_bed(cfg)
    summary["music"] = Path(music_arg).name if music_arg != "auto" else "auto"

    jobs_done = 0      # successful clips (real uploads, or dry-run previews)
    slot = 0           # global clip index -> unique .tmp filenames across sources
    processed = []     # source ids tried this run (skip on re-pick, success OR fail)
    last_error = None

    while jobs_done < target_clips and len(processed) < max_sources:
        # 1. Pick a fresh source (skips history + anything already tried this run).
        try:
            src = run_tool("find_source_video.py", "--exclude", ",".join(processed))
        except Exception as e:
            if jobs_done == 0:
                log("no source video to clip today:", e)
                summary["status"] = "no_source"
                summary["detail"] = str(e)
                print(json.dumps(summary, indent=2))
                return
            log("no more source videos available:", e)
            break

        video_id = src["video_id"]
        if video_id in processed:        # safety net (--exclude should prevent this)
            log(f"source {video_id} already tried this run; stopping.")
            break
        processed.append(video_id)
        summary["sources"].append(src)
        log(f"source {len(processed)}/{max_sources}: {video_id} - {src.get('title')} "
            f"({src['reason']}) | {jobs_done}/{target_clips} clips so far")

        # 2. Fetch + transcribe + select only as many clips as we still need.
        try:
            src_path = str(TMP / "source.mp4")
            dl = run_tool("download_video.py", "--url", src["url"], "--out", src_path)
            src_path = dl.get("path", src_path)
            src_title = src.get("title") or dl.get("title") or "MrBeast"

            probe = run_tool("probe_video.py", "--in", src_path)
            if not probe.get("has_audio"):
                raise RuntimeError("source has no audio track; cannot caption.")

            run_tool("transcribe_video.py", "--in", src_path)
            need = target_clips - jobs_done
            sel = run_tool("select_clips.py", "--count", need,
                           "--target-secs", target, "--max-secs", maxs)
            clips = sel.get("clips", [])
            log(f"selected {len(clips)} clips via {sel.get('provider')} (needed {need})")
        except Exception as e:
            last_error = str(e)
            log(f"source {video_id} failed before clipping: {e}")
            summary["errors"].append({"source": video_id, "error": last_error})
            continue   # try the next source; not recorded in permanent history

        # 3. Render + upload each clip until this source runs out or we hit target.
        clip_ids = []
        for clip in clips:
            if jobs_done >= target_clips:
                break
            slot += 1
            try:
                up = process_clip(clip, slot, src_path, src_title, music_arg,
                                  maxs, args.privacy, args.dry_run)
            except Exception as e:
                log(f"clip {slot:02d} FAILED:", e)
                summary["errors"].append({"clip": f"{slot:02d}", "error": str(e)})
                continue
            jobs_done += 1
            title = up.get("_title")
            if args.dry_run:
                log(f"clip {slot:02d}: DRY preview -> channel {up.get('channel_title')}")
                summary["uploaded"].append({"clip": f"{slot:02d}", "preview": True, "title": title})
            else:
                clip_ids.append(up.get("video_id"))
                log(f"clip {slot:02d}: uploaded {up.get('url')}  [{jobs_done}/{target_clips}]")
                summary["uploaded"].append({"clip": f"{slot:02d}", "video_id": up.get("video_id"),
                                            "url": up.get("url"), "title": title})

        # 4. Record this source so it's never repeated (only if it produced clips).
        if not args.dry_run and clip_ids:
            append_history(src, src_title, clip_ids, summary["date"])
            log(f"history updated: {video_id} (+{len(clip_ids)} clips)")

        # Dry-run validates one source; don't spin through the whole catalog.
        if args.dry_run:
            break

    summary["uploaded_count"] = len([u for u in summary["uploaded"] if not u.get("preview")])
    summary["jobs_done"] = jobs_done
    if jobs_done >= target_clips:
        summary["status"] = "ok"
    elif jobs_done > 0:
        summary["status"] = "partial"
        summary["detail"] = (f"only {jobs_done}/{target_clips} clips after "
                             f"{len(processed)} source(s); {last_error or 'sources exhausted'}")
    else:
        summary["status"] = "error"
        summary["detail"] = last_error or "no clips produced"
    print(json.dumps(summary, indent=2))
    if jobs_done == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
