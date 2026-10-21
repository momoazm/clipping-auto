"""Guards for the 2026-10-21 competitor-format batch (titles + caption preset).

Why these exist:
  * the 2026-10-20 uploads shipped raw ASR fragments as the post title
    ("the two. And even though the workouts were still grueling, they were startin"),
    because `deterministic_fallback` did `hook = text.split()[:16]` and then a blind
    `hook[:76]` character slice -- and nothing between the selector and the uploader
    ever checked that a title was a sentence;
  * the channel still used the old 1.6-second title card, while the winning competitor
    Shorts carry a big ALL-CAPS hook banner pinned for the whole clip.

Everything here is offline.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import build_captions  # noqa: E402
import select_clips  # noqa: E402

# The two titles that were actually published on 2026-10-20.
SHIPPED_FRAGMENTS = [
    "the two. And even though the workouts were still grueling, they were startin",
    "watching him be so consistent and work so I hard, have like a whole new vers",
]

SPOKEN = (
    "so anyway we started the challenge and everybody thought it was easy but then "
    "he lost one hundred thousand dollars in the last round today"
)


def word_timings(text, step=0.4):
    return [{"w": w, "start": i * step, "end": i * step + step * 0.9}
            for i, w in enumerate(text.split())]


class TitleQualityTest(unittest.TestCase):
    """A title must read as a sentence; a raw ASR fragment must never ship."""

    def test_the_two_published_fragments_are_rejected(self):
        for fragment in SHIPPED_FRAGMENTS:
            problem = select_clips.title_quality_problem(fragment)
            self.assertTrue(problem, f"should reject: {fragment}")
            self.assertIn("lowercase", problem)

    def test_good_competitor_titles_are_kept(self):
        for title in ("This Ending Broke Everyone",      # no terminal punctuation, still fine
                      "HOW DID MADA WIN THIS?!!!?",      # proven winner, all caps
                      "He Lost $1,000,000 Today",
                      "But He Never Expected This.",     # a capitalised "But" is a hook
                      "15 Sea Bombs Surrounding His Yacht - What?"):
            self.assertIsNone(select_clips.title_quality_problem(title), title)

    def test_a_truncated_tail_is_rejected(self):
        self.assertIn("hanging",
                      select_clips.title_quality_problem(
                          "He walked in and told them he wanted fifty thousand dollars for"))
        self.assertIn("filler", select_clips.title_quality_problem("and then he lost it all."))
        self.assertIn("subordinate",
                      select_clips.title_quality_problem("Even though they won the challenge."))

    def test_title_from_transcript_is_a_complete_sentence(self):
        title = select_clips.title_from_transcript(SPOKEN)
        self.assertIsNone(select_clips.title_quality_problem(title), title)
        self.assertIn("$100,000", title, "spoken money must be normalised")
        self.assertTrue(title[0].isupper(), title)

    def test_money_percent_and_thousands_are_normalised(self):
        self.assertIn("$150,000",
                      select_clips.title_from_transcript(
                          "I walked in and asked for one hundred and fifty thousand dollars."))
        self.assertIn("10%",
                      select_clips.title_from_transcript(
                          "They offered me ten percent of the company today."))

    def test_never_cuts_a_word_in_half(self):
        # The old bug: a 76-character slice cut the ASR word ("...they were startin").
        title = select_clips.title_from_transcript(SPOKEN * 3)
        self.assertLessEqual(len(title), 71)
        spoken = {w.lower() for w in SPOKEN.split()}
        for word in title.strip(".").split():
            bare = word.strip(".,!?;:")
            if bare and bare[0].isalpha():
                self.assertIn(bare.lower(), spoken, f"'{bare}' is not a whole spoken word")
        self.assertTrue(title.endswith((".", "!", "?")), title)

    def test_empty_transcript_gets_the_neutral_title(self):
        self.assertEqual(select_clips.title_from_transcript(""),
                         select_clips.NEUTRAL_TITLE)

    def test_deterministic_fallback_never_ships_a_fragment(self):
        clips = select_clips.deterministic_fallback(
            word_timings(SPOKEN), 1, 35, 60, [])
        self.assertTrue(clips)
        title = clips[0]["suggested_title"]
        self.assertIsNone(select_clips.title_quality_problem(title), title)
        self.assertNotIn("vers", title)

    def test_safe_title_keeps_a_good_title_and_rewrites_a_fragment(self):
        self.assertEqual(
            select_clips._safe_title("This Ending Broke Everyone", "hook", "clip text"),
            "This Ending Broke Everyone")
        rebuilt = select_clips._safe_title(SHIPPED_FRAGMENTS[1], SHIPPED_FRAGMENTS[1],
                                           SPOKEN)
        self.assertIsNone(select_clips.title_quality_problem(rebuilt), rebuilt)
        self.assertNotEqual(rebuilt, SHIPPED_FRAGMENTS[1])


class CompetitorCaptionTest(unittest.TestCase):
    """The new preset is the default; the old presets keep their exact behaviour."""

    WORDS = [{"w": "he", "start": 0.0, "end": 0.3},
             {"w": "lost", "start": 0.3, "end": 0.6},
             {"w": "$1,000,000", "start": 0.6, "end": 1.0},
             {"w": "today.", "start": 1.0, "end": 1.4}]

    def _hook_events(self, ass):
        return [line for line in ass.splitlines() if ",Hook,,0,0,0" in line]

    def test_competitor_is_the_default_style(self):
        with mock.patch.dict(os.environ, {"CLIP_CAPTION_STYLE": ""}):
            self.assertEqual(build_captions.default_style_name(), "competitor")

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"CLIP_CAPTION_STYLE": "hormozi"}):
            self.assertEqual(build_captions.default_style_name(), "hormozi")

    def test_persistent_hook_covers_the_whole_clip(self):
        name, style = build_captions.load_style("competitor")
        self.assertEqual(name, "competitor")
        ass, _ = build_captions.build_ass(self.WORDS, style, True,
                                         hook="HE LOST $1,000,000 TODAY", total=42.0)
        event = self._hook_events(ass)[0]
        self.assertIn("0:00:00.00,0:00:42.00", event)
        self.assertIn("\\fad(150,0)", event)

    def test_hook_is_multi_color_with_money_in_gold(self):
        _, style = build_captions.load_style("competitor")
        palette = build_captions.hook_colors(style)
        ass, _ = build_captions.build_ass(self.WORDS, style, True,
                                         hook="HE LOST $1,000,000 TODAY", total=42.0)
        event = self._hook_events(ass)[0]
        self.assertIn(f"{{\\c{palette['gold']}}}$1,000,000", event)
        self.assertIn(f"{{\\c{palette['alert']}}}LOST", event)
        self.assertIn(f"{{\\c{palette['gold_light']}}}TODAY", event)

    def test_hook_wraps_to_at_most_three_lines(self):
        _, style = build_captions.load_style("competitor")
        size = style["hook_font_size"]
        hook = "HE LOST A MILLION DOLLARS IN THE LAST ROUND OF THE CHALLENGE"
        lines = build_captions.wrap_hook(hook, build_captions.hook_char_budget(size))
        self.assertLessEqual(len(lines), 3)
        budget = build_captions.hook_char_budget(size)
        for line in lines:
            self.assertLessEqual(len(" ".join(line)), budget + 4, line)

    def test_long_hook_shrinks_instead_of_truncating(self):
        _, style = build_captions.load_style("competitor")
        hook = "HELICOPTER JUMP INTO OCEAN AT $16M ISLAND"
        size, lines = build_captions.fit_hook_layout(hook, style["hook_font_size"])
        flat = " ".join(" ".join(line) for line in lines)
        self.assertLess(size, style["hook_font_size"], "the banner should give up type, not words")
        self.assertLessEqual(len(lines), 3)
        self.assertNotIn("...", flat)
        self.assertEqual(flat, hook, "every hook token must survive the wrap")

    def test_fitted_hook_reaches_the_ass_style_and_event(self):
        _, style = build_captions.load_style("competitor")
        hook = "HELICOPTER JUMP INTO OCEAN AT $16M ISLAND"
        ass, _ = build_captions.build_ass(self.WORDS, style, True, hook=hook, total=42.0)
        fitted, _ = build_captions.fit_hook_layout(hook, style["hook_font_size"])
        hook_style = [line for line in ass.splitlines() if line.startswith("Style: Hook")][0]
        self.assertIn(f",{fitted},", hook_style)
        event = self._hook_events(ass)[0]
        self.assertNotIn("...", event)
        for token in hook.split():
            self.assertIn(token, event)

    def test_hook_that_fits_keeps_the_configured_size(self):
        _, style = build_captions.load_style("competitor")
        size, lines = build_captions.fit_hook_layout("HE LOST $1,000,000", style["hook_font_size"])
        self.assertEqual(size, style["hook_font_size"])
        self.assertEqual(" ".join(" ".join(line) for line in lines), "HE LOST $1,000,000")

    def test_old_presets_keep_the_flash_and_stay_single_color(self):
        for preset in ("hormozi", "brand", "clean"):
            _, style = build_captions.load_style(preset)
            ass, _ = build_captions.build_ass(
                self.WORDS, style, style.get("animation") == "word-highlight",
                hook="HE LOST $1,000,000 TODAY", hook_secs=1.6, total=42.0)
            event = self._hook_events(ass)[0]
            self.assertIn("0:00:00.00,0:00:01.60", event, preset)
            self.assertNotIn("\\c&H", event, preset)

    def test_unknown_style_falls_back_to_the_default(self):
        name, _ = build_captions.load_style("does-not-exist")
        self.assertEqual(name, "competitor")

    def test_every_preset_is_loadable(self):
        cfg = json.loads((REPO / "config" / "caption_styles.json").read_text("utf-8"))
        self.assertIn("competitor", cfg["styles"])
        for preset in cfg["styles"]:
            name, style = build_captions.load_style(preset)
            self.assertEqual(name, preset)
            self.assertTrue(style.get("primary_color"))


if __name__ == "__main__":
    unittest.main()
