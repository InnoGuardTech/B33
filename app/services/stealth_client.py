"""
V14.1 — StealthClient on top of curl_cffi (TLS/JA3 + HTTP/2 impersonation).

Why curl_cffi (and not httpx[http2])?
-------------------------------------
Cloudflare's WAF on api.webook.com fingerprints the TLS handshake (JA3),
HTTP/2 frame settings and header order. httpx[http2] uses Python's stdlib
TLS stack — its JA3 is unique to Python and Cloudflare blocks it with a
403 / "{message: ''}". curl_cffi links against a libcurl built on
BoringSSL/NSS and ships with REAL Chrome/Edge/Safari fingerprints — the
TLS+H2 handshake is byte-for-byte identical to a real browser, so
Cloudflare lets the request through.

Verified live (Hydra V14 release):
    GET https://api.webook.com/api/v2/currencies   →  200 OK
    GET https://webook.com/ar/.../events/<slug>   →  200 OK   (CF passed)

Public API (drop-in compatible with the V13 httpx version):
    async with StealthClient(proxy_url=acc.proxy_url) as cli:
        status, body = await cli.get_json(url, headers=...)
        status, body = await cli.post_json(url, headers=..., json=...)
        status, text = await cli.get_text(url, headers=...)
"""
from __future__ import annotations

import asyncio
import logging
import random
import secrets as _secrets
from typing import Any, Optional

try:
    from curl_cffi.requests import AsyncSession  # type: ignore
    from curl_cffi.requests.errors import RequestsError  # type: ignore
    _CURL_CFFI_OK = True
    _IMPORT_ERR = None
except Exception as _e:  # pragma: no cover
    AsyncSession = None  # type: ignore
    RequestsError = Exception  # type: ignore
    _CURL_CFFI_OK = False
    _IMPORT_ERR = _e

log = logging.getLogger("stealth_client")


# ════════════════════════════════════════════════════════════════════════
# Browser-impersonation pool — keep newest at the top.
# ════════════════════════════════════════════════════════════════════════
IMPERSONATE_POOL: tuple[str, ...] = (
    "chrome131",
    "chrome124",
    "chrome120",
    "chrome119",
    "chrome116",
    "edge101",
    "edge99",
    "safari17_0",
    "safari15_5",
)

ACCEPT_LANGUAGES: tuple[str, ...] = (
    "en-US,en;q=0.9,ar;q=0.8",
    "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "ar-AE,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "en-GB,en;q=0.9,ar;q=0.8",
)


def random_request_id() -> str:
    return _secrets.token_hex(8)


def _redact_url(url: str) -> str:
    q = url.find("?")
    return url if q < 0 else url[:q] + "?…"


def pick_profile(seed: Optional[str] = None) -> str:
    rng = random.Random(seed) if seed else random.Random()
    return rng.choice(IMPERSONATE_POOL)


def pick_accept_language(seed: Optional[str] = None) -> str:
    rng = random.Random(seed + ":lang" if seed else None) if seed else random.Random()
    return rng.choice(ACCEPT_LANGUAGES)


