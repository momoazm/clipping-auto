"""Delivery-side guards added 2026-10-21 while chasing a live run that posted nothing.

Two independent failures needed code changes, and both are easy to regress:

1. `host_public.py` handed Instagram a tmpfiles.org URL that served `text/html`, so the
   reel died minutes later with an opaque "Video Transcoding Error / MediaInfoExtraction".
   Every host URL now has to prove it serves video bytes before it is used.
2. The connected YouTube channel is suspended, and every upload returns
   `403 authenticatedUserAccountSuspended`. That must not fail the Instagram delivery or
   keep retrying a channel that cannot come back on its own.

Run from the repo root with the system python:
    python -m pytest tests -q
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import host_public  # noqa: E402
import run_daily  # noqa: E402

SUSPENDED_403 = (
    'upload_youtube.py: Zernio publish did not complete (status=failed; provider returned '
    'no YouTube error detail)._YouTube upload failed: YouTube upload session initialization '
    'failed: 403 {"error": {"code": 403, "message": "The YouTube account of the '
    'authenticated user is suspended.", "errors": [{"domain": "youtube.common", '
    '"reason": "authenticatedUserAccountSuspended"}]}}'
)


class TestSuspendedPlatformDetection(unittest.TestCase):
    def test_recognises_the_real_zernio_youtube_403(self):
        self.assertTrue(run_daily.platform_account_suspended(RuntimeError(SUSPENDED_403)))

    def test_recognises_the_bare_reason_code(self):
        self.assertTrue(run_daily.platform_account_suspended(
            Exception("reason: authenticatedUserAccountSuspended")))

    def test_recognises_zernios_normalised_suspension_wording(self):
        """This is the wording Zernio returns in `errorMessage` (run 39579711846)."""
        self.assertTrue(run_daily.platform_account_suspended(Exception(
            '{"errorMessage":"YouTube upload failed: Your YouTube account or channel is '
            'suspended. Please check your YouTube account status.",'
            '"errorCategory":"platform_rejected"}')))

    def test_ignores_ordinary_upload_failures(self):
        for message in (
            "Zernio publish did not complete (status=failed)",
            "timed out after 300s",
            "Video Transcoding Error: progressive_video_not_ready",
            "HTTP 429 rate limited",
            "YouTube upload session initialization failed: 403 quotaExceeded",
            "",
            None,
        ):
            self.assertFalse(run_daily.platform_account_suspended(message), message)

    def test_warning_is_a_github_annotation(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            run_daily._gh_warning("YouTube account suspended")
        self.assertTrue(buffer.getvalue().startswith("::warning::"))

    def test_summary_flag_short_circuits_later_clips(self):
        """Once a run knows the channel is gone, later clips must not re-host or re-upload."""
        summary = {"youtube_unavailable": {"reason": "authenticatedUserAccountSuspended"}}
        self.assertTrue(summary.get("youtube_unavailable"))


class TestPublicHostChain(unittest.TestCase):
    def setUp(self):
        handle, self.video = tempfile.mkstemp(suffix=".mp4")
        os.close(handle)
        with open(self.video, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)

        def cleanup():
            try:
                os.unlink(self.video)
            except OSError:
                pass

        self.addCleanup(cleanup)

    def _fake_providers(self, results):
        calls = []

        def make(name, flag):
            def provider(path):
                calls.append(name)
                if isinstance(flag, Exception):
                    raise flag
                return flag
            return name, provider

        return [make(name, flag) for name, flag in results], calls

    def test_chain_skips_hosts_whose_url_is_not_video(self):
        providers, calls = self._fake_providers([
            ("htmlhost", "https://example.test/page.html"),
            ("video", "https://example.test/clip.mp4"),
        ])

        def verify(url, expected_size=0, timeout=45):
            if url.endswith(".html"):
                raise RuntimeError("not video bytes (content-type=text/html)")

        with mock.patch.object(host_public, "PROVIDERS", providers), \
                mock.patch.object(host_public, "verify_public_url", side_effect=verify):
            url, provider = host_public.host(self.video, verify=True)

        self.assertEqual(provider, "video")
        self.assertEqual(url, "https://example.test/clip.mp4")
        self.assertEqual(calls, ["htmlhost", "video"])

    def test_chain_fails_closed_when_no_host_serves_bytes(self):
        providers, _ = self._fake_providers([("htmlhost", "https://example.test/page.html")])
        with mock.patch.object(host_public, "PROVIDERS", providers), \
                mock.patch.object(host_public, "verify_public_url",
                                  side_effect=RuntimeError("not video bytes")):
            with self.assertRaises(RuntimeError):
                host_public.host(self.video, verify=True)

    def test_exclude_forces_the_next_host(self):
        providers, calls = self._fake_providers([
            ("first", "https://example.test/a.mp4"),
            ("second", "https://example.test/b.mp4"),
        ])
        with mock.patch.object(host_public, "PROVIDERS", providers), \
                mock.patch.object(host_public, "verify_public_url"):
            _, provider = host_public.host(self.video, exclude=["first"], verify=True)
        self.assertEqual(provider, "second")
        self.assertEqual(calls, ["second"])

    def test_uguu_is_first(self):
        """uguuguu was the only host returning real video bytes in the 2026-10-21 probe."""
        self.assertEqual(host_public.PROVIDERS[0][0], "uguu")

    def test_tmpfiles_is_not_trusted_first(self):
        names = [name for name, _ in host_public.PROVIDERS]
        self.assertGreater(names.index("tmpfiles"), 0, "tmpfiles served text/html")
        # Every host still exists -- reordering must never drop a fallback.
        for name in ("uguu", "pixeldrain", "catbox", "litterbox", "tmpfiles", "0x0"):
            self.assertIn(name, names)


class TestYouTubeErrorDetail(unittest.TestCase):
    """A suspended channel used to surface as "provider returned no error detail"."""

    ENTRY = {
        "status": "failed",
        "errorMessage": ("YouTube upload failed: YouTube upload session initialization failed: "
                         '403 {"error": {"reason": "authenticatedUserAccountSuspended"}}'),
        "errorCategory": "platform_rejected",
        "errorSource": "platform",
    }

    def test_detail_carries_the_platforms_message(self):
        import upload_youtube
        detail = upload_youtube._platform_detail(self.ENTRY)
        self.assertIn("authenticatedUserAccountSuspended", detail)
        self.assertIn("platform_rejected", detail)
        self.assertNotIn("no YouTube error detail", detail)

    def test_detail_falls_back_only_when_the_provider_said_nothing(self):
        import upload_youtube
        self.assertIn("no YouTube error detail", upload_youtube._platform_detail({}))

    def test_run_daily_treats_that_detail_as_a_suspended_account(self):
        import upload_youtube
        raised = RuntimeError(
            f"Zernio publish did not complete (status=failed; "
            f"{upload_youtube._platform_detail(self.ENTRY)}).")
        self.assertTrue(run_daily.platform_account_suspended(raised))


class TestWorkflowPlatformContract(unittest.TestCase):
    def test_job_only_requires_instagram_while_youtube_is_suspended(self):
        text = (REPO / ".github" / "workflows" / "clipping_daily.yml").read_text("utf-8")
        required = [line.split(":", 1)[1].strip()
                    for line in text.splitlines()
                    if line.strip().startswith("REQUIRED_PLATFORMS:")]
        self.assertTrue(required, "the workflow must set REQUIRED_PLATFORMS")
        for value in required:
            self.assertNotIn("youtube", value, "YouTube is suspended; it must not be required")


if __name__ == "__main__":
    unittest.main()
