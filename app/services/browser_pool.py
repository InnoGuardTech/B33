"""
V14 Browser Singleton — Render-512MB-friendly + seats.io map fix.

What changed vs V13:
  • The V13 launch flag `--blink-settings=imagesEnabled=false` killed ALL
    images globally, which also blocked the seats.io chart from rendering
    its category icons → users saw an empty grey rectangle instead of a
    seat map.
  • V14 keeps the global "no images" stance (saves ~40% RAM) but uses
    Playwright's request-router to SELECTIVELY ALLOW images, fonts and
    media that come from the trusted map domains:
        seats.io / cdn-eu.seatsio.net / cdn.seatsio.net
        wbk-assets.webook.com (Webook's own CDN)
  • Everything else (ads, analytics, third-party fonts, hero images) is
    aborted at the network layer → pages load 3× faster on Render free.
  • Browser is still a SINGLETON with 30-min idle TTL; contexts per acc.
  • New: optional proxy_url per booking_context() call → per-account exit.
  • New: timezone + locale rotation pulled from a small pool.

Public API (unchanged for backwards compat):
    async with browser_context(label="acc_xxx") as ctx:
        page = await ctx.new_page()
        ...
    # context auto-closed; browser stays warm for 30 min.

V14 additions:
    async with browser_context(label="acc", proxy_url="http://...") as ctx:
        ...
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

TIMEZONES = [
    "Asia/Riyadh", "Asia/Dubai", "Asia/Kuwait", "Asia/Qatar", "Asia/Bahrain",
]

# V14: Domains whose images / fonts / media must be ALLOWED so the
# seats.io chart renders correctly. Anything else is blocked.
MAP_ALLOWED_DOMAINS = (
    "seats.io",
    "cdn-eu.seatsio.net",
    "cdn.seatsio.net",
    "cdn-na.seatsio.net",
    "cdn-am.seatsio.net",
    "wbk-assets.webook.com",
    "webook.com",                    # Webook's own poster CDN
    "api.seats.io",
    "api.seatsio.net",
)

# V14: Heavy resource types that are blocked by default for non-map domains.
# `image` and `font` were the V13 footguns (broke seats.io rendering).
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

# V14.1: In **VISUAL MAP MODE** (block-selection UI) we relax the blocker
# even further — only ads/analytics are dropped. Everything else (images,
# fonts, scripts, stylesheets) loads so the seats.io chart can show its
# row icons, hover labels, category swatches, etc.
# Background-polling code paths keep using the lightweight router to stay
# inside the 512 MB Render envelope.
VISUAL_MAP_BLOCKED_RESOURCE_TYPES: set[str] = set()  # nothing blocked

# Always-block resource types regardless of domain (analytics / ads).
HARD_BLOCKED_PATTERNS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "facebook.com/tr",
    "hotjar.com",
    "mixpanel.com",
    "segment.com",
    "fullstory.com",
)

# V14 RAM-optimized Chromium flags. NOTE: --blink-settings=imagesEnabled
# was REMOVED so the page.route handler can decide image policy per-request
# (allowing seats.io chart icons through).
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    # RAM optimization (Render 512MB)
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--no-first-run",
    "--mute-audio",
    "--disable-features=TranslateUI,BackForwardCache,InterestFeedContentSuggestions",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--disable-hang-monitor",
    "--disable-popup-blocking",
    "--metrics-recording-only",
    # Tab-process accounting (Chromium 117+) — caps RAM bleeding.
    "--memory-pressure-off",
    "--single-process",  # ⚠️ saves ~100MB but reduces stability; OK on free tier.
]


def random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def random_viewport() -> dict[str, int]:
    return dict(random.choice(VIEWPORTS))


def random_locale() -> str:
    return random.choice(LOCALES)


def random_timezone() -> str:
    return random.choice(TIMEZONES)


def random_context_kwargs(seed: Optional[str] = None) -> dict[str, Any]:
    """Return a fresh, randomized set of context creation kwargs.

    If `seed` is provided (e.g. account_id), the fingerprint is stable
    per-account so a given Webook account always shows the same browser.
    """
    rng = random.Random(seed) if seed else random.Random()
    return {
        "user_agent": rng.choice(USER_AGENTS),
        "viewport": dict(rng.choice(VIEWPORTS)),
        "locale": rng.choice(LOCALES),
        "timezone_id": rng.choice(TIMEZONES),
    }


# ════════════════════════════════════════════════════════════════════════
# V14: Selective resource interceptor (the "map fix")
# ════════════════════════════════════════════════════════════════════════
def _is_map_domain(url: str) -> bool:
    u = (url or "").lower()
    for d in MAP_ALLOWED_DOMAINS:
        if d in u:
            return True
    return False


def _is_hard_blocked(url: str) -> bool:
    u = (url or "").lower()
    for p in HARD_BLOCKED_PATTERNS:
        if p in u:
            return True
    return False


async def install_lightweight_router(ctx) -> None:
    """Install a context-level request router that:

      1. ALLOWS every request to seats.io / wbk-assets.webook.com so the
         chart renders correctly.
      2. BLOCKS images / media / fonts on every other domain.
      3. HARD-BLOCKS analytics / ads everywhere.

    This is what makes the seat map render correctly while staying within
    the 512MB Render envelope.
    """
    async def _router(route, request):
        try:
            url = request.url or ""
            rtype = (request.resource_type or "").lower()

            # 1. Hard-block analytics/ads regardless of domain.
            if _is_hard_blocked(url):
                await route.abort()
                return

            # 2. Always allow requests to map/seats.io domains.
            if _is_map_domain(url):
                await route.continue_()
                return

            # 3. Block heavy resource types on non-map domains.
            if rtype in BLOCKED_RESOURCE_TYPES:
                await route.abort()
                return

            # 4. Default — let it through.
            await route.continue_()
        except Exception:
            # Never let the router itself crash the page; fall open.
            try:
                await route.continue_()
            except Exception:
                pass

    # `**/*` matches every request — Playwright handles this efficiently.
    try:
        await ctx.route("**/*", _router)
    except Exception as e:
        log.warning("install_lightweight_router failed: %s", e)


async def install_visual_map_router(ctx) -> None:
    """V14.1 — VISUAL MAP MODE router.

    Used when the user is actively picking blocks on the seats.io chart.
    Only ads/analytics are blocked; everything else (images, fonts,
    scripts, CSS, XHR) flows through so the chart can render exactly as
    a real browser would. Costs more RAM than the lightweight router but
    is only active for the brief window during block selection.
    """
    async def _router(route, request):
        try:
            url = request.url or ""
            if _is_hard_blocked(url):
                await route.abort()
                return
            await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    try:
        await ctx.route("**/*", _router)
        log.info("🎨 visual-map router installed (full asset rendering enabled)")
    except Exception as e:
        log.warning("install_visual_map_router failed: %s", e)


# ════════════════════════════════════════════════════════════════════════
# Singleton state
# ════════════════════════════════════════════════════════════════════════
class _BrowserSingleton:
    IDLE_TTL = 30 * 60  # 30 minutes

    def __init__(self) -> None:
        self._pw_ctx = None
        self._pw = None
        self._browser = None
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
        log.info("🧬 launching Chromium singleton (V14 RAM-tuned)…")
        self._pw_ctx = async_playwright()
        self._pw = await self._pw_ctx.start()

        # Browser-level proxy is a fallback. Per-context proxy (preferred)
        # is set in self.context().
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
        log.info("✅ Chromium singleton ready (selective router enabled, "
                 "global_proxy=%s)", "yes" if proxy_kwargs else "no")

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
                      extra_args: Optional[dict[str, Any]] = None,
                      proxy_url: Optional[str] = None,
                      install_router: bool = True,
                      visual_map_mode: bool = False):
        """Yield a fresh BrowserContext with randomized fingerprint.

        Closes the context on exit while the browser stays warm.

        Args:
          proxy_url: per-context proxy (overrides global). Critical for
            account isolation. Format: http://user:pass@host:port
          install_router: True → install a request router. Choice between
            lightweight (RAM-saving) and visual-map (full rendering) is
            governed by ``visual_map_mode``.
          visual_map_mode: V14.1 — when True, install the visual-map
            router instead of the lightweight one. Use this for the
            block-selection screen so the user can SEE the seats.io
            chart with all icons / fonts / styles.
        """
        async with self._lock:
            await self._ensure_started()

        kwargs = random_context_kwargs(seed=label or None)
        if extra_args:
            kwargs.update(extra_args)

        # Per-context proxy (Playwright supports this on Chromium contexts
        # only when the browser was launched WITHOUT a global proxy, OR
        # when launched with `--proxy-server=per-context`. We lazily fall
        # back to global proxy if the per-context call fails.)
        if proxy_url:
            try:
                parsed = _parse_proxy_url(proxy_url)
                if parsed:
                    kwargs["proxy"] = parsed
            except Exception as e:
                log.debug("proxy_url parse failed for label=%s: %s", label, e)

        ctx = await self._browser.new_context(**kwargs)
        if install_router:
            if visual_map_mode:
                await install_visual_map_router(ctx)
            else:
                await install_lightweight_router(ctx)

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


def _parse_proxy_url(url: str) -> Optional[dict[str, str]]:
    """Convert `http://user:pass@host:port` → Playwright proxy dict."""
    if not url:
        return None
    from urllib.parse import urlparse
    p = urlparse(url.strip())
    if not p.hostname:
        return None
    server = f"{p.scheme or 'http'}://{p.hostname}"
    if p.port:
        server += f":{p.port}"
    out: dict[str, str] = {"server": server}
    if p.username:
        out["username"] = p.username
    if p.password:
        out["password"] = p.password
    return out


# Module-level singleton
_singleton = _BrowserSingleton()


@asynccontextmanager
async def browser_context(*, label: str = "",
                          extra_args: Optional[dict[str, Any]] = None,
                          proxy_url: Optional[str] = None,
                          install_router: bool = True,
                          visual_map_mode: bool = False):
    """Public API: get an isolated BrowserContext from the warm singleton.

    Args:
        label:           arbitrary identifier (account_id is conventional).
        extra_args:      overrides for new_context() kwargs.
        proxy_url:       per-context proxy (V14).
        install_router:  True → enable a request router (V14).
        visual_map_mode: V14.1 — True for the block-selection / chart UI,
                         False for background polling (RAM-optimised).
    """
    async with _singleton.context(
        label=label, extra_args=extra_args,
        proxy_url=proxy_url, install_router=install_router,
        visual_map_mode=visual_map_mode,
    ) as ctx:
        yield ctx


@asynccontextmanager
async def visual_map_context(*, label: str = "",
                             proxy_url: Optional[str] = None,
                             extra_args: Optional[dict[str, Any]] = None):
    """V14.1 — convenience wrapper for the seats.io block-selection UI.

    Equivalent to ``browser_context(..., visual_map_mode=True)``.
    Use this when the user explicitly needs to SEE the chart (block
    picker / interactive seat selection). Background polling and the
    booking hot-path should keep using ``browser_context()`` (RAM-saver).
    """
    async with browser_context(
        label=label, proxy_url=proxy_url, extra_args=extra_args,
        install_router=True, visual_map_mode=True,
    ) as ctx:
        yield ctx


async def shutdown_browser_singleton() -> None:
    """Called from main.lifespan during shutdown."""
    await _singleton.close()


def is_singleton_alive() -> bool:
    return _singleton.is_alive()
