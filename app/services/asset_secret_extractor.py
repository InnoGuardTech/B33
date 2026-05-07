"""
V14 — Dynamic secret extractor (bot00 DNA reimplemented in Python).

bot00 (Go) reverse-engineering revealed that the most valuable trick is
NOT TLS-impersonation — it is the live extraction of 5 secrets baked
into Webook's Vite JS bundle:

    VITE_PUBLIC_TICKETS_API_TOKEN     → 64-char hex   (Webook bearer)
    VITE_PUBLIC_SEATIO_WORKSPACE_KEY  → 36-char UUID  (seats.io workspace)
    VITE_PUBLIC_SEATCLOUD_WORKSPACE_KEY → 24-char hex (seatcloud workspace)
    VITE_PUBLIC_CAPTCHA_KEY           → 6L… 39-char  (Turnstile sitekey)
    Universal token / chart token     → 64-char hex   (seats.io signature)

Why this matters:
  • Tokens rotate without notice — hardcoded env vars eventually break.
  • Re-fetched on a 1-hour TTL so a token rotation self-heals.
  • Single shared async-cached coroutine: no thundering-herd on cold start.

Public API:
    secrets = await get_webook_secrets()        # cached 1h
    secrets = await get_webook_secrets(force_refresh=True)
    sk      = await get_secret("captcha_sitekey")

All extraction is best-effort and fails open (returns the previously
cached value, or empty strings) so a Webook outage cannot crash the bot.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

log = logging.getLogger("asset_secrets")

# ════════════════════════════════════════════════════════════════════════
# Constants — derived from bot00 binary analysis (`strings bot.exe`)
# ════════════════════════════════════════════════════════════════════════
WEBOOK_HOMEPAGE = "https://webook.com/en"
WEBOOK_ASSETS_HOST = "https://wbk-assets.webook.com"

# bot00 hard-codes a fallback bundle URL — we use it ONLY when the live
# discovery from the homepage fails.
FALLBACK_BUNDLE_URL = (
    f"{WEBOOK_ASSETS_HOST}/0.3.301/assets/index-r20cPaeH.js"
)

# Bundle-discovery regex — matches any /assets/index-*.js referenced by
# Webook's homepage HTML (or any /<version>/ asset path).
ASSET_BUNDLE_RE = re.compile(
    r'https?://wbk-assets\.webook\.com/[^"\'\s]+?/assets/index-[A-Za-z0-9_-]+\.js',
    re.I,
)

# ════════════════════════════════════════════════════════════════════════
# Secret-extraction regexes — copied verbatim from bot00 binary strings.
# Each pattern returns ONE capture group with the secret value.
# ════════════════════════════════════════════════════════════════════════
SECRET_PATTERNS: dict[str, list[re.Pattern]] = {
    "tickets_api_token": [
        re.compile(
            r'["\']VITE_PUBLIC_TICKETS_API_TOKEN["\']\s*:\s*["\']([^"\']+)["\']'
        ),
        re.compile(
            r'VITE_PUBLIC_TICKETS_API_TOKEN[^"\']*["\']([a-fA-F0-9]{64})["\']'
        ),
    ],
    "seatio_workspace_key": [
        re.compile(
            r'["\']VITE_PUBLIC_SEATIO_WORKSPACE_KEY["\']\s*:\s*["\']([^"\']+)["\']'
        ),
        re.compile(
            r'VITE_PUBLIC_SEATIO_WORKSPACE_KEY[^"\']*["\']([a-fA-F0-9\-]{36})["\']'
        ),
    ],
    "seatcloud_workspace_key": [
        re.compile(
            r'["\']VITE_PUBLIC_SEATCLOUD_WORKSPACE_KEY["\']\s*:\s*["\']([^"\']+)["\']'
        ),
        re.compile(
            r'VITE_PUBLIC_SEATCLOUD_WORKSPACE_KEY[^"\']*["\']([a-fA-F0-9]{24})["\']'
        ),
    ],
    "captcha_sitekey": [
        re.compile(
            r'["\']VITE_PUBLIC_CAPTCHA_KEY["\']\s*:\s*["\']([^"\']+)["\']'
        ),
        re.compile(
            r'VITE_PUBLIC_CAPTCHA_KEY[^"\']*["\'](6[A-Za-z0-9_\-]{39})["\']'
        ),
    ],
    "chart_token": [
        # bot00 calls this "Universal Token" / "Chart Token". 64 hex chars,
        # used as seats.io signature. Format: a quoted 64-char hex blob
        # appearing near "ChartToken" or as a plain literal.
        re.compile(r'["\']([a-fA-F0-9]{64})["\']'),
    ],
}

# Headers mimicking a real browser (kept minimal so any TLS path works).
_DEFAULT_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9,ar;q=0.8",
    "accept-encoding": "gzip, deflate, br",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
}


# ════════════════════════════════════════════════════════════════════════
# Public dataclass returned to callers
# ════════════════════════════════════════════════════════════════════════
@dataclass
class WebookSecrets:
    tickets_api_token: str = ""
    seatio_workspace_key: str = ""
    seatcloud_workspace_key: str = ""
    captcha_sitekey: str = ""
    chart_token: str = ""
    bundle_url: str = ""
    fetched_at: float = 0.0
    source: str = "uninitialised"  # "live" | "fallback" | "cache" | "stale"

    def is_complete(self) -> bool:
        """True when at least the 3 mission-critical secrets are present."""
        return bool(
            self.tickets_api_token
            and self.seatio_workspace_key
            and self.captcha_sitekey
        )

    def as_dict(self) -> dict:
        return {
            "tickets_api_token": self.tickets_api_token,
            "seatio_workspace_key": self.seatio_workspace_key,
            "seatcloud_workspace_key": self.seatcloud_workspace_key,
            "captcha_sitekey": self.captcha_sitekey,
            "chart_token": self.chart_token,
            "bundle_url": self.bundle_url,
            "fetched_at": self.fetched_at,
            "source": self.source,
        }


# ════════════════════════════════════════════════════════════════════════
# Cache state — single-flight semantics so concurrent callers share work
# ════════════════════════════════════════════════════════════════════════
_CACHE_TTL = 3600.0  # 1 hour, per requirements
_cache: Optional[WebookSecrets] = None
_cache_lock = asyncio.Lock()
_inflight: Optional[asyncio.Future] = None


def _redact(value: str, keep: int = 4) -> str:
    """Show only the prefix of a secret in logs."""
    if not value:
        return "<empty>"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]} (len={len(value)})"


# ════════════════════════════════════════════════════════════════════════
# HTTP helpers
# ════════════════════════════════════════════════════════════════════════
async def _fetch_text(session: aiohttp.ClientSession, url: str,
                       timeout: int = 15) -> Optional[str]:
    try:
        async with session.get(
            url, headers=_DEFAULT_HEADERS,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            if r.status != 200:
                log.debug("fetch %s -> HTTP %d", url, r.status)
                return None
            return await r.text()
    except Exception as e:
        log.debug("fetch %s err: %s", url, e)
        return None


async def _discover_bundle_url(session: aiohttp.ClientSession) -> Optional[str]:
    """Scan webook.com/en HTML for the live JS bundle URL."""
    html = await _fetch_text(session, WEBOOK_HOMEPAGE, timeout=15)
    if not html:
        return None
    matches = ASSET_BUNDLE_RE.findall(html)
    if not matches:
        return None
    # Prefer index-*.js (main bundle) over chunk-*.js
    for u in matches:
        if "/assets/index-" in u:
            return u
    return matches[0]


# ════════════════════════════════════════════════════════════════════════
# Core extraction
# ════════════════════════════════════════════════════════════════════════
def _extract_first(patterns: list[re.Pattern], text: str) -> str:
    for p in patterns:
        m = p.search(text)
        if m:
            try:
                return m.group(1).strip()
            except IndexError:
                continue
    return ""


def _parse_secrets(js_text: str, bundle_url: str) -> WebookSecrets:
    out = WebookSecrets(bundle_url=bundle_url, fetched_at=time.time())
    out.tickets_api_token = _extract_first(
        SECRET_PATTERNS["tickets_api_token"], js_text
    )
    out.seatio_workspace_key = _extract_first(
        SECRET_PATTERNS["seatio_workspace_key"], js_text
    )
    out.seatcloud_workspace_key = _extract_first(
        SECRET_PATTERNS["seatcloud_workspace_key"], js_text
    )
    out.captcha_sitekey = _extract_first(
        SECRET_PATTERNS["captcha_sitekey"], js_text
    )

    # chart_token: pick the FIRST 64-hex literal that is NOT the
    # tickets_api_token (which is also 64-hex). bot00 uses a more loose
    # heuristic; we keep it conservative.
    candidates = SECRET_PATTERNS["chart_token"][0].findall(js_text)
    for c in candidates:
        if c and c.lower() != (out.tickets_api_token or "").lower():
            out.chart_token = c
            break

    return out


async def _do_fetch() -> WebookSecrets:
    """Single network round: discover bundle URL → fetch JS → extract."""
    timeout = aiohttp.ClientTimeout(total=25, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        bundle_url = await _discover_bundle_url(session)
        source = "live"
        if not bundle_url:
            log.warning("bundle URL discovery failed — using fallback")
            bundle_url = FALLBACK_BUNDLE_URL
            source = "fallback"

        js_text = await _fetch_text(session, bundle_url, timeout=20)
        if not js_text and source == "live":
            # try the hardcoded fallback as a second chance
            log.warning("live bundle fetch empty — retrying fallback URL")
            bundle_url = FALLBACK_BUNDLE_URL
            source = "fallback"
            js_text = await _fetch_text(session, bundle_url, timeout=20)

        if not js_text:
            raise RuntimeError("could not download Webook JS bundle")

        secrets = _parse_secrets(js_text, bundle_url)
        secrets.source = source
        log.info(
            "🔑 secrets extracted (source=%s) "
            "| tickets=%s seatio=%s seatcloud=%s captcha=%s chart=%s",
            source,
            _redact(secrets.tickets_api_token),
            _redact(secrets.seatio_workspace_key),
            _redact(secrets.seatcloud_workspace_key),
            _redact(secrets.captcha_sitekey),
            _redact(secrets.chart_token),
        )
        return secrets


# ════════════════════════════════════════════════════════════════════════
# Public API — cached + single-flight
# ════════════════════════════════════════════════════════════════════════
async def get_webook_secrets(*, force_refresh: bool = False
                              ) -> WebookSecrets:
    """Return cached WebookSecrets (TTL 1h), refreshing when stale.

    Single-flight: if N coroutines call this at once on a cold cache,
    only ONE network roundtrip fires; the rest await the same future.
    """
    global _cache, _inflight
    now = time.time()

    if (not force_refresh) and _cache and (now - _cache.fetched_at) < _CACHE_TTL:
        cached = _cache
        # Return a copy with source=cache for observability
        out = WebookSecrets(**cached.as_dict())
        out.source = "cache"
        return out

    async with _cache_lock:
        # Double-check after acquiring the lock
        if (not force_refresh) and _cache and (time.time() - _cache.fetched_at) < _CACHE_TTL:
            out = WebookSecrets(**_cache.as_dict())
            out.source = "cache"
            return out

        if _inflight is not None and not _inflight.done():
            inflight = _inflight
        else:
            loop = asyncio.get_event_loop()
            _inflight = loop.create_future()
            inflight = None  # we are the leader

    if inflight is not None:
        try:
            return await inflight
        except Exception:
            # Leader failed; fall through and try once ourselves.
            pass

    try:
        secrets = await _do_fetch()
    except Exception as e:
        log.error("dynamic secret extraction failed: %s", e)
        # Fail-open: return last-known cache marked stale, or empty.
        if _cache:
            stale = WebookSecrets(**_cache.as_dict())
            stale.source = "stale"
            async with _cache_lock:
                if _inflight and not _inflight.done():
                    _inflight.set_result(stale)
                _inflight = None
            return stale
        empty = WebookSecrets(source="empty", fetched_at=time.time())
        async with _cache_lock:
            if _inflight and not _inflight.done():
                _inflight.set_result(empty)
            _inflight = None
        return empty

    async with _cache_lock:
        _cache = secrets
        if _inflight and not _inflight.done():
            _inflight.set_result(secrets)
        _inflight = None
    return secrets


async def get_secret(name: str, *, force_refresh: bool = False) -> str:
    """Convenience accessor for a single secret by name."""
    s = await get_webook_secrets(force_refresh=force_refresh)
    return getattr(s, name, "") or ""


def invalidate_cache() -> None:
    """Force the next call to refetch (e.g. after a 401 from Webook)."""
    global _cache
    _cache = None


# ════════════════════════════════════════════════════════════════════════
# Self-test block (per V14 requirement)
# ════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":  # pragma: no cover
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    async def _selftest() -> int:
        print("🧪 Hydra V14 — asset_secret_extractor self-test")
        print("=" * 70)
        t0 = time.time()
        secrets = await get_webook_secrets()
        elapsed = (time.time() - t0) * 1000
        print(f"\n  Fetched in {elapsed:.0f} ms (source={secrets.source})")
        print(f"  Bundle:           {secrets.bundle_url}")
        print(f"  tickets_api:      {_redact(secrets.tickets_api_token)}")
        print(f"  seatio_ws:        {_redact(secrets.seatio_workspace_key)}")
        print(f"  seatcloud_ws:     {_redact(secrets.seatcloud_workspace_key)}")
        print(f"  captcha_sitekey:  {_redact(secrets.captcha_sitekey)}")
        print(f"  chart_token:      {_redact(secrets.chart_token)}")
        print(f"  is_complete:      {secrets.is_complete()}")

        # Second call must hit cache
        t0 = time.time()
        again = await get_webook_secrets()
        elapsed = (time.time() - t0) * 1000
        print(f"\n  Second call: {elapsed:.2f} ms (source={again.source})")
        assert again.source == "cache", "expected cache hit on 2nd call"

        # force_refresh must bypass cache
        t0 = time.time()
        forced = await get_webook_secrets(force_refresh=True)
        elapsed = (time.time() - t0) * 1000
        print(f"  Forced refresh: {elapsed:.0f} ms (source={forced.source})")

        return 0 if secrets.is_complete() else 1

    sys.exit(asyncio.run(_selftest()))
