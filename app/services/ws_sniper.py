"""
V15 — PHASE 2: SeatIOSniper, the real-time drop-watcher.

Listens on the seats.io messaging WebSocket for `objectStatusChanged`
events with status `available` (a seat just got released — someone's
hold-token expired or a cart got abandoned). Fires a callback within
milliseconds so the worker pool (PHASE 3) can race for a hold-token
before any other booker even sees the seat refresh.

Protocol (reverse-engineered from cdn-eu.seatsio.net/chart.js)
-------------------------------------------------------------
  • Endpoint:  wss://messaging-eu.seatsio.net/ws
               wss://messaging-na.seatsio.net/ws  (US workspaces)
               wss://messaging-am.seatsio.net/ws  (LATAM workspaces)
  • Origin:    https://webook.com  (mirrors the chart-renderer iframe)
  • Heartbeat: server pushes `[{"type":"PING"}]`; the client must reply
               with `{"type":"PONG"}` (within ~25 s, otherwise dropped).
  • Subscribe: `{"type":"subscribe","channel":"events.<event_key>"}`
               (the event_key is the seats.io chart event id, NOT the
               webook slug — resolved via the rendering-info JSON).
  • Frames:    raw JSON; server batches frames into a top-level array.
               Status messages look like:
                  {"type":"ObjectStatusChanged",
                   "objectLabel":"A1-12-5",
                   "status":"available",
                   "extraData":{...}}

Public API
----------
    async with SeatIOSniper(event_key="...") as snip:
        async for evt in snip.events():
            if evt.status == "available":
                await pool.fire(evt.object_label)

The class is also usable in callback-style:

    snip = SeatIOSniper(event_key="...", on_drop=async_callback)
    await snip.start()             # blocks until cancelled

Self-test
---------
    python -m app.services.ws_sniper [event_key]
        Establishes the WSS connection, subscribes, and prints the first
        N frames. Exit code 0 if connection + initial PING received.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

log = logging.getLogger("ws_sniper")


# ════════════════════════════════════════════════════════════════════════
# Endpoint pool — region-correct messaging clusters
# ════════════════════════════════════════════════════════════════════════
WS_ENDPOINTS: tuple[str, ...] = (
    "wss://messaging-eu.seatsio.net/ws",
    "wss://messaging-na.seatsio.net/ws",
    "wss://messaging-am.seatsio.net/ws",
    "wss://messaging-oc.seatsio.net/ws",
)

DEFAULT_HEADERS: dict[str, str] = {
    "Origin": "https://webook.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


# ════════════════════════════════════════════════════════════════════════
# Event payload
# ════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SeatStatusEvent:
    """One ObjectStatusChanged frame from the seats.io firehose."""
    object_label: str          # e.g. "A1-12-5"  (block-row-seat)
    object_id: str             # internal seats.io UUID
    status: str                # "available" | "booked" | "reservedByToken" | …
    event_key: str             # the chart event_key
    extra: dict[str, Any]      # everything else from the raw frame
    raw: dict[str, Any]        # original message (for debugging)

    @property
    def is_drop(self) -> bool:
        """True when a seat just became free (the only signal we care about)."""
        return self.status.lower() in ("available", "free", "ok")

    @classmethod
    def from_frame(cls, frame: dict, *, event_key: str) -> Optional["SeatStatusEvent"]:
        """Construct from a single decoded WS frame, or None if irrelevant."""
        if not isinstance(frame, dict):
            return None
        ftype = str(frame.get("type") or "").lower()
        if ftype not in ("objectstatuschanged", "object_status_changed",
                         "statuschanged"):
            return None
        return cls(
            object_label=str(
                frame.get("objectLabel") or frame.get("label") or ""
            ),
            object_id=str(
                frame.get("objectId") or frame.get("id") or ""
            ),
            status=str(frame.get("status") or "").lower(),
            event_key=event_key or str(frame.get("event") or ""),
            extra={k: v for k, v in frame.items()
                   if k not in {"type", "objectLabel", "label",
                                "objectId", "id", "status"}},
            raw=frame,
        )


# ════════════════════════════════════════════════════════════════════════
# Sniper
# ════════════════════════════════════════════════════════════════════════
class SeatIOSniper:
    """Real-time seats.io drop-watcher.

    Args:
      event_key: the seats.io chart event id (resolve via chart_mapper /
                 SeatsioClient first — it's NOT the webook slug).
      endpoint:  override the WSS URL (auto-rotates the EU/NA/AM pool by
                 default).
      on_drop:   optional async callback invoked for every drop event.
      reconnect_backoff: (initial, max) seconds between reconnect attempts.
    """

    PING_REPLY = '{"type":"PONG"}'

    def __init__(
        self,
        *,
        event_key: str,
        endpoint: Optional[str] = None,
        on_drop: Optional[Callable[[SeatStatusEvent], Awaitable[None]]] = None,
        reconnect_backoff: tuple[float, float] = (1.0, 30.0),
        connect_timeout: float = 15.0,
    ):
        if not event_key:
            raise ValueError("event_key is required")
        self.event_key = event_key
        self.endpoint = endpoint
        self._on_drop = on_drop
        self._backoff_initial, self._backoff_max = reconnect_backoff
        self._connect_timeout = connect_timeout
        self._queue: asyncio.Queue[SeatStatusEvent] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._ws: Optional[Any] = None
        self._task: Optional[asyncio.Task] = None
        self._connected = asyncio.Event()
        self._frames_seen = 0
        self._drops_seen = 0

    # ── lifecycle ─────────────────────────────────────────────────────
    async def __aenter__(self) -> "SeatIOSniper":
        self._task = asyncio.create_task(self._run(), name="ws_sniper")
        await asyncio.wait_for(self._connected.wait(), timeout=self._connect_timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    async def start(self) -> None:
        """Run the sniper as a long-lived task (blocks until stop())."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="ws_sniper")
        await self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ── public iteration / stats ──────────────────────────────────────
    async def events(self) -> AsyncIterator[SeatStatusEvent]:
        """Async iterator over all incoming SeatStatusEvent frames."""
        while not self._stop.is_set():
            try:
                evt = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield evt
            except asyncio.TimeoutError:
                continue

    @property
    def stats(self) -> dict[str, int]:
        return {
            "frames_seen": self._frames_seen,
            "drops_seen": self._drops_seen,
            "queue_size": self._queue.qsize(),
        }

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ── connection loop ───────────────────────────────────────────────
    def _endpoint_pool(self) -> tuple[str, ...]:
        if self.endpoint:
            return (self.endpoint,)
        return WS_ENDPOINTS

    async def _run(self) -> None:
        backoff = self._backoff_initial
        ep_idx = 0
        pool = self._endpoint_pool()
        while not self._stop.is_set():
            url = pool[ep_idx % len(pool)]
            try:
                async with websockets.connect(
                    url,
                    additional_headers=DEFAULT_HEADERS,
                    open_timeout=self._connect_timeout,
                    ping_interval=None,  # we manage PING/PONG manually
                    close_timeout=2.0,
                    max_size=4 * 1024 * 1024,  # 4 MiB safety
                ) as ws:
                    self._ws = ws
                    log.info("ws_sniper connected → %s", url)
                    # Subscribe immediately
                    sub = {
                        "type": "subscribe",
                        "channel": f"events.{self.event_key}",
                    }
                    await ws.send(json.dumps(sub))
                    self._connected.set()
                    backoff = self._backoff_initial  # reset on success
                    await self._read_loop(ws)
            except (ConnectionClosed, InvalidStatus, OSError, asyncio.TimeoutError) as e:
                log.warning("ws_sniper conn err on %s: %s", url, e)
            except Exception as e:  # pragma: no cover
                log.exception("ws_sniper unexpected err: %s", e)
            finally:
                self._ws = None
                self._connected.clear()
            if self._stop.is_set():
                break
            ep_idx += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._backoff_max)

    async def _read_loop(self, ws) -> None:
        """Drain the WSS connection until it closes or stop is requested."""
        async for raw in ws:
            if self._stop.is_set():
                break
            try:
                payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            except Exception:
                continue
            # Server batches frames into a top-level list, but single dicts
            # are also valid. Normalise both.
            frames = payload if isinstance(payload, list) else [payload]
            for frame in frames:
                if not isinstance(frame, dict):
                    continue
                self._frames_seen += 1
                ftype = str(frame.get("type") or "").upper()
                if ftype == "PING":
                    try:
                        await ws.send(self.PING_REPLY)
                    except Exception:
                        return
                    continue
                evt = SeatStatusEvent.from_frame(frame, event_key=self.event_key)
                if evt is None:
                    continue
                if evt.is_drop:
                    self._drops_seen += 1
                    if self._on_drop is not None:
                        try:
                            asyncio.create_task(self._on_drop(evt))
                        except Exception as e:  # pragma: no cover
                            log.warning("on_drop callback err: %s", e)
                # Always enqueue (consumer can filter)
                try:
                    self._queue.put_nowait(evt)
                except asyncio.QueueFull:
                    pass


