"""Regression tests for the 2026-10-12 fix batch (C1-C6).

Run from the repo root with the system python:
    python -m unittest discover -s tests
"""
import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import find_source_video  # noqa: E402
import run_daily  # noqa: E402
import select_clips  # noqa: E402


class TestMinClipsRequired(unittest.TestCase):
    """C2: min-2 is a fresh-source bar; a continuation run may finish with one clip."""

    def test_fresh_source_keeps_the_configured_minimum(self):
        self.assertEqual(run_daily.min_clips_required(2, False), 2)
        self.assertEqual(run_daily.min_clips_required(3, False), 3)

    def test_continuation_drops_to_one(self):
        self.assertEqual(run_daily.min_clips_required(2, True), 1)
        self.assertEqual(run_daily.min_clips_required(3, True), 1)

    def test_never_zero(self):
        self.assertEqual(run_daily.min_clips_required(0, False), 1)


class TestSafeTitle(unittest.TestCase):
    """C4: the model title survives unless a screen rejects it; never unsafe, never empty."""

    def test_specific_clean_title_is_kept(self):
        title = "This Ending Broke Everyone"
        self.assertEqual(
            select_clips._safe_title(title, "welcome back to the challenge",
                                     "we are giving away one million dollars today"),
            title,
        )

    def test_unsafe_title_is_rewritten_and_never_passes(self):
        out = select_clips._safe_title("He Tried To Murder The Winner",
                                       "the last person out wins everything",
                                       "whoever is left takes the prize")
        self.assertIsNone(select_clips.UNSAFE_TITLE_RE.search(out))
        self.assertNotIn("Murder", out)

    def test_reversed_agent_claim_is_replaced(self):
        evidence = "everyone who drops improves the odds"
        out = select_clips._safe_title("Who Quits Wins The Mansion", evidence, evidence)
        self.assertEqual(out, "Someone Dropped - Now the Mansion Odds Improve!")
        self.assertIsNone(select_clips.UNSAFE_TITLE_RE.search(out))

    def test_elimination_keeps_the_survivor_count(self):
        out = select_clips._safe_title("", "someone is officially out",
                                       "only 3 people remain in the challenge")
        self.assertIn("Only 3 People Remain", out)

    def test_empty_and_garbage_inputs_are_never_empty(self):
        self.assertTrue(select_clips._safe_title(None, None, None))
        self.assertTrue(select_clips._safe_title("", "", ""))
        self.assertTrue(select_clips._safe_title("   ", "   ", "   "))

    def test_unsafe_screen_matches_the_run_daily_hard_block(self):
        # The two patterns must stay in sync (run_daily hard-blocks, this rewrites).
        self.assertEqual(select_clips.UNSAFE_TITLE_RE.pattern,
                         run_daily.UNSAFE_TITLE_RE.pattern)
        for phrase in ("murder", "suicide", "kidnapped", "nude", "massacre"):
            self.assertIsNotNone(select_clips.UNSAFE_TITLE_RE.search(phrase))


