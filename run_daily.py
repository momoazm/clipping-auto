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

def ensure_sfx():
    sfx_dir = HERE / "config" / "sfx"
    if not list(sfx_dir.glob("*.wav")):
        try:
            run_tool("build_sfx.py")
        except Exception as e:
            log("build_sfx failed:", e)

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
    video_attempts, attempted_videos, last_error = 0, [], None

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
            last_error = str(e)
            continue

    if video_attempts >= max_video_attempts or not clips:
        sys.exit(1)

    uploaded_ids = []
    for idx, clip in enumerate(clips, start=1):
        n = f"{idx:02d}"
        hook = clip.get("suggested_title") or clip.get("hook") or "Clip"
        short = str(TMP / f"short_{n}.mp4")
        try:
            # Rendering steps...
            run_tool("reframe_crop.py", "--in", src_path, "--start", clip["start"], "--end", clip["end"], "--out", str(TMP/f"reframed_{n}.mp4"))
            run_tool("plan_effects.py", "--start", clip["start"], "--end", clip["end"], "--emphasis", ",".join(clip.get("emphasis_words", [])), "--out", str(TMP/f"cues_{n}.json"))
            run_tool("build_captions.py", "--start", clip["start"], "--end", clip["end"], "--style", "hormozi", "--hook", hook, "--out", str(TMP/f"caps_{n}.ass"))
            run_tool("render_clip.py", "--in", str(TMP/f"reframed_{n}.mp4"), "--captions", str(TMP/f"caps_{n}.ass"), "--cues", str(TMP/f"cues_{n}.json"), "--out", short, "--max-secs", maxs)
            
            # YouTube Upload
            tags = run_tool("generate_hashtags.py", "--title", src_title, "--hook", hook, "--snippet", hook)
            up = run_tool("upload_youtube.py", "--video", short, "--title", hook, "--description", hook, "--tags", ",".join(tags.get("hashtags", [])), "--privacy", args.privacy, "--confirm")
            uploaded_ids.append(up.get("video_id"))
log(f"DEBUG: IG_ENABLED is {IG_ENABLED} (API present: {bool(os.environ.get('ZERNIO_API'))}, IG ID present: {bool(os.environ.get('ZERNIO_INSTAGRAM_ID'))})")
            # Direct Instagram Upload (SDK Implementation)
            if IG_ENABLED:
                try:
                    ig = run_tool("upload_instagram.py", "--video", short, "--caption", hook, "--confirm")
                    entry = {"clip": n, "video_id": up.get("video_id"), "instagram_media_id": ig.get("media_id")}
                    summary["uploaded"].append(entry)
                    log(f"clip {n}: Instagram -> {ig.get('media_id')}")
                except Exception as e:
                    log(f"clip {n}: Instagram FAILED: {e}")
        except Exception as e:
            log(f"clip {n} FAILED:", e)
            continue

    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