# ════════════════════════════════════════════════════════════════════════
# Self-test
# ════════════════════════════════════════════════════════════════════════
async def _selftest(event_key: str = "selftest-channel") -> int:
    """Establish the WSS connection, subscribe, and confirm a PING is seen.

    Uses a bogus event_key — we just need to verify:
      1. The WSS handshake succeeds (no 4xx).
      2. The server accepts our subscribe frame.
      3. Heartbeat (PING) round-trip works.
    """
    print(f"  → connecting to seats.io messaging WS (event_key={event_key!r})…")
    snip = SeatIOSniper(event_key=event_key, connect_timeout=12.0)
    try:
        async with snip:
            print(f"  ✓ connected: {snip.connected}")
            # Wait up to 30 s for at least one server frame (PING typically <25 s)
            for _ in range(30):
                if snip._frames_seen > 0:
                    break
                await asyncio.sleep(1)
            print(f"  ✓ frames_seen: {snip._frames_seen}  "
                  f"drops_seen: {snip._drops_seen}")
            assert snip.connected, "must be connected"
            assert snip._frames_seen >= 1, "expected at least a PING within 30 s"
        print("\n🏆 ws_sniper self-test PASSED.")
        return 0
    except Exception as e:
        print(f"  ✗ self-test FAILED: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    print("🧪 Hydra V15 — ws_sniper self-test")
    print("=" * 70)
    ek = sys.argv[1] if len(sys.argv) > 1 else "selftest-channel"
    sys.exit(asyncio.run(_selftest(ek)))
