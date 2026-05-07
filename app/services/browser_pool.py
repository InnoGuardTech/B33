"""
V13 Browser Singleton — one Chromium process, contexts per account.

Why:
  • Render free tier: 512MB RAM hard cap. Each Playwright launch costs
    ~150-180MB; reusing a single browser saves a full launch (~3s) and
    a full process (~150MB) per booking.
  • Cookies / localStorage isolation is preserved by giving each booking
    its OWN BrowserContext (close-on-exit).
  • Idle-eviction: the singleton self-closes after 30 minutes of inactivity
    so RAM is reclaimed when the bot is quiet.
  • Stealth: low-RAM Chromium flags + UA / viewport / locale rotation.

Public API:
    async with browser_context(label="acc_xxx") as ctx:
        page = await ctx.new_page()
        ...
    # context auto-closed on exit; browser stays warm for 30 min.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from app.core.config import (
    HEADLESS,
    proxy_password, proxy_server, proxy_username,
    use_stealth_browser,
)

log = logging.getLogger("browser_pool")

# ════════════════════════════════════════════════════════════════════════
# Stealth-first Playwright import (mirrors booking_playwright.py)
# ════════════════════════════════════════════════════════════════════════
_pw_err: Optional[Exception] = None
try:
    if use_stealth_browser():
        from patchright.async_api import async_playwright  # type: ignore
    else:
        raise ImportError("stealth disabled")
except Exception:
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as _e:  # pragma: no cover
        _pw_err = _e
        async_playwright = None  # type: ignore


# ════════════════════════════════════════════════════════════════════════
# Stealth pools — UA / viewport / locale rotation
# ════════════════════════════════════════════════════════════════════════
USER_AGENTS = [
    # 10 real, recent desktop browsers (Q1 2025 distribution).
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) "
    "Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:131.0) "
    "Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]

VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1600, "height": 900},
    {"width": 1280, "height": 800},
    {"width": 1680, "height": 1050},
]

LOCALES = ["ar-SA", "en-US"]

# Default Render-friendly Chromium flags (low RAM, no GPU).
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    # V13 RAM optimizations
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--no-first-run",
    "--mute-audio",
    "--disable-features=TranslateUI",
    "--blink-settings=imagesEnabled=false",  # ⚡ ~40% RAM saved
    "--single-process",                       # ⚡ ~100MB saved
]


def random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def random_viewport() -> dict[str, int]:
    return dict(random.choice(VIEWPORTS))


def random_locale() -> str:
    return random.choice(LOCALES)


def random_context_kwargs() -> dict[str, Any]:
    """Return a fresh, randomized set of context creation kwargs."""
    return {
        "user_agent": random_user_agent(),
        "viewport": random_viewport(),
        "locale": random_locale(),
        "timezone_id": random.choice([
            "Asia/Riyadh", "Asia/Dubai", "Asia/Kuwait", "Asia/Qatar",
        ]),
    }


# ════════════════════════════════════════════════════════════════════════
# Singleton state
# ════════════════════════════════════════════════════════════════════════
class _BrowserSingleton:
    IDLE_TTL = 30 * 60  # 30 minutes

    def __init__(self) -> None:
        self._pw_ctx = None        # async_playwright() context manager
        self._pw = None            # underlying Playwright instance
        self._browser = None       # Chromium browser
        self._lock = asyncio.Lock()
        self._last_used = 0.0
        self._active_contexts = 0
        self._reaper_task: Optional[asyncio.Task] = None

    def is_alive(self) -> bool:
        return self._browser is not None

    async def _ensure_started(self) -> None:
        if self._browser is not None:
            return
        if async_playwright is None:
            raise RuntimeError(
                f"Playwright unavailable: {_pw_err}" if _pw_err
                else "Playwright not installed"
            )
        log.info("🧬 launching Chromium singleton…")
        self._pw_ctx = async_playwright()
        self._pw = await self._pw_ctx.start()

        proxy_kwargs: dict[str, Any] = {}
        ps = (proxy_server() or "").strip()
        if ps:
            proxy_kwargs["proxy"] = {
                "server": ps,
                **({"username": proxy_username().strip()}
                   if proxy_username().strip() else {}),
                **({"password": proxy_password().strip()}
                   if proxy_password().strip() else {}),
            }

        self._browser = await self._pw.chromium.launch(
            headless=HEADLESS,
            args=list(LAUNCH_ARGS),
            **proxy_kwargs,
        )
        self._last_used = time.time()
        log.info("✅ Chromium singleton ready (low-RAM flags + proxy=%s)",
                 "yes" if proxy_kwargs else "no")

        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(
                self._idle_reaper(), name="browser-idle-reaper",
            )

    async def _idle_reaper(self) -> None:
        """Close the browser when idle for IDLE_TTL seconds."""
        try:
            while True:
                await asyncio.sleep(60)
                if self._browser is None:
                    continue
                if self._active_contexts > 0:
                    continue
                idle = time.time() - self._last_used
                if idle >= self.IDLE_TTL:
                    log.info("💤 closing idle Chromium singleton (idle=%ds)",
                             int(idle))
                    await self.close()
        except asyncio.CancelledError:
            return

    @asynccontextmanager
    async def context(self, *, label: str = "",
                      extra_args: Optional[dict[str, Any]] = None):
        """Yield a fresh BrowserContext with randomized fingerprint.

        Closes the context on exit while the browser stays warm.
        """
        async with self._lock:
            await self._ensure_started()

        kwargs = random_context_kwargs()
        if extra_args:
            kwargs.update(extra_args)
        ctx = await self._browser.new_context(**kwargs)
        self._active_contexts += 1
        self._last_used = time.time()
        try:
            yield ctx
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
            self._active_contexts = max(0, self._active_contexts - 1)
            self._last_used = time.time()

    async def close(self) -> None:
        """Hard close — releases all RAM. Safe to call multiple times."""
        b = self._browser
        pw_ctx = self._pw_ctx
        self._browser = None
        self._pw = None
        self._pw_ctx = None
        if b is not None:
            try:
                await b.close()
            except Exception:
                pass
        if pw_ctx is not None:
            try:
                await pw_ctx.__aexit__(None, None, None)
            except Exception:
                pass
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except (asyncio.CancelledError, Exception):
                pass
        self._reaper_task = None
        self._active_contexts = 0


# Module-level singleton
_singleton = _BrowserSingleton()


@asynccontextmanager
async def browser_context(*, label: str = "",
                          extra_args: Optional[dict[str, Any]] = None):
    """Public API: get an isolated BrowserContext from the warm singleton."""
    async with _singleton.context(label=label, extra_args=extra_args) as ctx:
        yield ctx


async def shutdown_browser_singleton() -> None:
    """Called from main.lifespan during shutdown."""
    await _singleton.close()


def is_singleton_alive() -> bool:
    return _singleton.is_alive()
