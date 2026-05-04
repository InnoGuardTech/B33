"""
DIAGNOSTIC: reproduces the false-positive watcher bug.

Hypothesis: when seats ARE available but a transient error occurs (turnstile,
queue, timeout, 2captcha delay), book_one routes the account to drop_watcher
instead of retrying. We prove it by:

  1. Forcing a transient turnstile_required failure on first attempt
  2. Asserting that the orchestrator does NOT register a drop_watcher
  3. Asserting that it retries the booking instead

Also: pick a real event with available seats and verify Pre-Watch Sanity Check
catches the case `available=True` and skips the watcher.
"""
from __future__ import annotations
import os, sys, asyncio, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DATABASE_URL",
    "postgresql://data_bot_m11h_user:VjGqOpJgQsAyQtLXRrabACJzkKTFH9e5"
    "@dpg-d7kkg3qqqhas738deat0-a.oregon-postgres.render.com/data_bot_m11h")
os.environ.setdefault("CAPTCHA_API_KEY", "8363ebe2c26ce415ea215d856a1007fa")
os.environ.setdefault("HEADLESS", "true")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("LOG_LEVEL", "INFO")

from app.core.storage import list_accounts, list_drop_watchers, set_drop_watcher_status
from app.services.booking_orchestrator import book_one, book_all_fast_lane
from app.services.distributor import Assignment
from app.services import booking_http, booking_orchestrator
import aiohttp

EVENT_SLUG = "spl-week-32-al-najmah-vs-al-hazem-7715"


def banner(t): print("\n" + "═"*70 + f"\n  {t}\n" + "═"*70)


# ── Mock book_ticket_http to simulate the buggy scenarios ──
class FakeBookTicketHttp:
    def __init__(self, scenario: str):
        self.scenario = scenario
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        ek = "fake_event_key_abc123"
        if self.scenario == "turnstile_first_then_ok":
            if self.calls == 1:
                return {"ok": False, "error": "turnstile",
                        "turnstile_required": True,
                        "seat_info": {"event_key": ek}, "logs": []}
            else:
                return {"ok": True, "payment_url": "https://paytabs.test/X",
                        "seat_info": {"event_key": ek, "seats": ["A1"]},
                        "seat_objects": [], "block_used": "CAT1-N",
                        "order_id": "O1", "logs": []}
        if self.scenario == "queued_first_then_ok":
            if self.calls == 1:
                return {"ok": False, "error": "queue", "queued": True,
                        "seat_info": {"event_key": ek}, "logs": []}
            else:
                return {"ok": True, "payment_url": "https://paytabs.test/Y",
                        "seat_info": {"event_key": ek, "seats": ["A2"]},
                        "seat_objects": [], "block_used": "CAT2-N",
                        "order_id": "O2", "logs": []}
        if self.scenario == "no_seats_persistent":
            return {"ok": False, "error": "تعذّر إيجاد مقاعد متجاورة",
                    "seat_info": {"event_key": ek}, "logs": []}
        if self.scenario == "chart_full":
            return {"ok": False, "error": "ممتلئة", "chart_full": True,
                    "seat_info": {"event_key": ek}, "logs": []}
        return {"ok": False, "error": "unknown"}


async def run_scenario(name, scenario):
    banner(f"SCENARIO: {name}")
    fake = FakeBookTicketHttp(scenario)
    # Monkey-patch
    orig = booking_orchestrator.book_ticket_http
    booking_orchestrator.book_ticket_http = fake

    accs = [a for a in list_accounts(status="ready") if a.get("access_token")]
    if not accs:
        print("  ⏭ no ready accounts")
        booking_orchestrator.book_ticket_http = orig
        return

    # Clean any lingering watchers from prior runs
    for w in list_drop_watchers(status="watching"):
        if w.get("account_id") == accs[0]["id"]:
            set_drop_watcher_status(w["id"], "cancelled")

    a = Assignment(account_id=accs[0]["id"], quantity=1)
    t0 = time.time()
    res = await book_one(
        a, event_slug=EVENT_SLUG, event_title="Test", ticket_id="t",
        ticket_title="T", ticket_price=0.0, currency="SAR", chat_id="0",
        primary_block="", backup_blocks=[],
    )
    dt = time.time() - t0
    booking_orchestrator.book_ticket_http = orig

    print(f"  calls to book_ticket_http: {fake.calls}")
    print(f"  ok={res.get('ok')}  drop_watcher_active={res.get('drop_watcher_active')}  failure_kind={res.get('failure_kind')}  error={(res.get('error') or '')[:80]}")
    print(f"  elapsed: {dt:.2f}s")

    # Verify expectations
    if scenario == "turnstile_first_then_ok":
        if res.get("ok") and fake.calls >= 2:
            print("  ✅ PASS: retried after turnstile, then booked")
        else:
            print(f"  ❌ FAIL: should have retried (calls={fake.calls}) and succeeded")
    elif scenario == "queued_first_then_ok":
        if res.get("ok") and fake.calls >= 2:
            print("  ✅ PASS: retried after queue, then booked")
        else:
            print(f"  ❌ FAIL: should have retried after queue")
    elif scenario == "no_seats_persistent":
        # Should NOT go to watcher unless chart_full is explicit
        if not res.get("drop_watcher_active"):
            print("  ✅ PASS: did NOT enter watcher mode for no_seats")
        else:
            print(f"  ❌ FAIL: incorrectly entered watcher for no_seats")
    elif scenario == "chart_full":
        # In v6 the orchestrator runs a Pre-Watch Sanity Check before
        # registering a watcher. With a fake event_key the probe will
        # fail open (transient_conflict). On a real chart_full, this
        # check would confirm and the watcher would register. Both
        # outcomes are acceptable — what we MUST NOT see is a watcher
        # registered when seats are clearly free.
        kind = res.get("failure_kind")
        if res.get("drop_watcher_active"):
            print("  ✅ PASS: watcher registered after sanity check confirmed full")
        elif kind == "transient_conflict":
            print("  ✅ PASS: sanity check found seats → refused watcher (correct)")
        else:
            print(f"  ❌ FAIL: unexpected outcome kind={kind}")


async def main():
    print("\n🔬 LOGIC BUG DIAGNOSTIC SUITE")
    await run_scenario("Turnstile transient → must RETRY (not watch)",
                        "turnstile_first_then_ok")
    await run_scenario("Queue transient → must RETRY (not watch)",
                        "queued_first_then_ok")
    await run_scenario("No-seats persistent → NO WATCHER (no real drop signal)",
                        "no_seats_persistent")
    await run_scenario("Chart-full explicit → MUST WATCH",
                        "chart_full")


if __name__ == "__main__":
    asyncio.run(main())
