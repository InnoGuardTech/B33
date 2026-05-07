"""
V14 — StealthClient: HTTP/2-native, fingerprint-rotated, proxy-per-account.

Inspired by bot00's pingy-http (Go) wrapper but reimplemented on top of
httpx[http2] for Python. Key properties:

  • HTTP/2 always-on (h2 ALPN negotiation handled by httpx).
  • User-Agent / sec-ch-ua / Accept-Language / Sec-Fetch headers rotated
    from a pool of 10 real, recent desktop browsers.
  • Optional `proxy_url` parameter — passes through to httpx.AsyncClient
    so each booking account can run behind its OWN exit IP.
  • Connection pooling per-instance: limits=20 / 8 per host (safe for
    Render free tier).
  • Privacy-preserving error logs (no body, no headers value leakage).
  • Drop-in replacement for the `aiohttp.ClientSession` calls used in
    booking_http.py — same `request(method, url, …)` shape.

Public API:
    async with StealthClient(proxy_url=acc.proxy_url) as cli:
        status, body = await cli.get_json(url, headers=...)
        status, body = await cli.post_json(url, headers=..., json=...)
        text = await cli.get_text(url, headers=...)

The class can also be used as a long-lived singleton (without `async with`)
for the shared meta-fetch path; just remember to call `await client.close()`
during shutdown.
"""
from __future__ import annotations

import asyncio
import logging
import random
import secrets as _secrets
from typing import Any, Optional

import httpx

log = logging.getLogger("stealth_client")


# ════════════════════════════════════════════════════════════════════════
# Fingerprint pools — 10 real, recent (Q1 2025) desktop browsers
# ════════════════════════════════════════════════════════════════════════
USER_AGENTS: tuple[str, ...] = (
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
)

# sec-ch-ua client-hints aligned with each Chromium/Edge UA above.
SEC_CH_UA_VARIANTS: tuple[str, ...] = (
    '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    '"Google Chrome";v="130", "Chromium";v="130", "Not_A Brand";v="24"',
    '"Google Chrome";v="129", "Chromium";v="129", "Not_A Brand";v="24"',
    '"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
)

ACCEPT_LANGUAGES: tuple[str, ...] = (
    "en-US,en;q=0.9,ar;q=0.8",
    "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "ar-AE,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "en-GB,en;q=0.9,ar;q=0.8",
)


def _platform_for_ua(ua: str) -> str:
    if "Macintosh" in ua:
        return '"macOS"'
    if "Linux" in ua:
        return '"Linux"'
    return '"Windows"'


def _is_chromium_like(ua: str) -> bool:
    return ("Chrome/" in ua) or ("Edg/" in ua)


def random_fingerprint(seed: Optional[str] = None) -> dict[str, str]:
    """Return a coherent set of headers that mimic ONE real browser.

    If `seed` is provided (e.g. an account_id), the fingerprint is
    deterministic per-account so a given Webook account always presents
    the same browser identity (avoids "your fingerprint changed" alerts).
    """
    rng = random.Random(seed) if seed else random.Random()
    ua = rng.choice(USER_AGENTS)
    headers = {
        "user-agent": ua,
        "accept": "application/json, text/plain, */*",
        "accept-language": rng.choice(ACCEPT_LANGUAGES),
        "accept-encoding": "gzip, deflate, br",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }
    if _is_chromium_like(ua):
        headers["sec-ch-ua"] = rng.choice(SEC_CH_UA_VARIANTS)
        headers["sec-ch-ua-mobile"] = "?0"
        headers["sec-ch-ua-platform"] = _platform_for_ua(ua)
    return headers


def random_request_id() -> str:
    """X-Request-Id helper — useful when troubleshooting Cloudflare logs."""
    return _secrets.token_hex(8)


def _redact_url(url: str) -> str:
    """Strip query string from a URL before logging (avoids leaking
    bearer/token query params)."""
    q = url.find("?")
    return url if q < 0 else url[:q] + "?…"


