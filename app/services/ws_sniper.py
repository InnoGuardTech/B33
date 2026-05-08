"""
V15.2 — SeatIOSniper: real-time WSS drop watcher with block-targeted filtering.

Listens on the seats.io / seatcloud messaging WebSocket for
``ObjectStatusChanged`` frames where ``status == "available"`` (a seat
just got released). Fires a callback within milliseconds so the worker
pool (PHASE 3) can race for a hold-token before any other booker even
sees the seat refresh.

V15.2 upgrade
-------------
The original V15 sniper subscribed only to ``events.<event_key>``. That
worked for legacy seatsio events but seats_planner needs a TWO-key
handshake:

    {
      "type":  "subscribe",
      "channel": "events.<event_key>",
      "chartKey": "<chart_key>",                 # NEW
      "workspaceKey": "<workspace_key>",         # NEW
      "token": "<workspace public token>"        # NEW (when present)
    }

The class now also:

  • Resolves messaging cluster from workspace region when known.
  • Filters drops by ``target_block_ids`` (set at runtime from Telegram
    keyboard pick) — only frames whose objectLabel/blockId matches one
    of the user's chosen blocks fire the on_drop callback. Frames are
    still enqueued for the consumer iterator (which can do its own
    filtering for stats).
  • Exposes ``set_targets(block_ids)`` so handlers.py can update the
    filter without restarting the WSS connection.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Optional

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

# seatcloud (Webook's seats_planner cluster) ships its own messaging hub.
# Some workspaces only publish updates here, so we probe both stacks.
SEATCLOUD_WS_ENDPOINTS: tuple[str, ...] = (
    "wss://messaging.seatcloud.com/ws",
    "wss://api.seatcloud.com/ws",
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
    """One ObjectStatusChanged frame from the seats.io / seatcloud hose."""
    object_label: str          # e.g. "130-A-12"  (block-row-seat)
    object_id: str             # internal seats.io UUID OR seats_planner block id
    status: str                # "available" | "booked" | "reservedByToken" | …
    event_key: str             # the chart event_key
    block_id: str              # parent block / area id (parsed best-effort)
    extra: dict[str, Any]      # everything else from the raw frame
    raw: dict[str, Any]        # original message (for debugging)

    @property
    def is_drop(self) -> bool:
        """True when a seat just became free (the only signal we care about)."""
        return self.status.lower() in ("available", "free", "ok")

    @classmethod
    def from_frame(
        cls, frame: dict, *, event_key: str,
    ) -> Optional["SeatStatusEvent"]:
        """Construct from a single decoded WS frame, or None if irrelevant."""
        if not isinstance(frame, dict):
            return None
        ftype = str(frame.get("type") or "").lower()
        if ftype not in (
            "objectstatuschanged", "object_status_changed",
            "statuschanged", "objectstatuschange",
        ):
            return None
        obj_label = str(
            frame.get("objectLabel")
            or frame.get("label")
            or frame.get("seatLabel")
            or ""
        )
        # Best-effort block id resolution (seats_planner usually sends it
        # as `parentArea` / `blockId` / `sectionId` / first segment of label).
        block_id = str(
            frame.get("blockId")
            or frame.get("block_id")
            or frame.get("sectionId")
            or frame.get("parentArea")
            or frame.get("areaId")
            or ""
        )
        if not block_id and obj_label:
            # Labels like "130-A-12" — first hyphen-segment is the block.
            head = obj_label.split("-", 1)[0].strip()
            if head:
                block_id = head
        return cls(
            object_label=obj_label,
            object_id=str(frame.get("objectId") or frame.get("id") or ""),
            status=str(frame.get("status") or "").lower(),
            event_key=event_key or str(frame.get("event") or ""),
            block_id=block_id,
            extra={k: v for k, v in frame.items()
                   if k not in {"type", "objectLabel", "label", "seatLabel",
                                "objectId", "id", "status", "blockId",
                                "block_id", "sectionId", "parentArea",
                                "areaId"}},
            raw=frame,
        )


# ════════════════════════════════════════════════════════════════════════
# Sniper
# ════════════════════════════════════════════════════════════════════════
class SeatIOSniper:
    """Real-time seats.io / seatcloud drop-watcher."""

    PING_REPLY = '{"type":"PONG"}'

    def __init__(
        self,
        *,
        event_key: str,
        chart_key: str = "",
        workspace_key: str = "",
        workspace_token: str = "",
        endpoint: Optional[str] = None,
        on_drop: Optional[Callable[[SeatStatusEvent], Awaitable[None]]] = None,
        target_block_ids: Optional[Iterable[str]] = None,
        reconnect_backoff: tuple[float, float] = (1.0, 30.0),
        connect_timeout: float = 15.0,
        prefer_seatcloud: bool = False,
    ):
        if not event_key:
            raise ValueError("event_key is required")
        self.event_key = event_key
        self.chart_key = chart_key or ""
        self.workspace_key = workspace_key or ""
        self.workspace_token = workspace_token or ""
        self.endpoint = endpoint
        self._on_drop = on_drop
        self._targets: set[str] = {str(b) for b in (target_block_ids or [])}
        self._backoff_initial, self._backoff_max = reconnect_backoff
        self._connect_timeout = connect_timeout
        self._prefer_seatcloud = prefer_seatcloud
        self._queue: asyncio.Queue[SeatStatusEvent] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._ws: Optional[Any] = None
        self._task: Optional[asyncio.Task] = None
        self._connected = asyncio.Event()
        self._frames_seen = 0
        self._drops_seen = 0
        self._matched_drops = 0
        self._endpoint_used = ""

    # ── public mutators ───────────────────────────────────────────────
    def set_targets(self, block_ids: Iterable[str]) -> None:
        """Update the block filter on the fly. Empty set = accept all."""
        self._targets = {str(b) for b in block_ids if str(b)}
        log.info("ws_sniper targets updated: %s", sorted(self._targets))

    def matches_target(self, evt: SeatStatusEvent) -> bool:
        """Whether this drop concerns one of the user's chosen blocks."""
        if not self._targets:
            return True
        return (
            evt.block_id in self._targets
            or evt.object_id in self._targets
            or evt.object_label in self._targets
            or any(t in evt.object_label for t in self._targets)
        )

    # ── lifecycle ─────────────────────────────────────────────────────
    async def __aenter__(self) -> "SeatIOSniper":
        self._task = asyncio.create_task(self._run(), name="ws_sniper")
        await asyncio.wait_for(
            self._connected.wait(), timeout=self._connect_timeout,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    async def start(self) -> None:
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
        while not self._stop.is_set():
            try:
                evt = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield evt
            except asyncio.TimeoutError:
                continue

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "frames_seen": self._frames_seen,
            "drops_seen": self._drops_seen,
            "matched_drops": self._matched_drops,
            "queue_size": self._queue.qsize(),
            "endpoint": self._endpoint_used,
            "targets": sorted(self._targets),
        }

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ── connection loop ───────────────────────────────────────────────
    def _endpoint_pool(self) -> tuple[str, ...]:
        if self.endpoint:
            return (self.endpoint,)
        if self._prefer_seatcloud:
            return SEATCLOUD_WS_ENDPOINTS + WS_ENDPOINTS
        return WS_ENDPOINTS + SEATCLOUD_WS_ENDPOINTS

    def _build_subscribe_frame(self) -> dict[str, Any]:
        """V15.2 — full subscription envelope.

        Carries chartKey + workspaceKey + token so seats_planner accepts
        the connection AND we receive seat-level status frames in
        real-time (legacy events ignore the extras silently).
        """
        sub: dict[str, Any] = {
            "type": "subscribe",
            "channel": f"events.{self.event_key}",
            "eventKey": self.event_key,
        }
        if self.chart_key:
            sub["chartKey"] = self.chart_key
        if self.workspace_key:
            sub["workspaceKey"] = self.workspace_key
        if self.workspace_token:
            sub["token"] = self.workspace_token
        return sub

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
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self._endpoint_used = url
                    log.info("ws_sniper connected → %s (event=%s chart=%s)",
                             url, self.event_key[:24], self.chart_key[:24])
                    await ws.send(json.dumps(self._build_subscribe_frame()))
                    self._connected.set()
                    backoff = self._backoff_initial
                    await self._read_loop(ws)
            except (ConnectionClosed, InvalidStatus, OSError,
                    asyncio.TimeoutError) as e:
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
        async for raw in ws:
            if self._stop.is_set():
                break
            try:
                payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            except Exception:
                continue
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
                    if self.matches_target(evt):
                        self._matched_drops += 1
                        log.info("🎯 matched drop: block=%s label=%s",
                                 evt.block_id, evt.object_label)
                        if self._on_drop is not None:
                            try:
                                asyncio.create_task(self._on_drop(evt))
                            except Exception as e:
                                log.warning("on_drop callback err: %s", e)
                # Always enqueue; consumer can filter.
                try:
                    self._queue.put_nowait(evt)
                except asyncio.QueueFull:
                    pass