class TestSourceAttemptCap(unittest.TestCase):
    """C1: an unfinished plan gets a no-progress attempt cap and is then quarantined."""

    def test_cap_comes_from_config_with_a_default(self):
        self.assertEqual(find_source_video.max_source_attempts(
            {"max_source_attempts": 7}), 7)
        self.assertEqual(find_source_video.max_source_attempts({}), 5)
        self.assertEqual(find_source_video.max_source_attempts(None), 5)

    def test_exhausted_ids_flags_capped_and_operator_flagged_sources(self):
        ledger = {
            "capped": {"count": 5, "windows": 2},
            "short": {"count": 2, "windows": 2},
            "flagged": {"count": 1, "windows": 2, "exhausted": True},
        }
        self.assertEqual(
            find_source_video.exhausted_ids(ledger, 5),
            {"capped", "flagged"},
        )

    def test_no_progress_increments_and_quarantines_at_the_cap(self):
        # Four no-progress attempts already recorded; the next offer is the 5th and
        # reaches the cap of 5, so it is quarantined right away.
        state = {"source_attempts": {"abc": {"count": 4, "windows": 2}}}
        self.assertTrue(find_source_video.track_source_offer(state, "abc", 2, 5))
        self.assertEqual(state["source_attempts"]["abc"]["count"], 5)
        self.assertTrue(state["source_attempts"]["abc"]["exhausted"])

        # Once exhausted it stays exhausted on any later offer.
        self.assertTrue(find_source_video.track_source_offer(state, "abc", 2, 5))
        self.assertEqual(state["source_attempts"]["abc"]["count"], 6)

    def test_quarantine_only_when_the_cap_is_reached(self):
        state = {"source_attempts": {"abc": {"count": 1, "windows": 2}}}
        self.assertFalse(find_source_video.track_source_offer(state, "abc", 2, 5))
        self.assertEqual(state["source_attempts"]["abc"]["count"], 2)

    def test_progress_resets_the_counter(self):
        state = {"source_attempts": {"abc": {"count": 4, "windows": 1}}}
        self.assertFalse(find_source_video.track_source_offer(state, "abc", 2, 5))
        entry = state["source_attempts"]["abc"]
        self.assertEqual(entry["count"], 1)
        self.assertEqual(entry["windows"], 2)
        self.assertFalse(entry["exhausted"])

    def test_first_offer_starts_at_one(self):
        state = {}
        self.assertFalse(find_source_video.track_source_offer(state, "new", 0, 5))
        self.assertEqual(state["source_attempts"]["new"]["count"], 1)

    def test_preseeded_history_quarantine_is_present(self):
        # The two sources that poisoned every scheduled run 2026-10-04..10-11.
        history = json.loads((REPO / "state" / "clipped_history.json").read_text(
            encoding="utf-8"))
        ledger = find_source_video.attempt_ledger(history)
        cap = find_source_video.max_source_attempts(
            json.loads((REPO / "config" / "channels.json").read_text(encoding="utf-8")))
        quarantined = find_source_video.exhausted_ids(ledger, cap)
        self.assertIn("v9QtM6qnG50", quarantined)
        self.assertIn("kJu5VMN3yow", quarantined)


