"""Weekly job: resolve any experimental Instagram post that's now old enough to
judge, compare it against a baseline of recent normal posts on the same
account, and WhatsApp Moemen if it clearly won.

Run by .github/workflows/style_experiment.yml on a weekly cron. Reads/writes
state/ig_post_log.json (written by run_daily.py's attempt_instagram_upload via
_common.log_ig_post). Never touches the live pipeline -- a win is a
notification, not an automatic style change; Moemen decides whether to roll it
into the default.

Usage:
    python tools/check_style_experiment.py [--min-age-days 4] [--baseline 6] [--win-margin 0.25]

Prints JSON: {"checked":N,"resolved":[...],"pending":[...]}
"""
import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

from _common import REPO_ROOT, emit

TOOLS = Path(__file__).resolve().parent
LOG_PATH = REPO_ROOT / "state" / "ig_post_log.json"


def run_tool(script, *args):
    cmd = [sys.executable, str(TOOLS / script), *map(str, args)]
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    text = (proc.stdout or "").strip()
    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j == -1:
        raise RuntimeError(f"{script}: no JSON output. stderr: {(proc.stderr or '')[-300:]}")
    return json.loads(text[i:j + 1])


def load_log():
    if not LOG_PATH.is_file():
        return {"posts": []}
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"posts": []}


def save_log(data):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def age_days(iso_ts):
    posted = datetime.datetime.fromisoformat(iso_ts)
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - posted).total_seconds() / 86400


def avg(nums):
    nums = [n for n in nums if n is not None]
    return sum(nums) / len(nums) if nums else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-age-days", type=float, default=4.0)
    ap.add_argument("--baseline", type=int, default=6)
    ap.add_argument("--win-margin", type=float, default=0.25)
    args = ap.parse_args()

    data = load_log()
    posts = data.get("posts", [])
    due = [p for p in posts if p.get("experiment") and not p.get("resolved")
           and p.get("posted_at") and age_days(p["posted_at"]) >= args.min_age_days]

    resolved, still_pending = [], []
    for entry in due:
        try:
            exp = run_tool("ig_fetch_analytics.py", "--post-id", entry["post_id"])
        except Exception as e:
            still_pending.append({"post_id": entry["post_id"], "error": str(e)})
            continue
        if exp.get("sync_status") == "pending":
            still_pending.append({"post_id": entry["post_id"], "reason": "sync_pending"})
            continue

        # Baseline: the last N NON-experiment posts logged before this one.
        idx = posts.index(entry)
        baseline_entries = [p for p in posts[:idx] if not p.get("experiment")][-args.baseline:]
        base_views, base_eng = [], []
        for b in baseline_entries:
            try:
                bstats = run_tool("ig_fetch_analytics.py", "--post-id", b["post_id"])
                if bstats.get("sync_status") != "pending":
                    base_views.append(bstats.get("views"))
                    base_eng.append(bstats.get("engagement_rate"))
            except Exception:
                continue

        base_views_avg, base_eng_avg = avg(base_views), avg(base_eng)
        views_delta = (exp.get("views", 0) / base_views_avg - 1) if base_views_avg else None
        eng_delta = (exp.get("engagement_rate", 0) / base_eng_avg - 1) if base_eng_avg else None
        win = bool((views_delta is not None and views_delta >= args.win_margin) or
                    (eng_delta is not None and eng_delta >= args.win_margin))

        entry["resolved"] = True
        entry["result"] = {
            "views": exp.get("views"), "engagement_rate": exp.get("engagement_rate"),
            "baseline_views_avg": base_views_avg, "baseline_engagement_avg": base_eng_avg,
            "views_delta": views_delta, "engagement_delta": eng_delta,
            "baseline_n": len(base_views), "win": win,
        }
        resolved.append(entry)

        if win:
            vpct = f"{views_delta * 100:+.0f}%" if views_delta is not None else "n/a"
            epct = f"{eng_delta * 100:+.0f}%" if eng_delta is not None else "n/a"
            msg = (
                f"MOMO clipping-auto style win: '{entry['style']}' caption style beat your "
                f"last {len(base_views)} normal clips -- views {vpct}, engagement {epct}. "
                f"Post: {entry['post_id']}. Consider making it the default in run_daily.py."
            )
            try:
                run_tool("send_whatsapp.py", "--text", msg)
            except Exception as e:
                entry.setdefault("result", {})["whatsapp_error"] = str(e)

    if resolved:
        save_log(data)

    emit({"checked": len(due), "resolved": [r["post_id"] for r in resolved],
          "pending": still_pending})


if __name__ == "__main__":
    main()