# ════════════════════════════════════════════════════════════════════════
# StealthClient
# ════════════════════════════════════════════════════════════════════════
class StealthClient:
    DEFAULT_TIMEOUT = 25.0

    def __init__(
        self,
        *,
        proxy_url: Optional[str] = None,
        fingerprint_seed: Optional[str] = None,
        impersonate: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        verify: bool = True,
    ):
        if not _CURL_CFFI_OK:
            raise RuntimeError(
                "curl_cffi is not installed. Run "
                "`pip install curl_cffi>=0.7.4` (added to requirements.txt "
                f"in V14). Original import error: {_IMPORT_ERR}"
            )
        self.proxy_url = (proxy_url or "").strip() or None
        self.fingerprint_seed = fingerprint_seed
        self._impersonate = impersonate or pick_profile(fingerprint_seed)
        self._accept_language = pick_accept_language(fingerprint_seed)
        self._timeout = timeout
        self._verify = verify
        self._session: Optional[Any] = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "StealthClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _ensure_session(self):
        if self._session is not None:
            return self._session
        async with self._lock:
            if self._session is not None:
                return self._session
            kwargs: dict[str, Any] = {
                "impersonate": self._impersonate,
                "timeout": self._timeout,
                "verify": self._verify,
            }
            if self.proxy_url:
                kwargs["proxies"] = {
                    "http": self.proxy_url,
                    "https": self.proxy_url,
                }
            self._session = AsyncSession(**kwargs)  # type: ignore
            log.debug(
                "stealth client up: impersonate=%s proxy=%s",
                self._impersonate, "yes" if self.proxy_url else "no",
            )
            return self._session

    async def close(self) -> None:
        s = self._session
        self._session = None
        if s is not None:
            try:
                await s.close()
            except Exception:
                pass

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
        follow_redirects: bool = True,
    ):
        s = await self._ensure_session()
        merged: dict[str, str] = {
            "accept": "application/json, text/plain, */*",
            "accept-language": self._accept_language,
            "origin": "https://webook.com",
            "referer": "https://webook.com/",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        }
        if headers:
            for k, v in headers.items():
                if v is None:
                    merged.pop(k.lower(), None)
                else:
                    merged[str(k).lower()] = str(v)
        merged.setdefault("x-request-id", random_request_id())
        try:
            return await s.request(
                method.upper(), url,
                headers=merged, params=params,
                json=json, data=data, cookies=cookies,
                timeout=timeout if timeout is not None else self._timeout,
                allow_redirects=follow_redirects,
            )
        except RequestsError as e:
            log.debug("stealth %s %s err: %s", method.upper(),
                      _redact_url(url), type(e).__name__)
            raise

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

    @property
    def fingerprint(self) -> dict[str, str]:
        return {
            "impersonate": self._impersonate,
            "accept-language": self._accept_language,
        }

    @property
    def http_version(self) -> str:
        return "h2"


def _safe_json(r) -> Any:
    try:
        return r.json()
    except Exception:
        try:
            return {"raw": r.text[:1200]}
        except Exception:
            return {"raw": ""}


# ════════════════════════════════════════════════════════════════════════
# Module-level shared client
# ════════════════════════════════════════════════════════════════════════
_shared_client: Optional[StealthClient] = None
_shared_lock = asyncio.Lock()


async def get_shared_stealth_client() -> StealthClient:
    global _shared_client
    if _shared_client is not None and _shared_client._session is not None:
        return _shared_client
    async with _shared_lock:
        if _shared_client is None or _shared_client._session is None:
            _shared_client = StealthClient(fingerprint_seed="shared-anon")
            await _shared_client._ensure_session()
        return _shared_client


async def close_shared_stealth_client() -> None:
    global _shared_client
    if _shared_client is not None:
        try:
            await _shared_client.close()
        except Exception:
            pass
        _shared_client = None


def random_fingerprint(seed: Optional[str] = None) -> dict[str, str]:
    """Compatibility shim — real impersonation happens at TLS layer."""
    return {
        "impersonate": pick_profile(seed),
        "accept-language": pick_accept_language(seed),
    }


# ════════════════════════════════════════════════════════════════════════
# Self-test
# ════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":  # pragma: no cover
    import sys
    import time

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    async def _selftest() -> int:
        print("🧪 Hydra V14.1 — stealth_client (curl_cffi) self-test")
        print("=" * 70)

        a = pick_profile("acc_001")
        b = pick_profile("acc_001")
        assert a == b, "seeded profile must be stable"
        print(f"  ✓ Seeded profile stable: {a}")

        async with StealthClient() as cli:
            t0 = time.time()
            r = await cli.request(
                "GET",
                "https://api.webook.com/api/v2/currencies",
                headers={"sec-fetch-site": "same-site"},
            )
            elapsed = (time.time() - t0) * 1000
            print(f"  ✓ /currencies → HTTP {r.status_code} in {elapsed:.0f} ms")
            assert r.status_code == 200
            assert "Saudi Riyal" in r.text or "SAR" in r.text

        async with StealthClient(fingerprint_seed="hydra-v14-test") as cli:
            t0 = time.time()
            r = await cli.request(
                "GET",
                "https://webook.com/ar/sa/bur/sports-event/events/"
                "spl-week-32-al-najmah-vs-al-hazem-7715",
            )
            elapsed = (time.time() - t0) * 1000
            print(f"  ✓ event HTML  → HTTP {r.status_code} in {elapsed:.0f} ms")
            assert r.status_code == 200

        print("\n🏆 All self-tests passed. curl_cffi successfully bypasses CF.")
        return 0

    sys.exit(asyncio.run(_selftest()))
