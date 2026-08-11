from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from scrapers.utils.browser import PlaywrightFetchMixin


class _Mixin(PlaywrightFetchMixin):
    min_wait = 0.0
    max_wait = 0.0


# ---------------------------------------------------------------------------
# _try_start_xvfb
# ---------------------------------------------------------------------------

class TryStartXvfbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mixin = _Mixin()

    def _env_no_display(self) -> dict:
        return {k: v for k, v in os.environ.items() if k != "DISPLAY"}

    def test_returns_true_when_display_already_set(self) -> None:
        with patch.dict("os.environ", {"DISPLAY": ":0"}, clear=False):
            self.assertTrue(self.mixin._try_start_xvfb())

    def test_returns_false_when_xvfb_not_installed(self) -> None:
        with patch.dict("os.environ", self._env_no_display(), clear=True):
            with patch("os.path.exists", return_value=False):
                with patch("subprocess.Popen", side_effect=FileNotFoundError):
                    with patch("time.sleep"):
                        self.assertFalse(self.mixin._try_start_xvfb())

    def test_skips_locked_slot_starts_on_next(self) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running

        def exists_side(path: str) -> bool:
            return path == "/tmp/.X99-lock"

        with patch.dict("os.environ", self._env_no_display(), clear=True):
            with patch("os.path.exists", side_effect=exists_side):
                with patch("subprocess.Popen", return_value=mock_proc):
                    with patch("time.sleep"):
                        result = self.mixin._try_start_xvfb()
                    self.assertTrue(result)
                    self.assertEqual(os.environ.get("DISPLAY"), ":100")
                    self.assertIs(self.mixin._xvfb_proc, mock_proc)

        self.mixin._xvfb_proc = None

    def test_terminates_dead_procs_and_returns_false(self) -> None:
        dead_proc = MagicMock()
        dead_proc.poll.return_value = 1  # died immediately

        with patch.dict("os.environ", self._env_no_display(), clear=True):
            with patch("os.path.exists", return_value=False):
                with patch("subprocess.Popen", return_value=dead_proc):
                    with patch("time.sleep"):
                        result = self.mixin._try_start_xvfb()

        self.assertFalse(result)
        self.assertEqual(dead_proc.terminate.call_count, 21)  # range(99, 120) = 21 slots


# ---------------------------------------------------------------------------
# _stop_browser
# ---------------------------------------------------------------------------

class StopBrowserTests(unittest.TestCase):
    def _started_mixin(self) -> _Mixin:
        mixin = _Mixin()
        mixin._context = MagicMock()
        mixin._browser = MagicMock()
        mixin._pw = MagicMock()
        mixin._xvfb_proc = MagicMock()
        return mixin

    def test_cleans_up_all_resources_and_removes_display(self) -> None:
        mixin = self._started_mixin()
        ctx, browser, pw, xvfb = mixin._context, mixin._browser, mixin._pw, mixin._xvfb_proc

        with patch.dict("os.environ", {"DISPLAY": ":99"}, clear=False):
            mixin._stop_browser()
            self.assertNotIn("DISPLAY", os.environ)

        ctx.close.assert_called_once()
        browser.close.assert_called_once()
        pw.stop.assert_called_once()
        xvfb.terminate.assert_called_once()
        xvfb.wait.assert_called_once_with(timeout=5)
        self.assertIsNone(mixin._context)
        self.assertIsNone(mixin._browser)
        self.assertIsNone(mixin._pw)
        self.assertIsNone(mixin._xvfb_proc)

    def test_safe_when_nothing_started(self) -> None:
        mixin = _Mixin()
        mixin._stop_browser()  # must not raise

    def test_does_not_remove_display_when_no_xvfb_proc(self) -> None:
        mixin = _Mixin()
        mixin._context = MagicMock()
        mixin._browser = MagicMock()
        mixin._pw = MagicMock()
        # _xvfb_proc intentionally left None

        with patch.dict("os.environ", {"DISPLAY": ":0"}, clear=False):
            mixin._stop_browser()
            self.assertIn("DISPLAY", os.environ)


# ---------------------------------------------------------------------------
# fetch_html_playwright
# ---------------------------------------------------------------------------

