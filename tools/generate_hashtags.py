"""Generate YouTube hashtags/tags for a clip via the shared LLM provider chain.

Pipeline role: feeds the upload step. run_daily.py passes the resulting tags to
upload_youtube.py --tags and also appends them (as #tags) to the description, so each
auto-uploaded Short gets relevant, varied discovery hashtags.

Reuses select_clips.py's provider chain (Groq -> Cerebras -> Gemini -> Mistral ->
OpenRouter) so there's one place that knows how to talk to the LLMs. Degrades to a small
base tag set if every provider fails (never blocks an upload).

Usage:
    python tools/generate_hashtags.py --title "<source title>" --hook "<clip hook>" \
        [--snippet "<transcript snippet>"] [--max 18]

Prints JSON: {"hashtags": ["tag1", ...], "provider": "groq"|...|null}
"""
import argparse
import json
import re

from _common import load_env, emit
import select_clips as sc

BASE = ["shorts", "mrbeast", "viral", "fyp"]

PROMPT = """Generate 12-18 YouTube HASHTAGS for a short vertical clip.
Rules: lowercase; letters/numbers only (no '#', no spaces, no punctuation); each a single
word or compound word; no duplicates. Mix BROAD discovery tags (shorts, viral, fyp, trending)
with SPECIFIC tags about the actual content/people/topic below.

Source video title: {title}
Clip hook line: {hook}
Transcript snippet: {snippet}

Return ONLY JSON, no prose: {{"hashtags": ["tag1","tag2", ...]}}"""


def clean_tag(t):
    return re.sub(r"[^A-Za-z0-9]", "", str(t)).lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="")
    ap.add_argument("--hook", default="")
    ap.add_argument("--snippet", default="")
    ap.add_argument("--max", type=int, default=18)
    args = ap.parse_args()

    load_env()
    prompt = PROMPT.format(title=args.title[:200], hook=args.hook[:200],
                           snippet=args.snippet[:500])

    raw, provider, errors = None, None, {}
    for name, fn in sc.CHAIN:
        try:
            raw = fn(prompt)
            provider = name
            break
        except Exception as e:
            errors[name] = str(e)
            continue

    llm_tags = []
    if raw is not None:
        try:
            data = json.loads(sc._strip_fences(raw))
            llm_tags = [clean_tag(t) for t in (data.get("hashtags") or [])]
        except Exception as e:
            errors["parse"] = str(e)

    seen, merged = set(), []
    for t in BASE + llm_tags:
        if t and t not in seen:
            seen.add(t)
            merged.append(t)
    merged = merged[: args.max]

    payload = {"hashtags": merged, "provider": provider}
    if not llm_tags:
        payload["note"] = "LLM hashtags unavailable; used base tags."
        payload["provider_errors"] = errors
    emit(payload)


if __name__ == "__main__":
    main()