class TestNoSourceStreak(unittest.TestCase):
    """C6: a multi-day drought WhatsApps Moemen once, at the threshold, rate-limited."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "no_source_streak.json"
        self._old_path = run_daily.NO_SOURCE_STREAK
        self._old_run_tool = run_daily.run_tool
        self.calls = []
        run_daily.NO_SOURCE_STREAK = self.path
        run_daily.run_tool = lambda script, *args: self.calls.append((script,) + args) or {}

    def tearDown(self):
        run_daily.NO_SOURCE_STREAK = self._old_path
        run_daily.run_tool = self._old_run_tool
        self._tmp.cleanup()

    def _write(self, state):
        self.path.write_text(json.dumps(state), encoding="utf-8")

    def test_below_threshold_counts_without_alerting(self):
        run_daily.note_no_source("No source was available", dry_run=False)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["count"], 1)
        self.assertEqual(self.calls, [])

    def test_same_day_hits_are_counted_once(self):
        self._write({"count": 1, "counted_on": datetime.date.today().isoformat()})
        run_daily.note_no_source("again", dry_run=False)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["count"], 1)

    def test_threshold_alerts_whatsapp_once(self):
        self._write({"count": 2, "counted_on": "2026-01-01"})
        run_daily.note_no_source("no qualifying source", dry_run=False)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["count"], 3)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "send_whatsapp.py")
        self.assertIn("--text", self.calls[0])

    def test_cooldown_suppresses_a_second_alert(self):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._write({"count": 3, "counted_on": "2026-01-01", "last_alert_at": now})
        run_daily.note_no_source("still dry", dry_run=False)
        self.assertEqual(self.calls, [])

    def test_dry_run_counts_nothing(self):
        run_daily.note_no_source("test only", dry_run=True)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.calls, [])

    def test_reset_clears_the_streak_but_keeps_the_file(self):
        self._write({"count": 3, "counted_on": "2026-01-01",
                     "last_alert_at": "2026-01-01T00:00:00+00:00"})
        run_daily.reset_no_source_streak()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["last_alert_at"], "2026-01-01T00:00:00+00:00")

    def test_reset_is_a_no_op_without_a_streak_file(self):
        run_daily.reset_no_source_streak()
        self.assertFalse(self.path.exists())


class TestTranscriptSnippet(unittest.TestCase):
    """C5: the hashtag prompt gets real speech, not a second copy of the hook."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_tmp = run_daily.TMP
        run_daily.TMP = Path(self._tmp.name)

    def tearDown(self):
        run_daily.TMP = self._old_tmp
        self._tmp.cleanup()

    def test_words_inside_the_window_are_joined(self):
        words = [
            {"w": "welcome", "start": 0.0, "end": 0.4},
            {"w": "to", "start": 0.4, "end": 0.5},
            {"w": "the", "start": 0.5, "end": 0.6},
            {"w": "challenge", "start": 0.6, "end": 1.2},
            {"w": "afterwards", "start": 9.0, "end": 9.6},
        ]
        (run_daily.TMP / "transcript.json").write_text(
            json.dumps({"words": words, "segments": []}), encoding="utf-8")
        out = run_daily.clip_transcript_snippet({"start": 0.0, "end": 1.5})
        self.assertEqual(out, "welcome to the challenge")

    def test_tight_window_falls_back_to_segments(self):
        (run_daily.TMP / "transcript.json").write_text(
            json.dumps({"words": [], "segments": [
                {"text": " only 3 people remain", "start": 10.0, "end": 14.0}]}),
            encoding="utf-8")
        out = run_daily.clip_transcript_snippet({"start": 10.0, "end": 10.5})
        self.assertEqual(out, "only 3 people remain")

    def test_missing_transcript_or_window_degrades_to_empty(self):
        self.assertEqual(run_daily.clip_transcript_snippet({"start": 0, "end": 1}), "")
        (run_daily.TMP / "transcript.json").write_text(
            json.dumps({"words": [{"w": "hi", "start": 0, "end": 1}]}), encoding="utf-8")
        self.assertEqual(run_daily.clip_transcript_snippet({"start": "x", "end": 1}), "")

    def test_snippet_is_truncated_to_the_limit(self):
        words = [{"w": "word", "start": i * 0.1, "end": i * 0.1 + 0.05}
                 for i in range(100)]
        (run_daily.TMP / "transcript.json").write_text(
            json.dumps({"words": words, "segments": []}), encoding="utf-8")
        out = run_daily.clip_transcript_snippet({"start": 0.0, "end": 10.0}, limit=20)
        self.assertLessEqual(len(out), 20)
        self.assertTrue(out.startswith("word"))
        self.assertNotIn("  ", out)


class TestSourcePoolDepth(unittest.TestCase):
    """C7: a "pool exhausted" result must be able to show it scanned deep enough.

    2026-10-16..10-20: every channel's first 60 uploads were already reserved while the
    61st-65th were untouched, so scan_depth=60 made a healthy pool look like a drought
    and the channel silently stopped posting for three scheduled days.
    """

    def test_configured_scan_depth_is_wide_enough(self):
        cfg = json.loads((REPO / "config" / "channels.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(
            int(cfg["scan_depth"]), 120,
            "scan_depth must leave headroom past the reserved window; 60 starved the feed")

    def test_scan_report_counts_listed_vs_free_per_channel(self):
        cache = {"MrBeast": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                 "Beast Reacts": [{"id": "x"}]}
        report = find_source_video.scan_report(cache, {"a", "x"}, 200)
        self.assertEqual(report["depth"], 200)
        self.assertEqual(report["listed"], {"MrBeast": 3, "Beast Reacts": 1})
        self.assertEqual(report["free"], {"MrBeast": 2, "Beast Reacts": 0})

    def test_scan_report_tolerates_channels_that_failed_to_fetch(self):
        cache = {"MrBeast": [{"id": "a"}], "More MrBeast": []}
        report = find_source_video.scan_report(cache, set(), 200)
        self.assertEqual(report["listed"]["More MrBeast"], 0)
        self.assertEqual(report["free"]["More MrBeast"], 0)


if __name__ == "__main__":
    unittest.main()
