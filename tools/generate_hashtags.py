"""Generate YouTube hashtags/tags for a clip via the shared LLM provider chain.

Pipeline role: feeds the upload step. run_daily.py passes the resulting tags to
upload_youtube.py --tags and also appends them (as #tags) to the description, so each
auto-uploaded Short gets relevant, varied discovery hashtags.

Reuses select_clips.py's provider chain (Groq -> Cerebras -> Gemini -> Mistral ->
OpenRouter) so there's one place that knows how to talk to the LLMs. Degrades to a small
base tag set if every provider fails (never blocks an upload).

Usage:
    python tools/generate_hashtags.py --title "<source title>" --hook "<clip hook>" \
        [--snippet "<transcript snippet>"] [--max 30]

Prints JSON: {"hashtags": ["tag1", ...], "provider": "groq"|...|null}
"""
import argparse
import json
import re

from _common import load_env, emit
import select_clips as sc

# Broad, evergreen discovery tags that apply to every MrBeast Short. Kept ahead of the
# LLM's content-specific tags in the merge so a Short is always well-tagged even if the
# LLM chain fails; the LLM tags below add the niche-correct relevance on top.
# Deliberately NO generic filler (#viral/#fyp/#foryou/#trending): 2026 Shorts/Reels
# research + our own reviews show 3-5 hyper-relevant tags beat generic stuffing for
# initial classification, and filler dilutes the topic signal.
BASE = [
    "shorts", "youtubeshorts", "shortsfeed", "shortsvideo", "mrbeast",
    "mrbeastshorts", "beast", "challenge", "money", "funny", "entertainment",
]

# Generic filler never ships, even if the LLM returns it.
BANNED = {"viral", "viralshorts", "trending", "trendingshorts", "fyp", "foryou",
          "foryoupage"}

PROMPT = """Generate 10-14 YouTube HASHTAGS for a short vertical clip.
Rules: lowercase; letters/numbers only (no '#', no spaces, no punctuation); each a single
word or compound word; no duplicates. Lead with the MOST SPECIFIC tags first (names of
people, the challenge type, prizes, locations, emotions, reactions). NEVER use generic
filler tags (viral, fyp, foryou, trending, funny-videos-style generics): a few exact tags
beat a pile of broad ones for Shorts classification.

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
    ap.add_argument("--max", type=int, default=30)
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
        if t and t not in seen and t not in BANNED:
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
