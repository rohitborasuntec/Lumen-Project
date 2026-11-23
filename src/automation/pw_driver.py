#!/usr/bin/env python3
"""
browser_manager.py  —  Patchright port
--------------------------------------
Async browser manager with persistent-profile support. Mirrors the
original Selenium/undetected-chromedriver surface (launch, quit, human_*)
so downstream call sites need minimal changes.

Browser strategy
----------------
Chrome / Brave / Edge : Patchright `chromium` channel with explicit
                        executable_path. Persistent profile per browser.
Opera                 : Patchright `chromium` + Opera executable_path.
                        Opera's CDP support is patchy — this works on
                        most builds but isn't guaranteed.
Firefox               : Stock Playwright Firefox (Patchright's stealth is
                        Chromium-only). -private == incognito=True.

Incognito mode
--------------
Pass incognito=True to Browser(). We skip user_data_dir entirely so
Playwright creates a throwaway context. (Passing --incognito via args
*and* a persistent profile silently ignores --incognito — same trap
the Selenium version documented.)

Performance notes
-----------------
- Patchright launches one browser per Browser instance. Reuse the
  instance; don't create one per navigation.
- `launch_persistent_context` closes the previous profile's browser
  if one is still running against the same user_data_dir.
"""

from __future__ import annotations

import asyncio
import platform
import random
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from patchright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

try:
    from src.logging import logger
except Exception:  # fallback for standalone use
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("browser_manager")


IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
IS_MAC = platform.system() == "Darwin"

# Kept for API compatibility with the Selenium version. Not applied by
# default — Patchright already neutralizes these vectors, and overriding
# them ourselves would re-introduce a fingerprint. Opt in per-page with
# `await page.add_init_script(STEALTH_JS)` if you need extra coverage.
STEALTH_JS = r"""
(() => {
    try { Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined, configurable: true }); } catch (_) {}
    try { Object.defineProperty(Navigator.prototype, 'languages', { get: () => ['en-US', 'en'], configurable: true }); } catch (_) {}
    try { if (!window.chrome) window.chrome = {}; if (!window.chrome.runtime) window.chrome.runtime = {}; } catch (_) {}
})();
"""


