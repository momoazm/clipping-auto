# PLAN — clipping-auto: clean titles + competitor caption format (2026-10-21)

Status: shipped (pending the next clipping_daily run's log check)
Sibling plan: `../ranking shorts/PLAN-sharktank-batch.md`
Vault summary: `PLAN-sharktank-batch-and-formats.md`

## Goal

1. **Never publish a transcript fragment as a title.** The last real uploads shipped
   `"the two. And even though the workouts were still grueling, they were startin"` and
   `"watching him be so consistent and work so I hard, have like a whole new vers"` — raw
   16-word ASR fragments, cut mid-word by a blind `[:76]`.
2. **Adopt the winning competitor caption format** (top MrBeast clip Shorts): centered
   multi-color word captions, a big persistent hook line, full-frame crop, no title card.
   Keep the existing `hormozi` / `brand` / `clean` presets available.

## Root cause (code evidence)

- `select_clips.deterministic_fallback` sets `hook = " ".join(text.split()[:16])` and
  `suggested_title = _safe_title(hook[:76], hook, text)`.
- `_safe_title` returns any title that clears the unsafe screen unchanged, so the fragment
  becomes the final title.
- No title QA anywhere between the selector and `run_daily`'s upload calls.
- `_safe_title` also blind-sliced at 80 characters (`title[:80]`), the same mid-word cut.

## Work checklist

### A. Title quality (select_clips.py + run_daily.py)

- [x] A1. `title_from_transcript(text, max_len=70)`: pick a complete sentence/clause, strip
      leading fillers ("and", "but", "so", "like", "you know"), capitalise, end with
      punctuation, never cut mid-word.
- [x] A2. `title_quality_problem(title)`: reject fragments (lowercase openers, leading
      conjunction, multiple sentence stops, > 12 words with no terminal punctuation, trailing
      partial word) — returns a reason string or None.
- [x] A3. Apply QA in `_safe_title` (LLM titles too) and in `deterministic_fallback`.
- [x] A4. `run_daily.py`: validate the final `hook` once more before render; rewrite with
      `title_from_transcript` using the clip transcript, then a neutral safe fallback.
- [x] A5. Tests for both functions and for the end-to-end fallback title.

### B. Competitor caption format (build_captions.py + config)

- [x] B1. New `competitor` preset in `config/caption_styles.json`: Anton, uppercase, 3 words
      per screen, white + brand-gold active word, 8px black stroke, centered lower third.
      Keep `hormozi`, `brand`, `clean`.
- [x] B2. `hook_mode: "persistent"` on the preset: the hook banner stays up for the whole
      clip (competitor evidence) instead of the current 1.6s flash.
- [x] B3. Multi-color hook banner: money/numbers gold, emphasis/power words teal-red accent,
      the final word gold; wrapped to ≤3 lines, ALL CAPS, thick stroke.
- [x] B4. `run_daily.py` default style comes from `config` (`default_style`) / env
      `CLIP_CAPTION_STYLE` = `competitor`; weekly experiment still overrides clip 1.
- [x] B5. Tests: persistent hook length, ≤3 lines, color tags present, existing presets
      unchanged, `default_style` resolves.

### C. Verification

- [x] `python -m pytest tests -q` (existing 27 tests + new ones).
- [x] Render one real clip locally with the new default and inspect a frame (hook + captions
      don't collide, handle no title card).

## Ship

- [x] Commit (own files only) → push `main` → confirm the next clipping-auto run uses the new
      default (artifact/log `style` field) and titles are complete sentences.
      Dry-run dispatch 39559867842 on `main` rendered four clips, each logging
      `caption style -> competitor` (now recorded per clip in `run_summary.json.caption_styles`),
      with complete-sentence titles — "Helicopter Jump Into Ocean at $16M Island", "Frozen Lake
      & Hovercraft: $5M Winter Island", "135-Ft Tallest Water Slide in North America",
      "Hundreds of Monkeys Swarm This $1M Island" — and `media_contract_ok` on all four.
      Commits: `9c0cdc5` (format), `042c525` (style logging), `2125953` (source pool),
      `3485901` (banner fit).

### D. Found while verifying (added, not in the original plan)

- [x] D1. **The channel had been silently posting nothing since 2026-10-16.** Every scheduled
      run 10-16/10-18/10-20 exited `status=no_source`, which the workflow treats as green, so
      nothing alerted. Cause: `config/channels.json` `scan_depth` was 60 and every channel's
      first 60-65 uploads were already reserved — the first untouched video sat at index 61-65,
      just past the cutoff. Raised to 200 (MrBeast alone now shows ~135 free candidates) and the
      "all exhausted" payload now carries `scan: {depth, listed, free}` so a too-shallow scan
      can never again read like a real drought. Tests: `TestSourcePoolDepth`.
- [x] D2. **Hook banner ellipsised real hooks** ("...OCEAN AT $16M..." in the 39559867842
      artifact). `fit_hook_layout()` now shrinks the banner size (138 → 111 on the real hook)
      until every token fits in ≤3 lines, mirroring ranking-shorts' `_hook_layout`. Tests:
      `test_long_hook_shrinks_instead_of_truncating`, `test_fitted_hook_reaches_the_ass_style_and_event`.

### E. Live delivery (manual runs with real uploads, 2026-10-21)

Goal: prove the competitor format actually reaches the accounts, not just the renderer.

- [x] E1. First live run (39571031055) built two valid clips (`media_contract.valid: true`) and
      delivered NOTHING: Instagram "timed out after 300s" then `Video Transcoding Error ...
      MediaInfoExtractionException`, YouTube "provider returned no YouTube error detail".
- [x] E2. Hosting was the Instagram cause: `host_public.py` accepted any URL a host returned,
      and tmpfiles.org answered with `text/html`. Now every candidate URL is verified for real
      MP4 bytes (`ftyp` / `video/*`) and uguu is tried first (the only host that returned the
      exact bytes in a same-machine probe: pixeldrain 401, catbox 0 bytes, litterbox 500).
      Tests: `tests/test_delivery_platforms.py::TestPublicHostChain`.
- [x] E3. YouTube was not a code bug: `GET /v1/posts/{id}` shows
      `403 ... reason: authenticatedUserAccountSuspended`, and
      `https://www.youtube.com/@itsmomoclips` returns 404 -- the channel is suspended.
      `upload_youtube.py` now surfaces the provider's own message (it used to print
      "no YouTube error detail"), `run_daily.py` skips YouTube for the rest of a run once it
      sees the suspension, and `REQUIRED_PLATFORMS` is `instagram` so a dead platform cannot
      fail a good Instagram post. `verify_accounts.py` probes the channel page and warns.
- [x] E4. Live runs after the fixes: 39576795308, 39579711846, 39582167269 -- all green,
      `status: uploaded`, 2 reels each, YouTube skipped from clip 2 onward. Reels verified
      public via the Zernio API: DewF1lQkvr2, DewGw-dj-6N, DewKto_ihA2, DewLml2FGNV
      (plus the two from the first green run).
- BLOCKED (needs Moemen): appeal the YouTube suspension or connect a different channel in
      Zernio, then set `REQUIRED_PLATFORMS: youtube,instagram` back in
      `.github/workflows/clipping_daily.yml`. Nothing else is required -- the upload path is
      already proven and will start working again.
- [x] E5. Budget/visibility follow-ups found while testing: `--reserve-source` used to reserve
      (permanently) BEFORE the delivery-budget check, so a run on a full 24h window burned a
      source and posted nothing -- it is now a clean `quota_guarded` no-op with no reservation
      (verified locally against the real 6/6 state). A `no_source` run still exits green on
      purpose but now raises a `::warning::` annotation, so a drought cannot look healthy again.
      Tests: `TestReservationBudgetGuard`, `TestNoSourceVisibility` (74 total, pyflakes clean).
