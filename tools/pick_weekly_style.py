"""Deterministically pick this ISO week's experimental caption style, and gate
it to exactly ONE clip so an experiment never overrides the whole week's output.

Rotation is keyed by ISO week number, so calling it more than once in the same
week always returns the same style (idempotent across retried GH Actions runs).
`--consume` marks it used: the FIRST caller in a given week gets the
experimental style; every later call that week gets style=null (fall back to
the normal default, "hormozi").

Called by run_daily.py (clip 01 of whichever daily run happens first that
week) and by check_style_experiment.py (to queue next week's style after
resolving this week's result).

Usage:
    python tools/pick_weekly_style.py             # peek this week's queued style
    python tools/pick_weekly_style.py --consume    # claim it for the caller's post

Prints JSON: {"week":"2026-W29","style":"brand","used":false}
             --consume: {"week":...,"style":"brand","consumed":true}
                     or {"week":...,"style":null,"consumed":false} (already used this week)
"""
import argparse
import datetime
import json

from _common import REPO_ROOT, emit

STATE_PATH = REPO_ROOT / "state" / "style_experiment.json"
# config/caption_styles.json presets; "hormozi" is the everyday default.
STYLES = ["hormozi", "brand", "clean"]


def _load():
    if STATE_PATH.is_file():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"history": []}


def _save(data):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--consume", action="store_true",
                        help="Claim this week's experimental style for the caller's post")
    args = parser.parse_args()

    iso = datetime.date.today().isocalendar()
    week_key = f"{iso[0]}-W{iso[1]:02d}"

    data = _load()
    cur = data.get("current")
    if not cur or cur.get("week") != week_key:
        cur = {"week": week_key, "style": STYLES[iso[1] % len(STYLES)], "used": False}
        data["current"] = cur
        _save(data)

    if not args.consume:
        emit({"week": week_key, "style": cur["style"], "used": cur["used"]})
        return

    if cur["used"]:
        emit({"week": week_key, "style": None, "consumed": False})
        return

    cur["used"] = True
    data.setdefault("history", []).append({"week": week_key, "style": cur["style"]})
    _save(data)
    emit({"week": week_key, "style": cur["style"], "consumed": True})


if __name__ == "__main__":
    main()
