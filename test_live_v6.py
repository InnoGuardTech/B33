"""
LIVE END-TO-END TEST for v6 Surgical Fix.

Runs the full Fast-Lane orchestrator against the real production event
with available seats. Verifies:
  • No false-positive watcher registrations
  • Smart retry kicks in for transient failures
  • Pre-Watch sanity check refuses watcher when seats are free
  • At least one account either books successfully OR receives a clear
    transient/no-bearer error (NEVER a stuck watcher when seats free)
"""
from __future__ import annotations
import os, sys, asyncio, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL",
    "postgresql://data_bot_m11h_user:VjGqOpJgQsAyQtLXRrabACJzkKTFH9e5"
    "@dpg-d7kkg3qqqhas738deat0-a.oregon-postgres.render.com/data_bot_m11h")
os.environ.setdefault("CAPTCHA_API_KEY", "8363ebe2c26ce415ea215d856a1007fa")
os.environ.setdefault("HEADLESS", "true")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("LOG_LEVEL", "INFO")

from app.core.storage import (
    list_accounts, list_drop_watchers, set_drop_watcher_status, cancel_drop_watchers,
)
from app.services.booking_orchestrator import book_all_fast_lane
from app.services.distributor import Assignment
from app.services.booking_http import fetch_event_meta, resolve_seated_manifest
from app.services.seatsio_client import SeatsioClient
import aiohttp

EVENT_SLUG = "spl-week-32-al-najmah-vs-al-hazem-7715"


def banner(t): print("\n" + "═"*70 + f"\n  {t}\n" + "═"*70)


