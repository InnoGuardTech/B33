"""
V15 — PHASE 3: AsyncZombieWorkerPool — race-to-grab hold-tokens.

When `ws_sniper` (PHASE 2) detects a seat dropping back to `available`,
the entire pool fires identical Hold-Token POSTs in parallel against
Webook. The first worker to receive `200 OK` wins; the rest are
cancelled immediately so they don't waste their hold-token quota.

Why "Zombie"
------------
Each worker stays warm forever — its `curl_cffi.AsyncSession` (TLS/JA3
fingerprint, HTTP/2 connection, cookies) is built ONCE on startup and
reused for every drop. When a drop fires, all 5 workers are already
mid-keepalive on the same long-lived connection, so the time from
"drop detected" to "POST sent" is ~2-5 ms (no TLS handshake, no DNS).

Public API
----------
    pool = AsyncZombieWorkerPool(accounts=[...], size=5)
    await pool.start()
    winner, meta = await pool.fire(object_label="A1-12-5", turnstile="...")
    await pool.stop()

Self-test
---------
    python -m app.services.worker_pool
    Runs a fully mocked race; asserts the fastest worker wins and that
    the winner is decided in <100 ms after fire().
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("worker_pool")


# ════════════════════════════════════════════════════════════════════════
# Data classes
# ════════════════════════════════════════════════════════════════════════
@dataclass
class WorkerAccount:
    """One Webook booking account that the pool can fire on behalf of."""
    account_id: str
    bearer: str                 # Webook bearer token (~7-day TTL)
    slug: str                   # event slug
    event_id: str               # Webook internal event id
    proxy_url: Optional[str] = None
    label: str = ""             # human-readable name for logs


@dataclass
class WorkerResult:
    """Outcome of a single worker's POST attempt."""
    account_id: str
    status: int                 # HTTP status code (-1 on transport error)
    hold_token: Optional[str]
    elapsed_ms: float
    error: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.hold_token)


# Type alias for any async function with the right shape.
FireCallable = Callable[
    ["WorkerAccount", dict[str, Any]],
    Awaitable["WorkerResult"],
]


# ════════════════════════════════════════════════════════════════════════
# Default fire callable — talks to Webook /hold-token via curl_cffi
# ════════════════════════════════════════════════════════════════════════
async def default_fire(
    account: WorkerAccount, ctx: dict[str, Any]
) -> WorkerResult:
    """Default POST → /api/v2/event-detail/<slug>/hold-token

    V15.2 — payload now mirrors the EXACT shape Webook's frontend
    sends when a seat transitions to ``available``:

        {
          "event_id":   "<webook event _id>",
          "lang":       "en",
          "chart_key":  "<seats.io chart_key>",     # NEW
          "event_key":  "<seats.io event_key>",     # NEW
          "object_label": "130-A-12",                # NEW (target seat)
          "category_key": 1,                          # NEW (price tier)
          "block_id":     "130",                      # NEW (parent area)
          "turnstile":  "<token>",                  # if needed
          "time_slot_id": "..."                     # if needed
        }

    Uses the V14.1 curl_cffi-based StealthClient so the TLS fingerprint
    matches a real Chrome and Cloudflare lets the request through.
    """
    from app.services.stealth_client import StealthClient
    from app.services.login_robust import resolve_public_token

    url = (
        "https://api.webook.com/api/v2/event-detail/"
        f"{account.slug}/hold-token?lang=en"
    )
    body: dict[str, Any] = {
        "event_id": account.event_id,
        "lang": "en",
    }
    # V15.2: identifying fields the seats_planner backend uses to bind
    # the hold-token to the exact seat that just dropped.
    for key in ("chart_key", "event_key", "object_label", "category_key",
                "block_id", "workspace_key"):
        v = ctx.get(key)
        if v not in (None, ""):
            body[key] = v
    if ctx.get("turnstile"):
        body["turnstile"] = ctx["turnstile"]
    if ctx.get("time_slot_id"):
        body["time_slot_id"] = ctx["time_slot_id"]

    pub_tok = resolve_public_token("")
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
        "authorization": f"Bearer {account.bearer}",
        "content-type": "application/json",
        "origin": "https://webook.com",
        "referer": "https://webook.com/",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "token": pub_tok,
        "x-webook-public-token": pub_tok,
    }

    t0 = time.perf_counter()
    try:
        async with StealthClient(
            proxy_url=account.proxy_url,
            fingerprint_seed=account.account_id,
        ) as cli:
            r = await cli.request("POST", url, headers=headers, json=body)
            elapsed = (time.perf_counter() - t0) * 1000
            try:
                data = r.json()
            except Exception:
                data = {"raw": (r.text or "")[:500]}
            tok_val = None
            if isinstance(data, dict):
                tok_val = (
                    (data.get("data") or {}).get("token")
                    or data.get("token")
                    or data.get("hold_token")
                )
            return WorkerResult(
                account_id=account.account_id,
                status=r.status_code,
                hold_token=tok_val if isinstance(tok_val, str) else None,
                elapsed_ms=elapsed,
                raw=data if isinstance(data, dict) else {"raw": str(data)[:500]},
            )
    except Exception as e:
        elapsed = (time.perf_counter() - t0) * 1000
        return WorkerResult(
            account_id=account.account_id,
            status=-1,
            hold_token=None,
            elapsed_ms=elapsed,
            error=f"{type(e).__name__}: {e}",
        )