# ════════════════════════════════════════════════════════════════════════
# StealthClient
# ════════════════════════════════════════════════════════════════════════
class StealthClient:
    """Thin, high-performance wrapper over httpx.AsyncClient.

    Each instance binds:
      • One `httpx.AsyncClient` with HTTP/2 negotiated via ALPN.
      • One coherent fingerprint (UA + sec-ch-ua + locale).
      • Optional proxy_url (per-account isolation).
    """

    DEFAULT_TIMEOUT = 25.0

    def __init__(
        self,
        *,
        proxy_url: Optional[str] = None,
        fingerprint_seed: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_connections: int = 20,
        max_keepalive: int = 8,
        verify: bool = True,
    ):
        self.proxy_url = (proxy_url or "").strip() or None
        self.fingerprint_seed = fingerprint_seed
        self._headers = random_fingerprint(fingerprint_seed)
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

        # httpx.Limits: a sane default that respects Render's 512MB envelope.
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive,
            keepalive_expiry=30.0,
        )
        self._verify = verify

    # ── async context-manager support ─────────────────────────────────
    async def __aenter__(self) -> "StealthClient":
        await self._ensure_client()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None and not self._client.is_closed:
            return self._client
        async with self._lock:
            if self._client is not None and not self._client.is_closed:
                return self._client

            kwargs: dict[str, Any] = {
                "http2": True,
                "timeout": httpx.Timeout(
                    self._timeout, connect=10.0, read=self._timeout,
                ),
                "limits": self._limits,
                "verify": self._verify,
                "follow_redirects": True,
                "headers": dict(self._headers),
            }
            if self.proxy_url:
                # httpx 0.27+ uses `proxy=` (singular) for AsyncClient.
                kwargs["proxy"] = self.proxy_url

            self._client = httpx.AsyncClient(**kwargs)
            log.debug(
                "stealth client up: http2=on proxy=%s ua=%s…",
                "yes" if self.proxy_url else "no",
                self._headers.get("user-agent", "")[:48],
            )
            return self._client

    async def close(self) -> None:
        c = self._client
        self._client = None
        if c is not None and not c.is_closed:
            try:
                await c.aclose()
            except Exception:
                pass

    # ── core request ──────────────────────────────────────────────────
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Any = None,
        json: Any = None,
        data: Any = None,
        cookies: Any = None,
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        client = await self._ensure_client()
        merged: dict[str, str] = dict(self._headers)
        if headers:
            for k, v in headers.items():
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[str(k).lower()] = str(v)
        # Rotate request id every call so retries don't collide.
        merged.setdefault("x-request-id", random_request_id())

        try:
            resp = await client.request(
                method.upper(), url,
                headers=merged, params=params,
                json=json, data=data, cookies=cookies,
                timeout=timeout if timeout is not None else self._timeout,
            )
            return resp
        except httpx.TimeoutException:
            log.debug("stealth %s %s timed out", method.upper(),
                      _redact_url(url))
            raise
        except httpx.RequestError as e:
            log.debug("stealth %s %s err: %s", method.upper(),
                      _redact_url(url), type(e).__name__)
            raise

    # ── convenience helpers (drop-in for aiohttp call sites) ──────────
    async def get_json(self, url: str, **kw) -> tuple[int, Any]:
        r = await self.request("GET", url, **kw)
        return r.status_code, _safe_json(r)

    async def post_json(self, url: str, **kw) -> tuple[int, Any]:
        r = await self.request("POST", url, **kw)
        return r.status_code, _safe_json(r)

    async def get_text(self, url: str, **kw) -> tuple[int, str]:
        r = await self.request("GET", url, **kw)
        try:
            return r.status_code, r.text
        except Exception:
            return r.status_code, ""

    # ── public introspection ──────────────────────────────────────────
    @property
    def fingerprint(self) -> dict[str, str]:
        return dict(self._headers)

    @property
    def http_version(self) -> str:
        if self._client is None:
            return "unknown"
        # httpx exposes http_version on Response; here we return the cfg.
        return "h2" if True else "1.1"


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:
        try:
            return {"raw": r.text[:1200]}
        except Exception:
            return {"raw": ""}


# ════════════════════════════════════════════════════════════════════════
# Module-level shared client (for read-only, no-proxy paths)
# ════════════════════════════════════════════════════════════════════════
_shared_client: Optional[StealthClient] = None
_shared_lock = asyncio.Lock()


async def get_shared_stealth_client() -> StealthClient:
    """Process-wide StealthClient for unauthenticated, no-proxy reads
    (asset bundle, public event meta when shared cache misses, etc.).
    """
    global _shared_client
    if _shared_client is not None and _shared_client._client is not None \
            and not _shared_client._client.is_closed:
        return _shared_client
    async with _shared_lock:
        if _shared_client is None or _shared_client._client is None \
                or _shared_client._client.is_closed:
            _shared_client = StealthClient(fingerprint_seed="shared-anon")
            await _shared_client._ensure_client()
        return _shared_client


async def close_shared_stealth_client() -> None:
    global _shared_client
    if _shared_client is not None:
        try:
            await _shared_client.close()
        except Exception:
            pass
        _shared_client = None


# ════════════════════════════════════════════════════════════════════════
# Self-test block
# ════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":  # pragma: no cover
    import sys
    import time

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    async def _selftest() -> int:
        print("🧪 Hydra V14 — stealth_client self-test")
        print("=" * 70)

        # Fingerprint stability per seed
        a = random_fingerprint("acc_001")
        b = random_fingerprint("acc_001")
        assert a["user-agent"] == b["user-agent"], "seeded fingerprint must be stable"
        print(f"  ✓ Seeded fingerprint stable: {a['user-agent'][:60]}…")

        c = random_fingerprint("acc_002")
        print(f"  ✓ Different seed → {c['user-agent'][:60]}…")

        # HTTP/2 round-trip against a public h2 endpoint
        async with StealthClient() as cli:
            t0 = time.time()
            r = await cli.request("GET", "https://www.cloudflare.com/cdn-cgi/trace")
            elapsed = (time.time() - t0) * 1000
            print(f"\n  ✓ HTTP {r.status_code} in {elapsed:.0f} ms"
                  f" (h2={r.http_version})")
            print(f"    body preview: {r.text[:80]!r}")

        # Webook reachability (the actual hot path)
        async with StealthClient(fingerprint_seed="hydra-v14-test") as cli:
            t0 = time.time()
            r = await cli.request(
                "GET",
                "https://api.webook.com/api/v2/event-detail/"
                "al-hilal-vs-al-nassr-test?lang=en&visible_in=rs",
            )
            elapsed = (time.time() - t0) * 1000
            print(f"  ✓ Webook API HTTP {r.status_code} in {elapsed:.0f} ms"
                  f" (h2={r.http_version})")

        return 0

    sys.exit(asyncio.run(_selftest()))
