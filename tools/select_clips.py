"""Pick the most clip-worthy moments from a transcript using an LLM.

Pipeline role: turns the timestamped transcript from transcribe_video.py into a
short list of candidate Shorts. The LLM scores moments the way top clipping
accounts choose them: a strong hook in the first ~3 seconds, a self-contained
idea, emotional/surprising/controversial/actionable payload, and a length that
fits a Short.

The model only proposes approximate spans; we then SNAP each span to real word
boundaries from the transcript and clamp it to < 60 s so the cut lands cleanly.

LLM providers are tried best-first, exactly like extract_article.py. Only providers
with a configured key are called; each call has a bounded timeout, and a malformed or
too-short response falls through to the next configured provider. A whole-chain failure
surfaces instead of hanging the daily workflow until its job timeout.

Usage:
    python tools/select_clips.py [--transcript .tmp/transcript.json] [--count 3] \
        [--target-secs 35] [--max-secs 60] [--out .tmp/clips.json]

Prints JSON: {"provider","count","clips":[{...}]}  and writes the same clips to --out.
"""
import argparse
import json
import os

from _common import load_env, emit, fail, tmp_path

SYSTEM = (
    "You are a world-class viral short-form editor who turns long videos into YouTube "
    "Shorts / TikToks. You think in terms of retention: the first 2 seconds decide whether "
    "the viewer swipes away, and completion rate is the #1 algorithm signal, so every clip "
    "must open on the single most scroll-stopping instant and never sag."
)

PROMPT_TMPL = """From the transcript below, choose the {count} BEST standalone clips for vertical Shorts.

The hook is the most important factor for virality. For each clip:
- It MUST open on the most scroll-stopping line in the FIRST ~1.5 seconds — no setup, no slow
  intro, no "so basically". Start ON the payoff/tension, not the run-up to it.
- The strongest hooks here are: a curiosity gap (a question the viewer needs answered), high
  stakes or a big number ("$1,000,000", "last person to leave"), a shocking/surprising turn,
  visible conflict or competition, or a raw emotional peak. Pick the moment with the most of these.
- It must be a self-contained thought that makes sense with NO prior context.
- For comedy/MrBeast moments, include only enough setup to understand the joke (usually 0.75-1.5
  seconds), then keep the full reaction/punchline. Do not cut away at the first laugh or reveal.
- Cut cleanly: don't start mid-word or end before the payoff lands. End on a punchline,
  resolution, or a cliffhanger that rewards finishing (which lifts completion + loops).
- Target length about {target} seconds; HARD MAX {maxs} seconds. Shorter, tighter clips finish
  more often — prefer the tightest cut that still delivers the full moment; never exceed the max.
- Clips must not overlap each other.

For each clip also write:
- "hook": the verbatim opening line(s) the clip starts on.
- "suggested_title": a curiosity-driven title (<=80 chars) that makes the click feel mandatory —
  tease the payoff, don't spoil it.
- "emphasis_words": the 2-4 highest-impact words/numbers in the clip (drive caption pop + zoom).

The transcript is timestamped lines as [start-end] text (seconds):
{body}

Return ONLY a JSON object, no prose, no markdown fences, in exactly this shape:
{{"clips": [
  {{"start": <sec>, "end": <sec>, "hook": "<the opening line, verbatim-ish>",
    "reason": "<one short sentence why it'll perform>", "virality_score": <0-100 int>,
    "suggested_title": "<punchy <=80 char title>", "emphasis_words": ["word1","word2"]}}
]}}
Order the clips best-first by virality_score."""