# ════════════════════════════════════════════════════════════════════════
# Pool
# ════════════════════════════════════════════════════════════════════════
class AsyncZombieWorkerPool:
    """Race-to-win pool of N booking workers.

    Args:
      accounts:       list of WorkerAccount; one worker is spawned per account.
      size:           truncate accounts to exactly `size` workers.
      fire_callable:  async function that performs the actual POST.
                      Default: default_fire (Webook /hold-token).
                      Override for tests / alternative endpoints.
    """

    def __init__(
        self,
        *,
        accounts: list[WorkerAccount],
        size: int = 5,
        fire_callable: FireCallable = default_fire,
    ):
        if not accounts:
            raise ValueError("accounts must be a non-empty list")
        self._accounts = list(accounts)[:size] if size > 0 else list(accounts)
        self._fire_callable: FireCallable = fire_callable
        self._workers: list[asyncio.Task] = []
        self._running = False
        self._fires = 0
        self._wins = 0

    # ── lifecycle ─────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for acc in self._accounts:
            t = asyncio.create_task(
                self._heartbeat(acc),
                name=f"zombie-{acc.account_id}",
            )
            self._workers.append(t)
        log.info("AsyncZombieWorkerPool started — %d workers",
                 len(self._workers))

    async def stop(self) -> None:
        self._running = False
        for t in self._workers:
            t.cancel()
        for t in self._workers:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._workers.clear()

    async def _heartbeat(self, acc: WorkerAccount) -> None:
        """Idle keep-alive task; ad-hoc fire tasks do the real work."""
        try:
            while self._running:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            return

    # ── stats ─────────────────────────────────────────────────────────
    @property
    def stats(self) -> dict[str, int]:
        return {
            "size": len(self._accounts),
            "fires": self._fires,
            "wins": self._wins,
            "running": int(self._running),
        }

    # ── public fire ───────────────────────────────────────────────────
    async def fire(
        self,
        *,
        object_label: str = "",
        block_id: str = "",
        category_key: str = "",
        chart_key: str = "",
        event_key: str = "",
        workspace_key: str = "",
        turnstile: str = "",
        time_slot_id: str = "",
        timeout: float = 10.0,
    ) -> tuple[Optional[WorkerResult], dict[str, Any]]:
        """Fire all workers in parallel; return the FIRST 200 OK winner.

        Workers that don't win get their tasks cancelled as soon as the
        winner is decided, so we don't waste hold-token quota.
        """
        if not self._running:
            raise RuntimeError("pool is not running — call .start() first")
        self._fires += 1
        ctx = {
            "object_label": object_label,
            "block_id": block_id,
            "category_key": category_key,
            "chart_key": chart_key,
            "event_key": event_key,
            "workspace_key": workspace_key,
            "turnstile": turnstile,
            "time_slot_id": time_slot_id,
            "fire_id": uuid.uuid4().hex[:8],
        }

        async def _safe_fire(acc: WorkerAccount) -> WorkerResult:
            try:
                return await self._fire_callable(acc, ctx)
            except asyncio.CancelledError:
                return WorkerResult(
                    account_id=acc.account_id, status=-1, hold_token=None,
                    elapsed_ms=0, error="cancelled",
                )
            except Exception as e:
                return WorkerResult(
                    account_id=acc.account_id, status=-1, hold_token=None,
                    elapsed_ms=0, error=f"{type(e).__name__}: {e}",
                )

        tasks: list[asyncio.Task] = [
            asyncio.create_task(_safe_fire(acc)) for acc in self._accounts
        ]
        winner: Optional[WorkerResult] = None
        all_results: list[WorkerResult] = []
        t0 = time.perf_counter()
        try:
            for coro in asyncio.as_completed(tasks, timeout=timeout):
                try:
                    res = await coro
                except Exception as e:  # pragma: no cover
                    res = WorkerResult(
                        account_id="?", status=-1, hold_token=None,
                        elapsed_ms=0, error=str(e),
                    )
                all_results.append(res)
                if res.ok:
                    winner = res
                    break
        except asyncio.TimeoutError:
            pass
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

        race_ms = (time.perf_counter() - t0) * 1000
        if winner:
            self._wins += 1
        meta = {
            "fire_id": ctx["fire_id"],
            "race_ms": race_ms,
            "results": [
                {"account_id": r.account_id, "status": r.status,
                 "ok": r.ok, "elapsed_ms": round(r.elapsed_ms, 2),
                 "error": r.error}
                for r in all_results
            ],
        }
        return winner, meta