# ════════════════════════════════════════════════════════════════════════
# Self-test
# ════════════════════════════════════════════════════════════════════════
async def _selftest(event_key: str = "selftest-channel") -> int:
    print(f"  → connecting to seats.io messaging WS (event_key={event_key!r})…")
    snip = SeatIOSniper(
        event_key=event_key,
        chart_key="3d17635d-e547-434b-b7de-f374036045d4",
        workspace_key="66e63c10464382fb1f049832",
        target_block_ids={"130", "131"},
        connect_timeout=12.0,
    )
    try:
        async with snip:
            print(f"  ✓ connected: {snip.connected}")
            for _ in range(30):
                if snip._frames_seen > 0:
                    break
                await asyncio.sleep(1)
            print(f"  ✓ {snip.stats}")
            assert snip.connected
            assert snip._frames_seen >= 1, "expected a PING within 30 s"

        # Filter test (offline)
        from dataclasses import replace
        evt_match = SeatStatusEvent(
            object_label="130-A-5", object_id="oid1", status="available",
            event_key=event_key, block_id="130", extra={}, raw={},
        )
        evt_miss = SeatStatusEvent(
            object_label="999-A-5", object_id="oid2", status="available",
            event_key=event_key, block_id="999", extra={}, raw={},
        )
        assert snip.matches_target(evt_match)
        assert not snip.matches_target(evt_miss)
        snip.set_targets([])
        assert snip.matches_target(evt_miss), "empty targets must accept all"
        print("  ✓ block filter accepts target block_id, rejects others")

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
    print("🧪 Hydra V15.2 — ws_sniper self-test")
    print("=" * 70)
    ek = sys.argv[1] if len(sys.argv) > 1 else "selftest-channel"
    sys.exit(asyncio.run(_selftest(ek)))