class FetchHtmlPlaywrightTests(unittest.TestCase):
    def _mixin_with_context(self):
        mixin = _Mixin()
        ctx = MagicMock()
        mixin._context = ctx
        return mixin, ctx

    def test_returns_html_on_success(self) -> None:
        mixin, ctx = self._mixin_with_context()
        page = MagicMock()
        page.content.return_value = "<html>ok</html>"
        ctx.new_page.return_value = page

        with patch.object(mixin, "_start_browser"):
            with patch("time.sleep"):
                result = mixin.fetch_html_playwright("https://example.com", max_retries=1)

        self.assertEqual(result, "<html>ok</html>")
        page.close.assert_called_once()

    def test_returns_empty_after_all_retries_fail(self) -> None:
        mixin, ctx = self._mixin_with_context()
        page = MagicMock()
        page.goto.side_effect = Exception("timeout")
        ctx.new_page.return_value = page

        with patch.object(mixin, "_start_browser"):
            with patch("time.sleep"):
                result = mixin.fetch_html_playwright("https://example.com", max_retries=2)

        self.assertEqual(result, "")
        self.assertEqual(ctx.new_page.call_count, 2)

    def test_page_always_closed_on_exception(self) -> None:
        mixin, ctx = self._mixin_with_context()
        page = MagicMock()
        page.goto.side_effect = Exception("network error")
        ctx.new_page.return_value = page

        with patch.object(mixin, "_start_browser"):
            with patch("time.sleep"):
                mixin.fetch_html_playwright("https://example.com", max_retries=1)

        page.close.assert_called_once()

    def test_waf_challenge_waits_and_returns_resolved_content(self) -> None:
        mixin, ctx = self._mixin_with_context()
        page = MagicMock()
        page.content.return_value = "AwsWafIntegration challenge page"
        ctx.new_page.return_value = page
        resolved_html = "<html>real content</html>"

        with patch.object(mixin, "_start_browser"):
            with patch.object(mixin, "_wait_waf_resolved", return_value=resolved_html):
                with patch("time.sleep"):
                    result = mixin.fetch_html_playwright("https://example.com", max_retries=1)

        self.assertEqual(result, resolved_html)

    def test_unresolved_waf_retries_then_returns_empty(self) -> None:
        mixin, ctx = self._mixin_with_context()
        page = MagicMock()
        page.content.return_value = "AwsWafIntegration challenge page"
        ctx.new_page.return_value = page

        with patch.object(mixin, "_start_browser"):
            with patch.object(mixin, "_wait_waf_resolved", return_value=None):
                with patch("time.sleep"):
                    result = mixin.fetch_html_playwright("https://example.com", max_retries=2)

        self.assertEqual(result, "")
        self.assertEqual(ctx.new_page.call_count, 2)

    def test_wait_for_selector_applied_when_provided(self) -> None:
        mixin, ctx = self._mixin_with_context()
        page = MagicMock()
        page.content.return_value = "<html>content</html>"
        ctx.new_page.return_value = page

        with patch.object(mixin, "_start_browser"):
            with patch.object(mixin, "_wait_selector_stable") as mock_stable:
                with patch("time.sleep"):
                    result = mixin.fetch_html_playwright(
                        "https://example.com",
                        max_retries=1,
                        wait_for_selector=".lot-card",
                    )

        mock_stable.assert_called_once_with(page, ".lot-card")
        self.assertEqual(result, "<html>content</html>")

    def test_no_selector_wait_when_not_provided(self) -> None:
        mixin, ctx = self._mixin_with_context()
        page = MagicMock()
        page.content.return_value = "<html>content</html>"
        ctx.new_page.return_value = page

        with patch.object(mixin, "_start_browser"):
            with patch.object(mixin, "_wait_selector_stable") as mock_stable:
                with patch("time.sleep"):
                    mixin.fetch_html_playwright("https://example.com", max_retries=1)

        mock_stable.assert_not_called()


# ---------------------------------------------------------------------------
# _wait_waf_resolved
# ---------------------------------------------------------------------------

class WaitWafResolvedTests(unittest.TestCase):
    def test_returns_content_when_challenge_clears(self) -> None:
        page = MagicMock()
        clean_html = "<html>" + "x" * 6000 + "</html>"
        page.content.side_effect = [
            "AwsWafIntegration still showing",
            clean_html,
        ]

        with patch("time.sleep"):
            result = _Mixin()._wait_waf_resolved(page)

        self.assertEqual(result, clean_html)

    def test_returns_none_when_challenge_never_clears(self) -> None:
        page = MagicMock()
        page.content.return_value = "AwsWafIntegration permanent block"

        with patch("time.sleep"):
            result = _Mixin()._wait_waf_resolved(page)

        self.assertIsNone(result)

    def test_requires_content_length_above_threshold(self) -> None:
        page = MagicMock()
        # No AwsWafIntegration but content is too short (<= 5000 chars)
        page.content.return_value = "<html>short</html>"

        with patch("time.sleep"):
            result = _Mixin()._wait_waf_resolved(page)

        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _wait_selector_stable
# ---------------------------------------------------------------------------

class WaitSelectorStableTests(unittest.TestCase):
    def test_waits_until_element_count_stabilizes(self) -> None:
        page = MagicMock()
        page.evaluate.side_effect = [2, 4, 4]  # grows then stable

        with patch("time.sleep"):
            PlaywrightFetchMixin._wait_selector_stable(page, ".card")

        page.wait_for_selector.assert_called_once_with(".card", timeout=15_000)

    def test_handles_selector_timeout_without_raising(self) -> None:
        page = MagicMock()
        page.wait_for_selector.side_effect = Exception("selector not found")

        with patch("time.sleep"):
            PlaywrightFetchMixin._wait_selector_stable(page, ".missing")  # must not raise


if __name__ == "__main__":
    unittest.main()