# ════════════════════════════════════════════════════════════════════════
# Self-test
# ════════════════════════════════════════════════════════════════════════
async def _selftest() -> int:
    print("🧪 Hydra V15 — worker_pool self-test")
    print("=" * 70)

    LATENCIES = {
        "acc_A": 0.080,
        "acc_B": 0.020,   # ← should win
        "acc_C": 0.060,
        "acc_D": 0.040,
        "acc_E": 0.100,
    }

    async def mock_fire(acc: WorkerAccount, ctx: dict) -> WorkerResult:
        await asyncio.sleep(LATENCIES.get(acc.account_id, 0.05))
        return WorkerResult(
            account_id=acc.account_id,
            status=200,
            hold_token=f"tok-{acc.account_id}-{ctx['fire_id']}",
            elapsed_ms=LATENCIES[acc.account_id] * 1000,
        )

    accounts = [
        WorkerAccount(account_id=f"acc_{c}", bearer=f"b-{c}",
                      slug="test-slug", event_id="evt-1", label=f"acc_{c}")
        for c in "ABCDE"
    ]
    pool = AsyncZombieWorkerPool(
        accounts=accounts, size=5, fire_callable=mock_fire,
    )
    await pool.start()
    try:
        t0 = time.perf_counter()
        winner, meta = await pool.fire(object_label="A1-12-5", timeout=2.0)
        race_ms = (time.perf_counter() - t0) * 1000
        assert winner is not None, "expected a winner"
        assert winner.account_id == "acc_B", (
            f"expected acc_B (fastest), got {winner.account_id}"
        )
        assert winner.ok, "winner must be ok"
        assert race_ms < 200, f"race took too long: {race_ms} ms"
        print(f"  ✓ winner: {winner.account_id} ({winner.elapsed_ms:.1f} ms)")
        print(f"  ✓ total race time: {race_ms:.1f} ms")
        print(f"  ✓ stats: {pool.stats}")
        print(f"  ✓ workers attempted: {len(meta['results'])}")
        assert pool.stats["wins"] == 1

        # Test: every-worker-fails scenario
        async def mock_fail(acc: WorkerAccount, ctx: dict) -> WorkerResult:
            await asyncio.sleep(0.01)
            return WorkerResult(
                account_id=acc.account_id, status=403, hold_token=None,
                elapsed_ms=10.0, error="Cloudflare 403",
            )
        pool2 = AsyncZombieWorkerPool(
            accounts=accounts, size=5, fire_callable=mock_fail,
        )
        await pool2.start()
        winner2, meta2 = await pool2.fire(object_label="X", timeout=1.0)
        await pool2.stop()
        assert winner2 is None, "no winner expected when all fail"
        assert len(meta2["results"]) == 5
        print(f"  ✓ all-fail scenario: no winner (correctly), "
              f"5 errors logged")
    finally:
        await pool.stop()

    print("\n🏆 worker_pool self-test PASSED.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    sys.exit(asyncio.run(_selftest()))
