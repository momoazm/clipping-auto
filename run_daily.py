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

try:
    from dotenv import load_dotenv
    load_dotenv(HERE / "API.env")
except ImportError:
    pass

# --- Zernio secret keys ---
IG_ENABLED = bool(os.environ.get("ZERNIO_API")) and bool(os.environ.get("ZERNIO_INSTAGRAM_ID"))

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
    video_attempts, attempted_videos = 0, []
    clips = []
    src_path = ""
    src_title = ""
    src = {}

    while video_attempts < max_video_attempts:
        video_attempts += 1
        try:
            src = run_tool("find_source_video.py")
        except Exception as e:
            log("no source video to clip today:", e)
            print(json.dumps({"status": "no_source", "detail": str(e)}))
            return

        if src["video_id"] in attempted_videos:
            continue
        attempted_videos.append(src["video_id"])

        try:
            ensure_sfx()
            src_path = str(TMP / "source.mp4")
            dl = run_tool("download_video.py", "--url", src["url"], "--out", src_path)
            src_path = dl.get("path", src_path)
            src_title = src.get("title") or "Video"
            run_tool("transcribe_video.py", "--in", src_path)
            sel = run_tool("select_clips.py", "--count", clips_per_day, "--target-secs", target, "--max-secs", maxs)
            clips = sel.get("clips", [])
            break
        except Exception as e:
            continue

    if video_attempts >= max_video_attempts or not clips:
        log("Failed to find or process clips.")
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
        up_args = ["upload_youtube.py", "--video", short, "--title", hook, "--description", hook, "--tags", ",".join(tags.get("hashtags", [])), "--privacy", args.privacy]
        if not args.dry_run:
            up_args.append("--confirm")
        
        entry = {"clip": n}
        
        # 2. Try YouTube (If this fails, log it but keep going!)
        try:
            up = run_tool(*up_args)
            if not args.dry_run:
                yt_id = up.get("video_id")
                uploaded_ids.append(yt_id)
                entry["video_id"] = yt_id
        except Exception as e:
            log(f"clip {n} YOUTUBE FAILED:", e)
            entry["youtube_error"] = str(e)
            
        if args.dry_run:
            summary["uploaded"].append({"clip": n, "preview": True})
            continue
            
        # 3. Try Instagram (This will now run even if YouTube fails)
        attempt_instagram_upload(short, hook, n, summary, entry)
        
        summary["uploaded"].append(entry)

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

    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