def build_body(transcript, char_budget=24000):
    """Compact timestamped lines; prefer segments, fall back to grouping words."""
    segments = transcript.get("segments") or []
    if not segments and transcript.get("words"):
        # Group words into ~8s pseudo-segments so the model still gets timestamps.
        words, cur, seg_start = transcript["words"], [], None
        for w in words:
            if seg_start is None:
                seg_start = w["start"]
            cur.append(w["w"])
            if w["end"] - seg_start >= 8:
                segments.append({"start": seg_start, "end": w["end"], "text": " ".join(cur)})
                cur, seg_start = [], None
        if cur:
            segments.append({"start": seg_start, "end": words[-1]["end"], "text": " ".join(cur)})

    all_lines = [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments]
    full = "\n".join(all_lines)
    if len(full) <= char_budget or len(all_lines) <= 1:
        return full
    # Too long: keep evenly-spaced segments so the model still sees the WHOLE
    # video (not just the first chunk) and can pick clips from anywhere.
    avg = len(full) / len(all_lines)
    keep = max(1, int(char_budget / avg))
    stride = max(1, len(all_lines) // keep)
    sampled = all_lines[::stride]
    return "\n".join(sampled)


def _strip_fences(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: t.rfind("```")]
    # Grab the outermost JSON object if the model added stray prose.
    i, j = t.find("{"), t.rfind("}")
    return t[i : j + 1] if i != -1 and j != -1 else t


def _load_exclude_windows(path):
    """Load source-time spans that have already been published for this video."""
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if isinstance(raw, dict):
        raw = raw.get("windows", [])
    windows = []
    for item in raw if isinstance(raw, list) else []:
        try:
            start = float(item.get("start")) if isinstance(item, dict) else float(item[0])
            end = float(item.get("end")) if isinstance(item, dict) else float(item[1])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if end > start:
            windows.append({"start": start, "end": end})
    return windows


def _overlaps_excluded(start, end, windows, padding=1.0):
    """Treat a one-second safety buffer around a published span as already used."""
    return any(start < w["end"] + padding and end > w["start"] - padding for w in windows)


# --- provider chain (OpenAI-compatible + Gemini + Groq SDK) ----------------

def _provider_timeout():
    """Return a bounded per-provider timeout so one dead API cannot stall the chain."""
    try:
        return max(15.0, min(90.0, float(os.environ.get("CLIP_PROVIDER_TIMEOUT_SEC", "45"))))
    except (TypeError, ValueError):
        return 45.0


def _provider_configured(name):
    """Avoid spending timeout budget on providers whose secrets are not present in CI."""
    if name.startswith("openrouter-"):
        return bool(os.environ.get("OPENROUTER_API_KEY"))
    if name == "zhipu":
        return bool(os.environ.get("ZHIPU_API_KEY"))
    if name == "groq":
        return bool(os.environ.get("GROQ_API_KEY"))
    if name == "cerebras":
        return bool(os.environ.get("CEREBRAS_API_KEY"))
    if name == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    if name == "mistral":
        return bool(os.environ.get("MISTRAL_API_KEY"))
    return True

def _chat_groq(prompt):
    from groq import Groq
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY not set")
    client = Groq(api_key=key, timeout=_provider_timeout(), max_retries=0)
    r = client.chat.completions.create(
        model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        temperature=0.5,
        response_format={"type": "json_object"},
    )
    return r.choices[0].message.content


def _chat_openai_compatible(prompt, base_url, key_env, default_model, model_env, extra_headers=None):
    import httpx
    key = os.environ.get(key_env)
    if not key:
        raise RuntimeError(f"{key_env} not set")
    headers = {"Authorization": f"Bearer {key}"}
    if extra_headers:
        headers.update(extra_headers)
    resp = httpx.post(
        f"{base_url}/chat/completions",
        headers=headers,
        json={
            "model": os.environ.get(model_env, default_model),
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "temperature": 0.5,
        },
        timeout=_provider_timeout(),
    )
    resp.raise_for_status()
    out = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    if not out:
        raise RuntimeError(f"{key_env} returned empty content")
    return out


def _chat_gemini(prompt):
    import httpx
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    model = os.environ.get("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
    resp = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": key},
        json={"contents": [{"parts": [{"text": SYSTEM + "\n\n" + prompt}]}]},
        timeout=_provider_timeout(),
    )
    resp.raise_for_status()
    parts = resp.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
    out = "".join(p.get("text", "") for p in parts).strip()
    if not out:
        raise RuntimeError("Gemini returned no text")
    return out


def _chat_cerebras(prompt):
    return _chat_openai_compatible(prompt, "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "gpt-oss-120b", "CEREBRAS_MODEL")


def _chat_mistral(prompt):
    return _chat_openai_compatible(prompt, "https://api.mistral.ai/v1", "MISTRAL_API_KEY", "mistral-small-latest", "MISTRAL_MODEL")


def _chat_openrouter_nemotron(prompt):
    return _chat_openai_compatible(prompt, "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "nvidia/nemotron-3-ultra-550b-a55b:free", "OPENROUTER_MODEL")


def _chat_openrouter_qwen_coder(prompt):
    return _chat_openai_compatible(prompt, "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "qwen/qwen3-coder:free", "OPENROUTER_MODEL_2")


def _chat_zhipu(prompt):
    base = os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    return _chat_openai_compatible(prompt, base, "ZHIPU_API_KEY", "glm-4.7-flash", "ZHIPU_MODEL")


# Accuracy-first order (Artificial Analysis index, 2026-07-10): Nemotron 3 Ultra (38,
# best model with a live :free route — Kimi/DeepSeek :free are gone) -> Qwen3 Coder ->
# GLM-4.7-Flash (Zhipu, permanently free; skipped until ZHIPU_API_KEY exists) ->
# gpt-oss-120b on Groq/Cerebras (fast, big quotas) -> tail.
# OpenRouter :free = 50 req/day (1000/day after a one-time $10 top-up); when the cap
# trips the first two links fail fast and Groq takes over.
CHAIN = (
    ("openrouter-nemotron", _chat_openrouter_nemotron),
    ("openrouter-qwen-coder", _chat_openrouter_qwen_coder),
    ("zhipu", _chat_zhipu),
    ("groq", _chat_groq),
    ("cerebras", _chat_cerebras),
    ("gemini", _chat_gemini),
    ("mistral", _chat_mistral),
)


def snap_to_words(words, start, end, target_secs, max_secs, min_secs=20):
    """Snap a span to clean word/sentence boundaries and clamp to the Short limit.

    LLMs often return only the timestamp of a hook even when the transcript shows a longer
    payoff. The old expansion picked an arbitrary word near ``target_secs``, which could cut a
    MrBeast reveal mid-sentence. When expansion is needed, prefer the nearest spoken sentence
    boundary; otherwise use the nearest word as a safe fallback.
    """
    if not words:
        return start, end
    start_w = min(words, key=lambda w: abs(w["start"] - start))
    # A tiny lead-in makes a selected joke/reveal understandable without bringing back the old
    # slow setup. The LLM still owns the moment; this only prevents a hard cut on the first word.
    s = max(0.0, start_w["start"] - 0.85)

    desired = end - s
    if desired < min_secs:        # model gave a degenerate/near-zero span
        desired = target_secs
    desired = min(desired, max_secs)
    target_end = s + desired

    end_candidates = [w for w in words if w["end"] > s]
    sentence_candidates = [
        w for w in end_candidates
        if w["end"] >= s + min_secs
        and w["end"] <= s + max_secs
        and str(w.get("w", "")).rstrip().endswith((".", "!", "?", "…"))
    ]
    # Only use a sentence boundary for the synthetic expansion. If the model supplied a
    # meaningful longer end, preserving that chosen payoff is more important than re-cutting it.
    if (end - s) < min_secs and sentence_candidates:
        end_w = min(sentence_candidates, key=lambda w: abs(w["end"] - target_end))
    else:
        end_w = min(end_candidates or words, key=lambda w: abs(w["end"] - target_end))
    e = end_w["end"]

    if e - s > max_secs:          # hard cap: trim to last word that fits
        fit = [w for w in words if w["start"] >= s and w["end"] <= s + max_secs]
        e = fit[-1]["end"] if fit else s + max_secs
    if e - s < min_secs:          # never emit a sub-second "clip"
        # Preserve a chosen sentence boundary that already lands after the target; the old
        # `min(...)` could move it backwards and cut the final punctuation off again.
        e = max(e, min(s + target_secs, words[-1]["end"]))
    return round(s, 3), round(e, 3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcript", default=None)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--target-secs", type=int, default=35)
    parser.add_argument("--max-secs", type=int, default=60)
    parser.add_argument("--exclude-windows", default=None,
                        help="JSON file containing previously published source spans")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    load_env()
    tpath = args.transcript or tmp_path("transcript.json")
    out_path = args.out or tmp_path("clips.json")

    if not os.path.isfile(tpath):
        fail(f"Transcript not found: {tpath}. Run transcribe_video.py first.")
        return
    with open(tpath, "r", encoding="utf-8") as f:
        transcript = json.load(f)

    words = transcript.get("words") or []
    excluded_windows = _load_exclude_windows(args.exclude_windows)
    body = build_body(transcript)
    if not body.strip():
        fail("Transcript has no usable text/timestamps.", transcript=tpath)
        return

    # Ask for a small buffer of candidates. The best six can overlap in a long transcript;
    # collecting extra ranked options lets the non-overlap pass fill the requested six slots
    # without accepting weaker moments or cutting a winner in half.
    # Ask for extra candidates when old spans must be removed after the provider responds.
    candidate_request_count = args.count + max(3, args.count // 2) + len(excluded_windows)
    prompt = PROMPT_TMPL.format(
        count=candidate_request_count, target=args.target_secs, maxs=args.max_secs, body=body)
    if excluded_windows:
        spans = ", ".join(f"{w['start']:.1f}-{w['end']:.1f}s" for w in excluded_windows)
        prompt += (
            "\n\nPreviously published source spans (seconds): " + spans +
            "\nDo not select or overlap any of these spans; choose genuinely different moments "
            "elsewhere in the source."
        )

    configured_chain = tuple((name, fn) for name, fn in CHAIN if _provider_configured(name))
    if not configured_chain:
        fail("No LLM provider is configured for clip selection.",
             expected_keys=["GROQ_API_KEY", "GEMINI_API_KEY"])
        return

    # MrBeast runs require multiple clips. If a provider returns malformed JSON or fewer than
    # two candidates, try the next configured provider rather than letting run_daily spend the
    # remaining source-attempt budget on an unusable response.
    minimum_candidates = min(args.count, 2)
    candidates, provider, errors = None, None, {}
    for name, fn in configured_chain:
        try:
            raw = fn(prompt)
            data = json.loads(_strip_fences(raw))
            parsed = data["clips"] if isinstance(data, dict) else data
            if not isinstance(parsed, list) or len(parsed) < minimum_candidates:
                raise RuntimeError(
                    f"provider returned {len(parsed) if isinstance(parsed, list) else 0} usable "
                    f"candidates; need at least {minimum_candidates}"
                )
            candidates, provider = parsed, name
            break
        except Exception as e:
            errors[name] = str(e)
            continue
    if candidates is None:
        fail("All configured LLM providers failed for clip selection.", provider_errors=errors)
        return

    clips = []
    for c in candidates:
        try:
            s, e = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError):
            continue
        s, e = snap_to_words(words, s, e, args.target_secs, args.max_secs)
        if _overlaps_excluded(s, e, excluded_windows):
            continue
        clips.append({
            "start": s,
            "end": e,
            "duration": round(e - s, 3),
            "hook": c.get("hook", ""),
            "reason": c.get("reason", ""),
            "virality_score": c.get("virality_score"),
            "suggested_title": c.get("suggested_title", ""),
            "emphasis_words": c.get("emphasis_words", []),
        })

    # Drop overlaps (keep higher score / earlier), sort best-first.
    clips.sort(key=lambda x: (-(x.get("virality_score") or 0), x["start"]))
    kept = []
    for c in clips:
        if all(c["end"] <= k["start"] or c["start"] >= k["end"] for k in kept):
            kept.append(c)
    kept = kept[: args.count]

    payload = {"provider": provider, "count": len(kept),
               "target_count": args.count, "candidates_requested": candidate_request_count,
               "clips": kept}
    if provider != "groq":
        payload["note"] = f"Primary failed; used {provider}."
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    emit(payload)


if __name__ == "__main__":
    main()