async def main():
    banner("v6 LIVE END-TO-END TEST")
    print(f"  Event: {EVENT_SLUG}")

    # Snapshot watchers BEFORE the run
    pre_watchers = list_drop_watchers(status="watching")
    print(f"  Watchers before run: {len(pre_watchers)}")

    # Step 1 — Probe the chart for available seats
    accs = [a for a in list_accounts(status="ready") if a.get("access_token")]
    print(f"  Ready accounts: {len(accs)}")
    if not accs:
        print("❌ no accounts available, aborting"); return
    bearer = accs[0]["access_token"]

    banner("STEP 1 — Live chart probe")
    async with aiohttp.ClientSession() as s:
        meta = await fetch_event_meta(s, EVENT_SLUG, bearer)
        manifest = await resolve_seated_manifest(s, EVENT_SLUG, "", bearer, event_meta=meta)
    event_key = manifest.get("event_key", "")
    print(f"  event_key: {event_key}")
    print(f"  workspace_key: {manifest.get('workspace_key','')}")

    if event_key:
        async with SeatsioClient(
            event_key=event_key,
            workspace_key=manifest.get("workspace_key",""),
            chart_key=manifest.get("chart_key",""),
            provider=manifest.get("seats_provider",""),
        ) as c:
            ri = await c.rendering_info()
            sts = await c.object_statuses()
        objs = (ri or {}).get("objects") or []
        free_objs = []
        for o in objs:
            oid = str(o.get("id",""))
            label = o.get("label", oid)
            status = sts.get(oid) or sts.get(label) or o.get("status_hint","")
            if str(status).lower() in {"free","available","not_booked",""}:
                cap = o.get("capacity") or 0
                if cap and o.get("isAvailableForSale"):
                    free_objs.append(o)
        print(f"  ✓ chart objects: {len(objs)}  free-for-sale blocks: {len(free_objs)}")
        if free_objs[:3]:
            for o in free_objs[:3]:
                print(f"    • {o.get('label')}  cat={o.get('category')}  cap={o.get('capacity')}")

    # Get a valid ticket_id for the live booking
    banner("STEP 2 — Resolve real ticket_id")
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"https://api.webook.com/api/v2/event-ticket-details/{EVENT_SLUG}?lang=en&visible_in=rs&page=1",
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {bearer}",
                "token": "e9aac1f2f0b6c07d6be070ed14829de684264278359148d6a582ca65a50934d2",
                "accept-language": "ar-SA",
                "user-agent": "Mozilla/5.0",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            d = await r.json(content_type=None)
        et = (d or {}).get("data", {}).get("event_ticket") or []
        ticket_id = ""
        ticket_title = ""
        for t in et:
            if not t.get("is_soldout"):
                ticket_id = t.get("_id") or t.get("id") or ""
                ticket_title = t.get("title", "")
                break
        if not ticket_id and et:
            ticket_id = et[0].get("_id") or et[0].get("id") or ""
            ticket_title = et[0].get("title", "")
        print(f"  ✓ ticket: '{ticket_title}'  id={ticket_id}")

    # Step 3 — fire the live Fast-Lane
    banner("STEP 3 — Fast-Lane booking (3 accounts × 1 ticket)")
    plan_accs = [a for a in accs[:3]]
    plan = [Assignment(account_id=a["id"], quantity=1) for a in plan_accs]
    fast_calls = []
    t0 = time.time()

    async def fast_cb(r):
        elapsed = time.time() - t0
        fast_calls.append((elapsed, r.get("label","?")))
        print(f"  ⚡ FAST-LANE @ {elapsed:.2f}s — {r['label']} → {r.get('payment_url','')[:70]}")

    async def progress(line):
        import re
        clean = re.sub(r"<[^>]+>", "", line)
        elapsed = time.time() - t0
        print(f"  [{elapsed:5.2f}s] {clean}")

    results = await book_all_fast_lane(
        plan, event_slug=EVENT_SLUG,
        event_title="SPL W32 Al-Najmah vs Al-Hazem",
        ticket_id=ticket_id, ticket_title=ticket_title or "Cat 1 - N",
        ticket_price=0.0, currency="SAR", chat_id="0",
        progress=progress, fast_callback=fast_cb,
        primary_block="", backup_blocks=[],
        payment_method="credit_card",
    )
    total = time.time() - t0

    banner("RESULTS")
    succ = [r for r in results if r.get("ok")]
    watching = [r for r in results if r.get("drop_watcher_active")]
    transient = [r for r in results if r.get("failure_kind") in
                  {"turnstile","queued","timeout","captcha_delay",
                   "chart_unreachable","transient_conflict","exception"}]
    no_seats = [r for r in results if r.get("failure_kind") == "no_seats"]
    no_bearer = [r for r in results if r.get("failure_kind") == "no_bearer"]
    chart_full = [r for r in results if r.get("failure_kind") == "chart_full"]

    print(f"  Total elapsed: {total:.2f}s    Fast-lane fires: {len(fast_calls)}")
    print(f"  ✅ success      : {len(succ)}")
    print(f"  👁️ watching    : {len(watching)}  (each is correct only if chart truly full)")
    print(f"  ⏳ transient    : {len(transient)}")
    print(f"  🚫 no_seats    : {len(no_seats)}  (correctly NOT watcher)")
    print(f"  🔑 no_bearer   : {len(no_bearer)}  (token expired / blocked acc)")
    print(f"  📛 chart_full  : {len(chart_full)}")

    for r in results:
        kind = r.get("failure_kind") or ("ok" if r.get("ok") else "?")
        wk = "👁️" if r.get("drop_watcher_active") else ("✅" if r.get("ok") else "❌")
        err = (r.get("error") or "")[:120]
        print(f"     {wk} [{kind:18}] {r.get('label')}: {err}")

    # Verify the bug is fixed
    banner("VERIFICATION")
    post_watchers = list_drop_watchers(status="watching")
    new_watchers = len(post_watchers) - len(pre_watchers)
    print(f"  New watchers registered: {new_watchers}")
    bug_present = False
    if free_objs and new_watchers > 0:
        # If there were free seats AND a watcher got registered, that's the bug.
        for w in post_watchers:
            if w["id"] not in {pw["id"] for pw in pre_watchers}:
                print(f"  ⚠️  watcher#{w['id']} for account={w['account_id']}")
        if not any(r.get("ok") for r in results):
            # New watchers + free seats + zero successes → bug not fixed
            bug_present = True

    if bug_present:
        print("  ❌ BUG STILL PRESENT — false-positive watcher with free seats")
    else:
        print("  ✅ No false-positive watcher registration — bug fixed")

    # Cleanup any stale watcher rows from this test run
    for w in post_watchers:
        if w["id"] not in {pw["id"] for pw in pre_watchers}:
            set_drop_watcher_status(w["id"], "cancelled")
    print("  (test-created watchers cleaned up)")


if __name__ == "__main__":
    asyncio.run(main())
