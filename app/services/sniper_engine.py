"""
V15 — PHASE 5: SniperEngine — wires Phases 1-4 into a single async machine.

Pipeline:

    chart_mapper.fetch_rendering_info(slug)         ┐  PHASE 1
        ↓ extract_blocks / extract_categories       │
        ↓ chart_mapper.build_blocks_keyboard        │
                                                    │
    ws_sniper.SeatIOSniper(event_key)               ┐  PHASE 2
        ↓ SeatStatusEvent (status=available)        │
                                                    │
    worker_pool.AsyncZombieWorkerPool.fire(...)     ┐  PHASE 3
        ↓ winner WorkerResult (hold_token=...)      │
                                                    │
    checkout_handler.create_checkout(...)           ┐  PHASE 4
        ↓ CheckoutResult (payment_url=...)          │
        ↓ format_telegram_alert                     │
                                                    ▼
    notifier.send(chat_id, alert)

Public API
----------
    eng = SniperEngine(
        slug="...", event_key="...",
        accounts=[...], notifier=..., chat_id="...",
        target_blocks=["A1", "B12"], quantity=2,
    )
    await eng.start()        # returns immediately; engine runs in bg
    ...
    await eng.stop()

Self-test
---------
    python -m app.services.sniper_engine
    Mocks all four sub-modules and verifies the end-to-end flow:
    drop event → race → checkout → notify.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from app.services.checkout_handler import (
    CheckoutResult, create_checkout, format_telegram_alert,
)
from app.services.worker_pool import (
    AsyncZombieWorkerPool, WorkerAccount, WorkerResult,
)
from app.services.ws_sniper import SeatIOSniper, SeatStatusEvent

log = logging.getLogger("sniper_engine")


# ════════════════════════════════════════════════════════════════════════
# Notifier protocol — minimal interface so tests don't need Telegram
# ════════════════════════════════════════════════════════════════════════
NotifyCallable = Callable[[str, str], Awaitable[None]]


@dataclass
class SniperConfig:
    """Configuration for one running SniperEngine."""
    slug: str                       # webook event slug
    event_key: str                  # seats.io chart event_key
    chat_id: str                    # telegram chat to notify on win
    accounts: list[WorkerAccount]   # booking accounts to race with
    target_labels: list[str] = field(default_factory=list)
    quantity: int = 1               # tickets per booking attempt
    payment_method: str = "credit_card"
    pool_size: int = 5
    event_title: str = ""
    fire_throttle_ms: int = 800     # cooldown after a successful fire


@dataclass
class SniperStats:
    drops_seen: int = 0
    fires: int = 0
    wins: int = 0
    checkouts_ok: int = 0
    notifications_sent: int = 0


# ════════════════════════════════════════════════════════════════════════
# Engine
# ════════════════════════════════════════════════════════════════════════
class SniperEngine:
    """End-to-end sniping pipeline — drop → race → checkout → notify."""

    def __init__(
        self,
        config: SniperConfig,
        *,
        notify: NotifyCallable,
        # injection points for tests
        sniper_factory: Optional[Callable[[], SeatIOSniper]] = None,
        pool_factory: Optional[Callable[[], AsyncZombieWorkerPool]] = None,
        checkout_callable: Optional[
            Callable[..., Awaitable[CheckoutResult]]
        ] = None,
    ):
        self.cfg = config
        self._notify = notify
        self._sniper_factory = sniper_factory or self._default_sniper
        self._pool_factory = pool_factory or self._default_pool
        self._checkout = checkout_callable or create_checkout
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._fire_lock = asyncio.Lock()
        self._last_fire_ts = 0.0
        self.stats = SniperStats()

    def _default_sniper(self) -> SeatIOSniper:
        return SeatIOSniper(event_key=self.cfg.event_key)

    def _default_pool(self) -> AsyncZombieWorkerPool:
        return AsyncZombieWorkerPool(
            accounts=self.cfg.accounts, size=self.cfg.pool_size,
        )

    # ── lifecycle ─────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(
                self._run(), name=f"sniper-{self.cfg.event_key}",
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        sniper = self._sniper_factory()
        pool = self._pool_factory()
        await pool.start()
        try:
            async with sniper:
                async for evt in sniper.events():
                    if self._stop.is_set():
                        break
                    if not evt.is_drop:
                        continue
                    self.stats.drops_seen += 1
                    if not self._matches_target(evt):
                        continue
                    asyncio.create_task(self._handle_drop(evt, pool))
        finally:
            await pool.stop()

    def _matches_target(self, evt: SeatStatusEvent) -> bool:
        """Return True if the drop concerns one of our target blocks."""
        if not self.cfg.target_labels:
            return True  # no filter → accept all drops
        label_l = (evt.object_label or "").lower()
        for t in self.cfg.target_labels:
            if t.lower() in label_l or label_l.startswith(t.lower()):
                return True
        return False

    # ── drop handler ──────────────────────────────────────────────────
    async def _handle_drop(
        self, evt: SeatStatusEvent, pool: AsyncZombieWorkerPool,
    ) -> None:
        # Throttle: at most one race per fire_throttle_ms window.
        async with self._fire_lock:
            import time as _t
            now = _t.monotonic() * 1000
            if now - self._last_fire_ts < self.cfg.fire_throttle_ms:
                return
            self._last_fire_ts = now

        self.stats.fires += 1
        log.info("🎯 DROP detected: %s — firing pool of %d",
                 evt.object_label, len(self.cfg.accounts))

        winner, fire_meta = await pool.fire(
            object_label=evt.object_label, timeout=8.0,
        )
        if not winner:
            log.info("  no winner this race (results=%d)",
                     len(fire_meta.get("results", [])))
            return
        self.stats.wins += 1
        log.info("  ✓ winner=%s hold_token=%s…",
                 winner.account_id, (winner.hold_token or "")[:16])

        # Find the account so we can re-use its bearer/proxy in checkout.
        acc = next((a for a in self.cfg.accounts
                    if a.account_id == winner.account_id), None)
        if acc is None:
            return

        # Build the tickets payload — caller can override via target_labels.
        tickets = [{
            "object_label": evt.object_label,
            "category_key": evt.extra.get("categoryKey", "") if evt.extra else "",
            "quantity": max(1, self.cfg.quantity),
        }]

        co = await self._checkout(
            slug=acc.slug,
            event_id=acc.event_id,
            bearer=acc.bearer,
            hold_token=winner.hold_token,
            tickets=tickets,
            payment=self.cfg.payment_method,
            proxy_url=acc.proxy_url,
            fingerprint_seed=acc.account_id,
        )
        if co.ok:
            self.stats.checkouts_ok += 1
            msg = format_telegram_alert(
                co,
                event_title=self.cfg.event_title,
                seat_label=evt.object_label,
            )
            await self._notify(self.cfg.chat_id, msg)
            self.stats.notifications_sent += 1


# ════════════════════════════════════════════════════════════════════════
# Self-test (mocked — no live network)
# ════════════════════════════════════════════════════════════════════════
async def _selftest() -> int:
    print("🧪 Hydra V15 — sniper_engine self-test")
    print("=" * 70)

    # 1. Mock notifier
    sent: list[tuple[str, str]] = []
    async def mock_notify(chat_id: str, text: str) -> None:
        sent.append((chat_id, text))

    # 2. Mock checkout — always returns a valid PayTabs URL
    async def mock_checkout(**kwargs) -> CheckoutResult:
        return CheckoutResult(
            status=200,
            payment_url=f"https://secure.paytabs.sa/payment/{kwargs['hold_token']}",
            order_id="WBK-TEST-1",
            amount=350.0, currency="SAR", method="credit_card",
            expires_at=None, elapsed_ms=120,
        )

    # 3. Mock pool — instant fake hold-token
    async def mock_fire(acc: WorkerAccount, ctx: dict) -> WorkerResult:
        await asyncio.sleep(0.01)
        return WorkerResult(
            account_id=acc.account_id, status=200,
            hold_token=f"hold-{acc.account_id}", elapsed_ms=10,
        )

    accounts = [
        WorkerAccount(account_id=f"acc_{c}", bearer=f"b-{c}",
                      slug="test-slug", event_id="evt-1")
        for c in "AB"
    ]

    def pool_factory():
        return AsyncZombieWorkerPool(
            accounts=accounts, size=2, fire_callable=mock_fire,
        )

    # 4. Mock sniper — stub object that yields one drop event then sleeps
    class _FakeSniper:
        def __init__(self):
            self._events = asyncio.Queue()
            # pre-populate with a drop event
            self._events.put_nowait(SeatStatusEvent(
                object_label="A1-12-5", object_id="oid-1",
                status="available", event_key="evt-1",
                extra={"categoryKey": "premium"},
                raw={},
            ))
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            return None
        async def events(self):
            while True:
                try:
                    evt = await asyncio.wait_for(self._events.get(), timeout=0.5)
                    yield evt
                except asyncio.TimeoutError:
                    return  # finish iteration after first event drains

    cfg = SniperConfig(
        slug="test-slug", event_key="evt-1", chat_id="123",
        accounts=accounts, target_labels=["A1"], quantity=1,
        event_title="Test Event",
    )
    eng = SniperEngine(
        cfg, notify=mock_notify,
        sniper_factory=lambda: _FakeSniper(),  # type: ignore[return-value]
        pool_factory=pool_factory,
        checkout_callable=mock_checkout,
    )
    await eng.start()
    # Give the engine ~1s to process the drop end-to-end
    await asyncio.sleep(1.2)
    await eng.stop()

    print(f"  ✓ stats: {eng.stats}")
    assert eng.stats.drops_seen >= 1, "should have seen the drop"
    assert eng.stats.fires == 1, f"expected 1 fire, got {eng.stats.fires}"
    assert eng.stats.wins == 1, f"expected 1 win, got {eng.stats.wins}"
    assert eng.stats.checkouts_ok == 1
    assert eng.stats.notifications_sent == 1
    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == "123"
    assert "secure.paytabs.sa" in text
    assert "A1-12-5" in text
    assert "Test Event" in text
    print(f"  ✓ notification sent to chat_id={chat_id}")
    print(f"  ✓ alert preview: {text.splitlines()[0]}")

    # Test target-filter rejection: a non-matching drop must not fire
    cfg2 = SniperConfig(
        slug="t", event_key="e", chat_id="9", accounts=accounts,
        target_labels=["ZZ_NOMATCH"], quantity=1,
    )
    eng2 = SniperEngine(
        cfg2, notify=mock_notify,
        sniper_factory=lambda: _FakeSniper(),  # type: ignore[return-value]
        pool_factory=pool_factory,
        checkout_callable=mock_checkout,
    )
    await eng2.start()
    await asyncio.sleep(0.8)
    await eng2.stop()
    assert eng2.stats.drops_seen >= 1
    assert eng2.stats.fires == 0, "must NOT fire for non-matching block"
    print(f"  ✓ target-filter correctly rejected non-matching drop")

    print("\n🏆 sniper_engine self-test PASSED.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    sys.exit(asyncio.run(_selftest()))
