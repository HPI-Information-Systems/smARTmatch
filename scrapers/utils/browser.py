from __future__ import annotations

import os
import random
import subprocess
import time
from typing import Optional

from playwright.sync_api import Browser, BrowserContext, Playwright, sync_playwright

_USER_AGENTS = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
)

_EXTRA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-AT,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Injected before any page script runs.  Patches the navigator properties
# that Cloudflare, AWS WAF, and similar services use to detect automation.
# Kept minimal: aggressive patches (WebGL, canvas, permissions) interfere
# with WAF challenge fingerprinting and cause verification failures.
_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
document.documentElement.removeAttribute('webdriver');
if (!window.chrome) {
    window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
}
Object.defineProperty(navigator, 'languages',           { get: () => ['de-AT', 'de', 'en-US', 'en'] });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
"""


class PlaywrightFetchMixin:
    """Headless Chromium page fetcher via Playwright.

    Manages a single persistent browser context for the lifetime of a
    scraper run.  Call ``_start_browser()`` / ``_stop_browser()`` in
    ``_prepare_run`` / ``_after_run`` to bind the lifetime to the run;
    otherwise the browser starts lazily on the first fetch call.

    Prefers non-headless mode (more transparent to bot-detection) and
    auto-starts a virtual Xvfb framebuffer when no real display is available.
    Falls back to headless if Xvfb is not installed.
    """

    _pw: Optional[Playwright] = None
    _browser: Optional[Browser] = None
    _context: Optional[BrowserContext] = None
    _xvfb_proc: Optional[subprocess.Popen] = None

    def _log(self, message: str) -> None:
        """Prefix terminal output with the host scraper's 3-letter tag.

        Falls back to ``[scr]`` when the mixin is used outside a ``Scraper``
        (which sets ``log_prefix`` in its ``__init__``).
        """

        log_method = getattr(self, "log", None)
        if callable(log_method):
            log_method(message)
            return
        prefix = getattr(self, "log_prefix", "scr")
        print(f"[{prefix}] {message}")

    def _start_browser(self) -> None:
        if self._context is not None:
            return

        headless = not self._try_start_xvfb()

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        self._context = self._browser.new_context(
            user_agent=random.choice(_USER_AGENTS),
            locale="de-AT",
            viewport={"width": 1280, "height": 800},
            extra_http_headers=_EXTRA_HEADERS,
        )
        self._context.add_init_script(_STEALTH_SCRIPT)

    def _try_start_xvfb(self) -> bool:
        """Start Xvfb on the first free display slot.  Returns True on success."""
        if os.environ.get("DISPLAY"):
            return True  # Real or already-configured virtual display

        for num in range(99, 120):
            if os.path.exists(f"/tmp/.X{num}-lock"):
                continue
            try:
                proc = subprocess.Popen(
                    ["Xvfb", f":{num}", "-screen", "0", "1280x800x24", "-ac"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(0.5)
                if proc.poll() is None:  # Still running — display is ready
                    self._xvfb_proc = proc
                    os.environ["DISPLAY"] = f":{num}"
                    self._log(f"[browser] Xvfb started on :{num} (non-headless mode)")
                    return True
                proc.terminate()
            except FileNotFoundError:
                return False  # Xvfb not installed; fall back to headless
        return False

    def _stop_browser(self) -> None:
        for attribute, method_name in (
            ("_context", "close"),
            ("_browser", "close"),
            ("_pw", "stop"),
        ):
            resource = getattr(self, attribute, None)
            setattr(self, attribute, None)
            if resource is None:
                continue
            try:
                getattr(resource, method_name)()
            except Exception as exc:
                self._log(f"[browser] cleanup warning for {attribute}: {exc}")

        xvfb = self._xvfb_proc
        self._xvfb_proc = None
        if xvfb is None:
            return
        os.environ.pop("DISPLAY", None)
        try:
            xvfb.terminate()
            xvfb.wait(timeout=5)
        except subprocess.TimeoutExpired:
            xvfb.kill()
            xvfb.wait(timeout=5)
        except Exception as exc:
            self._log(f"[browser] Xvfb cleanup warning: {exc}")

    def fetch_html_playwright(
        self,
        url: str,
        max_retries: int = 3,
        wait_for_selector: Optional[str] = None,
    ) -> str:
        self._start_browser()
        min_wait: float = getattr(self, "min_wait", 0.25)
        max_wait: float = getattr(self, "max_wait", 0.75)

        for attempt in range(1, max_retries + 1):
            time.sleep(random.uniform(min_wait, max_wait))
            page = self._context.new_page()
            try:
                page.goto(url, wait_until="networkidle", timeout=60_000)
                content = page.content()

                if "AwsWafIntegration" in content:
                    content = self._wait_waf_resolved(page)
                    if content is None:
                        self._log(f"[playwright] WAF challenge unresolved (attempt {attempt}/{max_retries})")
                        if attempt < max_retries:
                            self._log(f"[retry] attempt {attempt}/{max_retries}")
                        continue

                if wait_for_selector:
                    self._wait_selector_stable(page, wait_for_selector)
                    content = page.content()

                return content
            except Exception as exc:
                self._log(f"[playwright] [fail] {url}: {exc}")
                if attempt < max_retries:
                    self._log(f"[retry] attempt {attempt}/{max_retries}")
            finally:
                page.close()

        return ""

    def _wait_waf_resolved(self, page) -> Optional[str]:
        """Poll until the AWS WAF challenge auto-resolves.

        The non-headless browser completes the JS fingerprint challenge and the
        page reloads to the real content.  Returns the resolved HTML, or None
        if the challenge does not pass within 15 seconds.
        """
        self._log("[playwright] WAF challenge detected; waiting for auto-resolution")
        for _ in range(15):
            time.sleep(1)
            content = page.content()
            if "AwsWafIntegration" not in content and len(content) > 5_000:
                return content
        return None

    @staticmethod
    def _wait_selector_stable(page, selector: str) -> None:
        """Wait for ``selector`` to appear, then wait until its count stops growing.

        JS-rendered listing pages (e.g. lot cards) trickle elements into the
        DOM after networkidle fires.  Polling until the count is stable ensures
        the caller receives a fully-populated page rather than a partial render.
        """
        try:
            page.wait_for_selector(selector, timeout=15_000)
            prev_count = 0
            for _ in range(10):
                time.sleep(1)
                cur_count = page.evaluate(
                    f"document.querySelectorAll({selector!r}).length"
                )
                if cur_count == prev_count and cur_count > 0:
                    break
                prev_count = cur_count
        except Exception:
            pass  # Return whatever is already in the DOM
