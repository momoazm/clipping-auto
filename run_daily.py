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
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
TMP = HERE / ".tmp"
CONFIG = HERE / "config" / "channels.json"
HISTORY = HERE / "state" / "clipped_history.json"

# Load this project's own API.env (CI writes it from the API_ENV secret) so this process --
# not just the subprocess tools -- can see IG_ACCESS_TOKEN/IG_USER_ID and decide whether to
# also publish each clip to Instagram. No workflow .yml edit needed: whatever lands in the
# API_ENV secret is already written verbatim to API.env by the existing "Materialize secrets"
# step, so adding the two IG lines there is enough.
try:
    from dotenv import load_dotenv
    load_dotenv(HERE / "API.env")
except ImportError:
    pass
IG_ENABLED = bool(os.environ.get("IG_ACCESS_TOKEN")) and bool(os.environ.get("IG_USER_ID"))


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
    """Regenerate the SFX pack if it's missing (it's gitignored / regenerable)."""
    sfx_dir = HERE / "config" / "sfx"
    if not list(sfx_dir.glob("*.wav")):
        try:
            run_tool("build_sfx.py")
        except Exception as e:
            log("build_sfx failed (clips will render without SFX):", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--privacy", default="public",
                    choices=["public", "unlisted", "private"])
    args = ap.parse_args()

    TMP.mkdir(parents=True, exist_ok=True)
    cfg = load_json(CONFIG, {})
    clips_per_day = int(cfg.get("clips_per_day", 6))
    if args.limit:
        clips_per_day = min(clips_per_day, args.limit)
    target = int(cfg.get("target_secs", 35))
    maxs = int(cfg.get("max_secs", 60))
    max_video_attempts = int(cfg.get("max_video_attempts", 5))

    summary = {"date": datetime.date.today().isoformat(), "dry_run": args.dry_run,
               "uploaded": [], "errors": []}

    # Retry loop: try multiple videos if download/pipeline fails.
    video_attempts = 0
    attempted_videos = []
    last_error = None

    while video_attempts < max_video_attempts:
        video_attempts += 1
        
        # 1. Pick a source video (no quota). A clean "nothing new" is not an error.
        try:
            src = run_tool("find_source_video.py")
        except Exception as e:
            log("no source video to clip today:", e)
            summary["status"] = "no_source"
            summary["detail"] = str(e)
            print(json.dumps(summary, indent=2))
            return

        video_id = src["video_id"]
        if video_id in attempted_videos:
            log(f"video {video_id} already attempted, skipping...")
            continue

        attempted_videos.append(video_id)
        log(f"attempt {video_attempts}: source {video_id} - {src.get('title')} ({src['reason']})")
        summary["source"] = src

        try:
            ensure_sfx()
            src_path = str(TMP / "source.mp4")
            dl = run_tool("download_video.py", "--url", src["url"], "--out", src_path)
            src_path = dl.get("path", src_path)
            src_title = src.get("title") or dl.get("title") or "MrBeast"
            summary["source_title"] = src_title

            probe = run_tool("probe_video.py", "--in", src_path)
            if not probe.get("has_audio"):
                raise RuntimeError("source has no audio track; cannot caption.")

            run_tool("transcribe_video.py", "--in", src_path)
            sel = run_tool("select_clips.py", "--count", clips_per_day,
                           "--target-secs", target, "--max-secs", maxs)
            clips = sel.get("clips", [])
            log(f"selected {len(clips)} clips via {sel.get('provider')}")
            summary["selected"] = len(clips)
            
            # Success! Break out of retry loop.
            break
            
        except Exception as e:
            last_error = str(e)
            log(f"attempt {video_attempts} failed: {e}")
            summary["attempt_errors"] = summary.get("attempt_errors", [])
            summary["attempt_errors"].append({
                "video_id": video_id,
                "error": last_error
            })
            continue

    # If all attempts failed, exit with error.
    if video_attempts >= max_video_attempts or len(clips) == 0:
        log("all video attempts failed or no clips selected")
        summary["status"] = "error"
        summary["detail"] = last_error or "exhausted all video attempts"
        print(json.dumps(summary, indent=2))
        sys.exit(1)

    uploaded_ids = []
    for idx, clip in enumerate(clips, start=1):
        n = f"{idx:02d}"
        start, end = clip["start"], clip["end"]
        emph = ",".join(clip.get("emphasis_words") or [])
        hook = clip.get("suggested_title") or clip.get("hook") or src_title
        reframed = str(TMP / f"reframed_{n}.mp4")
        cues = str(TMP / f"cues_{n}.json")
        caps = str(TMP / f"caps_{n}.ass")
        short = str(TMP / f"short_{n}.mp4")
        try:
            run_tool("reframe_crop.py", "--in", src_path, "--start", start, "--end", end, "--out", reframed)
            run_tool("plan_effects.py", "--start", start, "--end", end, "--emphasis", emph, "--out", cues)
            run_tool("build_captions.py", "--start", start, "--end", end,
                     "--style", "hormozi", "--hook", hook, "--out", caps)
            run_tool("render_clip.py", "--in", reframed, "--captions", caps,
                     "--cues", cues, "--out", short, "--max-secs", maxs)
            tags = run_tool("generate_hashtags.py", "--title", src_title,
                            "--hook", clip.get("hook") or hook, "--snippet", clip.get("hook") or "")
            hashtags = tags.get("hashtags", [])
            desc = (clip.get("hook") or "") + "\n\n" + " ".join("#" + t for t in hashtags)
            up_args = ["upload_youtube.py", "--video", short, "--title", hook,
                       "--description", desc, "--tags", ",".join(hashtags) or "shorts",
                       "--privacy", args.privacy]
            if not args.dry_run:
                up_args.append("--confirm")
            up = run_tool(*up_args)
            if args.dry_run:
                log(f"clip {n}: DRY preview -> channel {up.get('channel_title')}")
                summary["uploaded"].append({"clip": n, "preview": True, "title": hook})
            else:
                uploaded_ids.append(up.get("video_id"))
                log(f"clip {n}: uploaded {up.get('url')}")
                entry = {"clip": n, "video_id": up.get("video_id"),
                         "url": up.get("url"), "title": hook}
                summary["uploaded"].append(entry)
                if IG_ENABLED:
                    # IG can't take a local file -> host the mp4 at a PUBLIC url, then
                    # publish it as a Reel. A failure here must not undo the YouTube
                    # upload that already succeeded, so it's its own try/except.
                    try:
                        host = run_tool("host_public.py", "--video", short)
                        ig = run_tool("upload_instagram.py", "--video-url", host["url"],
                                      "--caption", desc, "--confirm")
                        entry["instagram_media_id"] = ig.get("post_id") or ig.get("media_id")
                        log(f"clip {n}: Instagram -> {entry['instagram_media_id']}")
                    except Exception as e:
                        log(f"clip {n}: Instagram FAILED (YouTube upload still kept): {e}")
                        summary.setdefault("instagram_errors", []).append({"clip": n, "error": str(e)})
        except Exception as e:
            log(f"clip {n} FAILED:", e)
            summary["errors"].append({"clip": n, "error": str(e)})
            continue

    # Record the source as processed so it's never repeated (skip on dry-run).
    if not args.dry_run:
        hist = load_json(HISTORY, {"clipped": []})
        if isinstance(hist, list):
            hist = {"clipped": hist}
        hist.setdefault("clipped", []).append({
            "source_id": src["video_id"], "source_title": src_title,
            "channel": src.get("channel"), "date": summary["date"],
            "clip_video_ids": uploaded_ids,
        })
        with open(HISTORY, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
        log(f"history updated: {src['video_id']} (+{len(uploaded_ids)} clips)")

    summary["status"] = "ok"
    summary["uploaded_count"] = len([u for u in summary["uploaded"] if not u.get("preview")])
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
