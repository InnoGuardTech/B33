"""
V13 Performance layer — shared aiohttp session, event-meta cache,
and Turnstile token prewarm pool.

Goals:
  • One TCP connector reused across all bookings (50 sockets, 15/host).
  • Avoid duplicate /event-detail and /event-ticket-details calls when
    multiple accounts target the SAME event simultaneously.
  • Keep 5 Turnstile tokens hot at all times to remove the 4-8s solve
    latency from the booking critical path.

All caches are async-safe and bounded; they self-prune to stay within
Render's 512MB RAM budget.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import aiohttp

log = logging.getLogger("perf_cache")


# ════════════════════════════════════════════════════════════════════════
# Shared aiohttp session (singleton, lazy-init)
# ════════════════════════════════════════════════════════════════════════
_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()


async def get_shared_session() -> aiohttp.ClientSession:
    """Return the process-wide aiohttp ClientSession.

    Reusing one connector across booking attempts saves ~150-300ms per
    request (no fresh TLS handshake) and keeps the file-descriptor count
    low on Render free tier.
    """
    global _session
    if _session is not None and not _session.closed:
        return _session

    async with _session_lock:
        if _session is not None and not _session.closed:
            return _session
        connector = aiohttp.TCPConnector(
            limit=50,            # total parallel sockets
            limit_per_host=15,   # per-origin cap (Webook + seats.io + paytabs)
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        _session = aiohttp.ClientSession(
            connector=connector, timeout=timeout, trust_env=True,
        )
        log.info("🌐 shared aiohttp session ready (limit=50, per_host=15)")
        return _session


async def close_shared_session() -> None:
    """Graceful shutdown — called from main.lifespan."""
    global _session
    if _session is not None and not _session.closed:
        try:
            await _session.close()
        except Exception:
            pass
        _session = None


# ════════════════════════════════════════════════════════════════════════
# Event-meta cache — 30 second TTL, async-safe
# ════════════════════════════════════════════════════════════════════════
class _AsyncTTLCache:
    """Tiny async-safe TTL cache with single-flight semantics.

    When two callers request the same key simultaneously, only ONE
    upstream fetch runs; the second caller awaits the same future.
    """
    def __init__(self, ttl: float = 30.0, max_size: int = 256):
        self.ttl = float(ttl)
        self.max_size = int(max_size)
        self._store: dict[str, tuple[float, Any]] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def get_or_fetch(self, key: str,
                           fetcher: Callable[[], Awaitable[Any]]) -> Any:
        now = time.time()
        async with self._lock:
            hit = self._store.get(key)
            if hit and (now - hit[0]) < self.ttl:
                return hit[1]
            inflight = self._inflight.get(key)
            if inflight is not None:
                # someone else is already fetching — piggy-back
                fut = inflight
            else:
                fut = asyncio.get_event_loop().create_future()
                self._inflight[key] = fut

        # Outside the lock: either await the in-flight future or fetch.
        if inflight is not None:
            try:
                return await fut
            except Exception:
                # The leader failed; we fall through to retry below.
                pass

        try:
            value = await fetcher()
        except Exception as e:
            async with self._lock:
                self._inflight.pop(key, None)
            if not fut.done():
                fut.set_exception(e)
            raise
        else:
            async with self._lock:
                self._store[key] = (time.time(), value)
                self._inflight.pop(key, None)
                if len(self._store) > self.max_size:
                    # Drop the 20% oldest entries
                    items = sorted(self._store.items(), key=lambda kv: kv[1][0])
                    for k, _ in items[: max(1, self.max_size // 5)]:
                        self._store.pop(k, None)
            if not fut.done():
                fut.set_result(value)
            return value

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)


# Single instance shared across the booking layer
event_meta_cache = _AsyncTTLCache(ttl=30.0, max_size=256)


# ════════════════════════════════════════════════════════════════════════
# Turnstile token prewarm pool
# ════════════════════════════════════════════════════════════════════════
class TurnstilePrewarmPool:
    """Background-warmed pool of pre-solved Turnstile tokens.

    A small worker keeps `target_size` tokens ready. When a booking
    needs a token, it grabs one in O(1) from `pop()` instead of waiting
    4-8s for a fresh 2Captcha solve. Tokens older than `max_age` are
    discarded (Turnstile tokens have a short server-side validity).
    """
    def __init__(self, target_size: int = 5, max_age: float = 90.0):
        self.target_size = int(target_size)
        self.max_age = float(max_age)
        self._pool: list[tuple[float, str]] = []  # (timestamp, token)
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._enabled = False

    def enable(self) -> None:
        self._enabled = True

    async def pop(self) -> Optional[str]:
        """Return a fresh token from the pool, or None if empty/stale."""
        if not self._enabled:
            return None
        async with self._lock:
            now = time.time()
            while self._pool:
                ts, tok = self._pool.pop(0)
                if (now - ts) <= self.max_age:
                    return tok
            return None

    async def _refill_loop(self) -> None:
        """Background worker — keeps pool topped up."""
        from app.services.turnstile_solver import solve_turnstile
        from app.core.config import two_captcha_api_key, WEBOOK_ORIGIN

        # Default warm-up URL: Webook checkout page (carries the same
        # sitekey as live booking flow). solve_turnstile auto-discovers
        # the sitekey when not provided.
        warmup_url = f"{WEBOOK_ORIGIN}/"

        while not self._stop.is_set():
            try:
                if not two_captcha_api_key():
                    # No 2Captcha key configured — prewarm is a no-op.
                    await asyncio.sleep(30)
                    continue

                async with self._lock:
                    # Drop stale entries
                    now = time.time()
                    self._pool = [(ts, t) for ts, t in self._pool
                                  if (now - ts) <= self.max_age]
                    deficit = self.target_size - len(self._pool)

                if deficit <= 0:
                    await asyncio.sleep(5)
                    continue

                # Solve one token at a time (2Captcha is sequential-friendly).
                try:
                    tok = await asyncio.wait_for(
                        solve_turnstile(
                            warmup_url, sitekey="", force_refresh=True,
                        ),
                        timeout=60,
                    )
                except asyncio.TimeoutError:
                    log.debug("prewarm solve timed out")
                    await asyncio.sleep(10)
                    continue
                except Exception as e:
                    log.debug(f"prewarm solve failed: {e}")
                    await asyncio.sleep(10)
                    continue

                if tok:
                    async with self._lock:
                        self._pool.append((time.time(), tok))
                    log.info(
                        f"🛡️  turnstile pool: +1 token (size={len(self._pool)}/"
                        f"{self.target_size})"
                    )
                else:
                    await asyncio.sleep(8)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"prewarm loop tick err: {e}")
                await asyncio.sleep(15)

    def start(self) -> Optional[asyncio.Task]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop.clear()
        self._task = asyncio.create_task(
            self._refill_loop(), name="turnstile-prewarm",
        )
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        t = self._task
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None


# Singleton
turnstile_pool = TurnstilePrewarmPool(target_size=5, max_age=90.0)


# ════════════════════════════════════════════════════════════════════════
# Helpers — used by booking_orchestrator
# ════════════════════════════════════════════════════════════════════════
async def fetch_event_meta_cached(slug: str,
                                   fetcher: Callable[[], Awaitable[dict]]
                                   ) -> dict:
    """Cached wrapper around an upstream meta fetcher (TTL 30s)."""
    return await event_meta_cache.get_or_fetch(f"meta:{slug}", fetcher)