class Browser:
    """
    Example:
        b = Browser(headless=False)
        ctx, page, name = await b.launch("Chrome")
        await page.goto("https://example.com")
        await b.quit()
    """

    SUPPORTED = ["Chrome", "Brave", "Opera", "Edge", "Firefox"]

    _WINDOW_SIZES = [(1920, 1080)]

    # Kept for API compat with the Selenium version. No-op in the port.
    FIREFOX_INSTALL_DIR: Optional[str] = None

    def __init__(
        self,
        headless: bool = False,
        user_agent: Optional[str] = None,
        window_size: Optional[tuple[int, int]] = None,
        user_data_dir: Optional[str] = None,
        use_undetected_firefox: bool = True,    # accepted, unused
        firefox_install_dir: Optional[str] = None,  # accepted, unused
        post_launch_delay: tuple[float, float] = (0, 0),
        done_browser: Optional[list[str]] = None,
        incognito: bool = False,
        locale: str = "en-US",
        timezone_id: str = "America/New_York",
        block_media: bool = False,
    ):
        if done_browser is None:
            done_browser = []

        self.SUPPORTED = [b for b in self.SUPPORTED if b not in done_browser]
        self.headless = headless
        self.user_agent = user_agent
        self.window_size = window_size or random.choice(self._WINDOW_SIZES)
        self.post_launch_delay = post_launch_delay
        self.incognito = incognito
        self.locale = locale
        self.timezone_id = timezone_id
        self.block_media = block_media

        base = Path(user_data_dir or (Path.home() / ".browser_profiles"))
        base.mkdir(parents=True, exist_ok=True)
        self.profile_root = base

        # Runtime state
        self._pw: Optional[Playwright] = None
        self._pw_cm = None              # the async_playwright() context manager
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.browser_name: Optional[str] = None

        if firefox_install_dir:
            type(self).FIREFOX_INSTALL_DIR = firefox_install_dir

    # ---------------- public entry ---------------- #

    async def launch(self, browser_name: Optional[str] = None):
        """
        Returns (context, page, name). Caller is responsible for driving
        the page. Call `await b.quit()` when done.
        """
        requested = browser_name or random.choice(self.SUPPORTED)
        key = requested.strip().lower()

        launchers = {
            "chrome":  self._launch_chrome,
            "brave":   self._launch_brave,
            "edge":    self._launch_edge,
            "opera":   self._launch_opera,
            "firefox": self._launch_firefox,
        }
        if key not in launchers:
            raise ValueError(
                f"Unsupported browser '{browser_name}'. "
                f"Choose from {list(launchers.keys())}"
            )

        self.browser_name = key
        logger.info(f"[Browser] Starting {requested}...")

        if self._pw is None:
            self._pw_cm = async_playwright()
            self._pw = await self._pw_cm.__aenter__()

        try:
            self.context, self.page = await launchers[key]()
        except Exception as exc:
            logger.error(f"[Browser] Failed to start {requested}: {exc!r}")
            await self.quit()
            raise

        if self.block_media:
            await self.context.route(
                "**/*.{mp4,webm,ogg,mp3,wav,woff,woff2,ttf,eot}",
                lambda route: route.abort(),
            )

        logger.info(f"[Browser] {requested} ready.")
        return self.context, self.page, requested

    # ---------------- helpers ---------------- #

    def _profile(self, browser: str) -> Optional[str]:
        """Return a profile dir, or None if incognito."""
        if self.incognito:
            return None
        path = self.profile_root / browser.lower()
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _common_launch_kwargs(self) -> dict:
        kwargs = {
            "headless": self.headless,
            "locale": self.locale,
            "timezone_id": self.timezone_id,
            "no_viewport": True,
        }
        if self.user_agent:
            kwargs["user_agent"] = self.user_agent
        if self.window_size:
            w, h = self.window_size
            kwargs["viewport"] = {"width": w, "height": h}
            kwargs["no_viewport"] = False
        return kwargs

    @staticmethod
    def _which(*names: str) -> Optional[str]:
        for name in names:
            path = shutil.which(name)
            if path:
                return path
        return None

    @staticmethod
    def _find_existing(paths: list[str]) -> Optional[str]:
        import os
        for item in paths:
            if not item:
                continue
            expanded = os.path.expandvars(item)
            if Path(expanded).is_file():
                return expanded
        return None

    # ---------------- browser binaries ---------------- #

    def _chrome_binary(self) -> Optional[str]:
        import os
        if IS_WINDOWS:
            return self._find_existing([
                os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            ])
        if IS_MAC:
            return self._find_existing([
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ])
        return self._which("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")

    def _brave_binary(self) -> Optional[str]:
        import os
        if IS_WINDOWS:
            return self._find_existing([
                os.path.expandvars(r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe"),
                os.path.expandvars(r"%PROGRAMFILES(X86)%\BraveSoftware\Brave-Browser\Application\brave.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            ])
        if IS_MAC:
            return self._find_existing([
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            ])
        return self._which("brave-browser", "brave-browser-stable", "brave")

    def _opera_binary(self) -> Optional[str]:
        import os
        if IS_WINDOWS:
            return self._find_existing([
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera\opera.exe"),
                os.path.expandvars(r"%PROGRAMFILES%\Opera\opera.exe"),
                os.path.expandvars(r"%PROGRAMFILES(X86)%\Opera\opera.exe"),
            ])
        if IS_MAC:
            return self._find_existing(["/Applications/Opera.app/Contents/MacOS/Opera"])
        return self._which("opera", "opera-stable")

    def _edge_binary(self) -> Optional[str]:
        import os
        if IS_WINDOWS:
            return self._find_existing([
                os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
            ])
        if IS_MAC:
            return self._find_existing([
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ])
        return self._which("microsoft-edge", "microsoft-edge-stable")

    def _firefox_binary(self) -> Optional[str]:
        import os
        if IS_WINDOWS:
            for c in [
                os.path.expandvars(r"%PROGRAMFILES%\Mozilla Firefox"),
                os.path.expandvars(r"%PROGRAMFILES(X86)%\Mozilla Firefox"),
                os.path.expandvars(r"%LOCALAPPDATA%\Mozilla Firefox"),
            ]:
                exe = os.path.join(c, "firefox.exe")
                if os.path.isfile(exe):
                    return exe
        elif IS_MAC:
            p = "/Applications/Firefox.app/Contents/MacOS/firefox"
            if os.path.isfile(p):
                return p
        return self._which("firefox")

    # ---------------- launchers ---------------- #

    async def _launch_chromium_persistent(
        self, *, browser_key: str, executable_path: str,
        extra_args: Optional[list[str]] = None,
    ) -> tuple[BrowserContext, Page]:
        """
        Shared launcher for all Chromium-family browsers. Uses
        `channel="chrome"` so Patchright's bundled Chromium patches apply,
        plus `executable_path` to point at the real binary (Brave/Edge/Opera).
        """
        kwargs = self._common_launch_kwargs()
        kwargs["channel"] = "chrome"
        kwargs["executable_path"] = executable_path
        if extra_args:
            kwargs["args"] = extra_args
        # Explicitly drop 'viewport' when no_viewport is set — Playwright
        # rejects both together.
        if kwargs.get("no_viewport") and "viewport" in kwargs:
            kwargs.pop("viewport", None)

        profile = self._profile(browser_key)
        if profile:
            ctx = await self._pw.chromium.launch_persistent_context(
                user_data_dir=profile, **kwargs,
            )
        else:
            browser = await self._pw.chromium.launch(**{
                k: v for k, v in kwargs.items() if k not in ("channel", "executable_path")
            })
            ctx = await browser.new_context(**{
                k: v for k, v in kwargs.items()
                if k in ("user_agent", "locale", "timezone_id", "viewport")
            })

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        return ctx, page

    async def _launch_chrome(self) -> tuple[BrowserContext, Page]:
        binary = self._chrome_binary()
        if not binary:
            raise FileNotFoundError("Chrome was not found.")
        return await self._launch_chromium_persistent(
            browser_key="chrome", executable_path=binary,
        )

    async def _launch_brave(self) -> tuple[BrowserContext, Page]:
        binary = self._brave_binary()
        if not binary:
            raise FileNotFoundError("Brave was not found.")
        return await self._launch_chromium_persistent(
            browser_key="brave", executable_path=binary,
        )

    async def _launch_edge(self) -> tuple[BrowserContext, Page]:
        binary = self._edge_binary()
        if not binary:
            raise FileNotFoundError("Edge was not found.")
        return await self._launch_chromium_persistent(
            browser_key="edge", executable_path=binary,
        )

    async def _launch_opera(self) -> tuple[BrowserContext, Page]:
        binary = self._opera_binary()
        if not binary:
            raise FileNotFoundError("Opera was not found.")
        # Opera is Chromium-based but isn't a first-class Playwright target.
        # This often works; when it doesn't, nothing Patchright can do.
        return await self._launch_chromium_persistent(
            browser_key="opera", executable_path=binary,
            extra_args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

    async def _launch_firefox(self) -> tuple[BrowserContext, Page]:
        binary = self._firefox_binary()
        if not binary:
            raise FileNotFoundError(
                "Firefox was not found. Install Firefox or pass "
                "firefox_install_dir= to Browser()."
            )
        kwargs = self._common_launch_kwargs()
        if kwargs.get("no_viewport"):
            kwargs.pop("viewport", None)

        profile = self._profile("firefox")
        if profile:
            ctx = await self._pw.firefox.launch_persistent_context(
                user_data_dir=profile, executable_path=binary, **kwargs,
            )
        else:
            browser = await self._pw.firefox.launch(executable_path=binary)
            ctx = await browser.new_context(**{
                k: v for k, v in kwargs.items()
                if k in ("user_agent", "locale", "timezone_id", "viewport")
            })

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        return ctx, page

    # ---------------- human helpers (async) ---------------- #

    @staticmethod
    async def human_type(page: Page, css_selector: str, text: str, wpm: int = 220):
        el = await page.wait_for_selector(css_selector)
        await el.click()
        await asyncio.sleep(random.uniform(0.2, 0.6))
        for char in text:
            await el.type(char, delay=random.randint(30, 100))
        await asyncio.sleep(random.uniform(0.1, 0.4))

    @staticmethod
    async def human_scroll(page: Page, steps: int = 5):
        for _ in range(steps):
            delta = random.randint(200, 700)
            await page.evaluate(
                "(d) => window.scrollBy({top: d, behavior: 'smooth'})", delta,
            )
            await asyncio.sleep(random.uniform(0.3, 1.0))

    @staticmethod
    async def human_mouse_wander(page: Page, moves: int = 4):
        try:
            size = page.viewport_size or {"width": 1280, "height": 720}
            for _ in range(moves):
                await page.mouse.move(
                    random.randint(10, max(20, size["width"] - 30)),
                    random.randint(10, max(20, size["height"] - 30)),
                )
                await asyncio.sleep(random.uniform(0.2, 0.7))
        except Exception:
            pass

    # ---------------- cleanup ---------------- #

    async def quit(self):
        if self.context is not None:
            try:
                await self.context.close()
                logger.info("Browser context closed.")
            except Exception as exc:
                logger.error(f"context.close() error: {exc!r}")
            finally:
                self.context = None
                self.page = None

        if self._pw_cm is not None:
            try:
                await self._pw_cm.__aexit__(None, None, None)
            except Exception as exc:
                logger.error(f"playwright shutdown error: {exc!r}")
            finally:
                self._pw = None
                self._pw_cm = None

        if self.browser_name == "brave":
            self._kill_by_name("brave.exe" if IS_WINDOWS else "brave")

    @staticmethod
    def _kill_by_name(image_name: str) -> None:
        import subprocess
        try:
            if IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/F", "/IM", image_name],
                    check=False, capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                subprocess.run(["pkill", "-x", image_name], check=False, capture_output=True)
            logger.info(f"Killed leftover {image_name} processes.")
        except Exception as exc:
            logger.warning(f"kill {image_name} error: {exc}")


# ---------------- compatibility helpers ---------------- #

@asynccontextmanager
async def create_browser_context(
    user_data_dir: str | Path,
    *,
    headless: bool = False,
    block_media: bool = False,
):
    """Yield a persistent Chrome context and page for scraper callers."""
    browser = Browser(
        headless=headless,
        user_data_dir=str(user_data_dir),
        block_media=block_media,
    )
    try:
        context, page, _ = await browser.launch("Chrome")
        yield context, page
    finally:
        await browser.quit()


async def safe_goto(
    page: Page,
    url: str,
    *,
    timeout: int = 60_000,
    cf_timeout_sec: float = 90,
    log_fn=None,
) -> bool:
    """Navigate to a URL and wait briefly for a Cloudflare interstitial to clear."""
    response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    if response is not None and response.status >= 400:
        return False

    deadline = asyncio.get_running_loop().time() + cf_timeout_sec
    challenge_script = """() => {
        const title = document.title.toLowerCase();
        const body = (document.body?.innerText || '').toLowerCase();
        return title.includes('just a moment') ||
            title.includes('attention required') ||
            Boolean(document.querySelector(
                '#challenge-form, #cf-challenge-running, iframe[src*="challenges.cloudflare.com"]'
            )) ||
            body.includes('verify you are human') ||
            body.includes('checking your browser');
    }"""

    while await page.evaluate(challenge_script):
        if asyncio.get_running_loop().time() >= deadline:
            if log_fn:
                log_fn(f"Cloudflare challenge did not clear for {url}")
            return False
        await asyncio.sleep(1)

    return True


# ---------------- demo ---------------- #

async def _main():
    b = Browser(headless=False)
    try:
        ctx, page, name = await b.launch("Brave")
        await page.goto("https://www.google.com", wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(1.5, 3.0))
        await b.human_mouse_wander(page, moves=3)
        selector = "textarea[name=q], input[name=q]"
        await b.human_type(page, selector, "python tutorial")
        await page.keyboard.press("Enter")
        await asyncio.sleep(random.uniform(2.0, 4.0))
        logger.info(f"Browser: {name}\nTitle: {await page.title()}")
    finally:
        await b.quit()


if __name__ == "__main__":
    asyncio.run(_main())