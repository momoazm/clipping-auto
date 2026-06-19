"""Decide where sound effects and zoom punch-ins land for one clip.

Pipeline role: the bridge between the transcript/clip metadata and render_clip's
new sound-design + punch-in layers. It reuses build_captions' screen-grouping so
the cues line up with the SAME on-screen caption beats the viewer sees:

  - a `riser` into the hook (t=0),
  - a `whoosh` on natural caption-screen changes (a pause before a new line),
  - a `pop` on emphasis words (from select_clips' `emphasis_words`),
  - `punch_ins` (brief zoom) on those same emphasis moments.

Cue times are CLIP-RELATIVE seconds (clip starts at 0), matching the reframed clip
and the .ass captions. Everything is capped + min-spaced so the effects punctuate
rather than overwhelm.

Usage:
    python tools/plan_effects.py --start 12.5 --end 58.0 \
        [--emphasis "recreated,Squid Game,real life"] [--style hormozi] \
        [--transcript .tmp/transcript.json] [--out .tmp/cues_01.json]

Prints JSON: {"sfx":[{"name","t"}], "punch_ins":[t,...], "duration", "screens"}
"""
import argparse
import json
import os
import re

from _common import load_env, emit, fail, tmp_path
import build_captions as bc


def norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcript", default=None)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--emphasis", default="", help="comma-separated emphasis words/phrases")
    parser.add_argument("--style", default="hormozi")
    parser.add_argument("--out", default=None)
    parser.add_argument("--max-whooshes", type=int, default=6)
    parser.add_argument("--max-pops", type=int, default=6)
    parser.add_argument("--max-punch-ins", type=int, default=4)
    parser.add_argument("--whoosh-lead", type=float, default=0.2,
                        help="start the whoosh this many secs before the cut so its swell peaks on it")
    args = parser.parse_args()

    load_env()
    tpath = args.transcript or tmp_path("transcript.json")
    out_path = args.out or tmp_path("cues.json")
    dur = args.end - args.start

    if not os.path.isfile(tpath):
        fail(f"Transcript not found: {tpath}. Run transcribe_video.py first.")
        return
    with open(tpath, "r", encoding="utf-8") as f:
        transcript = json.load(f)

    all_words = transcript.get("words") or []
    words = [
        {"w": w["w"], "start": w["start"] - args.start, "end": w["end"] - args.start}
        for w in all_words
        if w["end"] > args.start and w["start"] < args.end and w["w"]
    ]
    if not words:
        fail("No words fall inside this clip range; nothing to plan.",
             start=args.start, end=args.end)
        return

    style_name, style = bc.load_style(args.style)
    per_screen = style.get("words_per_screen", 3)
    screens = bc.group_screens(words, per_screen)

    sfx = [{"name": "riser", "t": 0.0}]

    # Whoosh on caption-screen changes that follow a real pause, min-spaced so they
    # land on natural transitions instead of every line.
    last_whoosh = -99.0
    whooshes = []
    for idx in range(1, len(screens)):
        s_start = screens[idx][0]["start"]
        prev_end = screens[idx - 1][-1]["end"]
        gap = s_start - prev_end
        if gap >= 0.3 and (s_start - last_whoosh) >= 2.2:
            whooshes.append(round(max(0.0, s_start - args.whoosh_lead), 3))
            last_whoosh = s_start
    for t in whooshes[: args.max_whooshes]:
        sfx.append({"name": "whoosh", "t": t})

    # Emphasis -> pops + punch-ins. Split phrases into words so "Squid Game"
    # matches either token; ignore tiny words.
    emph = set()
    for phrase in args.emphasis.split(","):
        for tok in phrase.split():
            n = norm(tok)
            if len(n) >= 3:
                emph.add(n)

    pop_times, last_pop = [], -99.0
    if emph:
        for w in words:
            if norm(w["w"]) in emph and (w["start"] - last_pop) >= 0.8:
                pop_times.append(round(w["start"], 3))
                last_pop = w["start"]

    for t in pop_times[: args.max_pops]:
        sfx.append({"name": "pop", "t": t})

    punch_ins = pop_times[: args.max_punch_ins]

    sfx.sort(key=lambda c: c["t"])
    payload = {
        "sfx": sfx,
        "punch_ins": punch_ins,
        "duration": round(dur, 3),
        "screens": len(screens),
        "style": style_name,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    emit(payload)


if __name__ == "__main__":
    main()
