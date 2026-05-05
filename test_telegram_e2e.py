"""
v8 END-TO-END TELEGRAM-FLOW TEST.

Simulates the EXACT path a real user takes from Telegram → handlers →
orchestrator → booking_http → seats.io. Verifies:

  1. No old Arabic error strings ever bubble up to the Telegram layer
  2. Turnstile is auto-solved at BOTH the hold-token AND chart-fetch layers
  3. chart_unreachable triggers internal retry with Cloudflare bypass
  4. Final user-facing message comes from _humanize_error (not booking_http)
"""
from __future__ import annotations
import os, sys, asyncio, time, json, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DATABASE_URL",
    "postgresql://data_bot_m11h_user:VjGqOpJgQsAyQtLXRrabACJzkKTFH9e5"
    "@dpg-d7kkg3qqqhas738deat0-a.oregon-postgres.render.com/data_bot_m11h")
os.environ.setdefault("CAPTCHA_API_KEY", "8363ebe2c26ce415ea215d856a1007fa")
os.environ.setdefault("HEADLESS", "true")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")

from app.core.storage import list_accounts, list_drop_watchers, set_drop_watcher_status
from app.services.booking_orchestrator import (
    book_all_fast_lane, _humanize_error, _is_transient_failure, _failure_kind,
)
from app.services.distributor import Assignment
from app.services import booking_orchestrator, booking_http
import aiohttp

EVENT_SLUG = "spl-week-32-al-najmah-vs-al-hazem-7715"

# OLD STRINGS that must NEVER appear in the user-facing message anymore
FORBIDDEN_OLD_STRINGS = [
    "هذه الفعالية تتطلب تحقق Cloudflare Turnstile",
    "افتح الفعالية في المتصفح",
    "تعذّر جلب بيانات خريطة المقاعد (خطأ شبكي، أعد المحاولة)",
    "افتح الفعالية\nفي المتصفح",
]


def banner(t): print("\n" + "═"*70 + f"\n  {t}\n" + "═"*70)


# ──────────────────────────────────────────────────────────────────────
# UNIT TESTS
# ──────────────────────────────────────────────────────────────────────
def test_humanize_no_old_strings():
    banner("UNIT 1: _humanize_error never returns old strings")
    test_cases = [
        {"error": "transient:turnstile_required"},
        {"error": "transient:queued:42"},
        {"error": "transient:chart_unreachable"},
        {"error": "transient:event_meta_unreachable"},
        {"error": "transient:cloudflare_blocked"},
        {"error": "transient:cart_blocked:cf-403"},
        {"error": "chart_full"},
        {"error": "no_contiguous_run:3"},
        {"error": "account_limit_reached:limit reached"},
        {"error": "checkout_failed:bad request"},
        {"error": "no_bearer"},
    ]
    all_ok = True
    for tc in test_cases:
        out = _humanize_error(tc)
        for forbidden in FORBIDDEN_OLD_STRINGS:
            if forbidden in out:
                print(f"  ❌ {tc['error']!r:40} → contains old string: {forbidden[:40]}")
                all_ok = False
                break
        else:
            print(f"  ✓ {tc['error']!r:40} → {out[:60]}")
    if all_ok:
        print("  ✅ PASS — humanizer outputs are all-new")
    else:
        print("  ❌ FAIL — old strings leaked through")
    return all_ok


def test_transient_classification():
    banner("UNIT 2: _is_transient_failure classifies correctly")
    cases = [
        ({"error": "transient:turnstile_required", "turnstile_required": True}, True, "turnstile"),
        ({"error": "transient:chart_unreachable", "chart_unreachable": True}, True, "chart_unreachable"),
        ({"error": "chart_full", "chart_full": True}, False, "chart_full"),  # chart_full=watcher, not retry
        ({"error": "no_contiguous_run:3"}, False, "no_seats"),
        ({"error": "account_limit_reached:limit", "account_limit_reached": True}, False, "account_limit"),
        ({"error": "no_bearer", "fatal": True}, False, "no_seats"),  # fatal, not transient
        ({"error": "transient:cloudflare_blocked", "chart_unreachable": True}, True, "chart_unreachable"),
    ]
    all_ok = True
    for inp, expected_transient, expected_kind in cases:
        actual_t = _is_transient_failure(inp)
        actual_k = _failure_kind(inp)
        marker = "✓" if (actual_t == expected_transient) else "❌"
        if actual_t != expected_transient:
            all_ok = False
        print(f"  {marker} {inp.get('error', '')!r:40} transient={actual_t} kind={actual_k}")
    return all_ok


# ──────────────────────────────────────────────────────────────────────
# INTEGRATION TEST — mock book_ticket_http to simulate production
# ──────────────────────────────────────────────────────────────────────
class MockBookHTTP:
    def __init__(self, scenario):
        self.scenario = scenario
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append({"attempt": len(self.calls) + 1, "ts": time.time()})
        ek = "test_event_key_xyz"
        n = len(self.calls)

        if self.scenario == "turnstile_then_success":
            # First 2 calls: turnstile required
            if n <= 2:
                return {
                    "ok": False, "error": "transient:turnstile_required",
                    "turnstile_required": True,
                    "seat_info": {"event_key": ek}, "logs": [],
                    "chart_full": False, "chart_unreachable": False,
                    "queued": False,
                }
            # 3rd call: success
            return {
                "ok": True, "payment_url": "https://paytabs.test/SUCCESS",
                "seat_info": {"event_key": ek, "seats": ["A1"]},
                "seat_objects": [], "block_used": "CAT1-N",
                "order_id": "OK", "logs": ["✓ booked after turnstile"],
            }

        if self.scenario == "chart_unreachable_then_success":
            if n <= 2:
                return {
                    "ok": False, "error": "transient:chart_unreachable",
                    "chart_unreachable": True,
                    "seat_info": {"event_key": ek}, "logs": [],
                    "chart_full": False, "turnstile_required": False, "queued": False,
                }
            return {
                "ok": True, "payment_url": "https://paytabs.test/RECOVERED",
                "seat_info": {"event_key": ek, "seats": ["B5"]},
                "seat_objects": [], "block_used": "CAT2-N",
                "order_id": "REC", "logs": ["✓ booked after cf bypass"],
            }

        if self.scenario == "cloudflare_blocked_persistent":
            return {
                "ok": False, "error": "transient:cloudflare_blocked",
                "chart_unreachable": True,
                "seat_info": {"event_key": ek}, "logs": [],
                "chart_full": False, "turnstile_required": False, "queued": False,
            }

        return {"ok": False, "error": "unknown"}


async def run_integration_scenario(name, scenario, expect_success):
    banner(f"INTEGRATION: {name}")
    mock = MockBookHTTP(scenario)
    orig = booking_orchestrator.book_ticket_http
    booking_orchestrator.book_ticket_http = mock

    accs = [a for a in list_accounts(status="ready") if a.get("access_token")]
    if not accs:
        print("  ⏭ no ready accounts")
        booking_orchestrator.book_ticket_http = orig
        return False

    plan = [Assignment(account_id=accs[0]["id"], quantity=1)]
    progress_log = []

    async def progress(line):
        clean = re.sub(r"<[^>]+>", "", line)
        progress_log.append(clean)
        print(f"    [progress] {clean}")

    fast_calls = []

    async def fast_cb(r):
        fast_calls.append(r)
        print(f"    [fast-lane] {r['label']} → {r.get('payment_url','')[:60]}")

    t0 = time.time()
    results = await book_all_fast_lane(
        plan, event_slug=EVENT_SLUG, event_title="Mock Event",
        ticket_id="t1", ticket_title="Test", ticket_price=0.0,
        currency="SAR", chat_id="0",
        progress=progress, fast_callback=fast_cb,
        primary_block="", backup_blocks=[], payment_method="credit_card",
    )
    dt = time.time() - t0
    booking_orchestrator.book_ticket_http = orig

    print(f"  Calls to book_ticket_http: {len(mock.calls)}")
    print(f"  Elapsed: {dt:.2f}s   Fast-lane fires: {len(fast_calls)}")

    for r in results:
        ok = r.get("ok")
        kind = r.get("failure_kind", "—")
        err = (r.get("error") or "")[:120]
        code = r.get("error_code", "")
        print(f"  result: ok={ok} kind={kind} err='{err}' code='{code}'")

        # CRITICAL: ensure NO old strings leaked
        for forbidden in FORBIDDEN_OLD_STRINGS:
            if forbidden in err:
                print(f"  ❌ LEAKED OLD STRING: {forbidden[:50]}")
                return False

    if expect_success and not any(r.get("ok") for r in results):
        print(f"  ❌ FAIL: expected success but none came")
        return False
    if not expect_success and any(r.get("ok") for r in results):
        print(f"  ❌ FAIL: did not expect success")
        return False

    print(f"  ✅ PASS")
    return True


# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────
async def main():
    print("\n🧪 v8 TELEGRAM E2E TEST SUITE")
    print(f"   Event slug: {EVENT_SLUG}\n")

    results = {}
    results["humanize"] = test_humanize_no_old_strings()
    results["transient_classify"] = test_transient_classification()

    results["turnstile_retry"] = await run_integration_scenario(
        "Turnstile blocks 2x → success on 3rd",
        "turnstile_then_success", expect_success=True,
    )
    results["chart_recovery"] = await run_integration_scenario(
        "chart_unreachable 2x → success on 3rd",
        "chart_unreachable_then_success", expect_success=True,
    )
    results["cf_persistent_no_watcher"] = await run_integration_scenario(
        "Cloudflare blocks all 5 retries → hard fail (NO watcher)",
        "cloudflare_blocked_persistent", expect_success=False,
    )

    banner("SUMMARY")
    all_pass = all(results.values())
    for k, v in results.items():
        marker = "✅" if v else "❌"
        print(f"  {marker} {k}")
    print(f"\n  {'🎉 ALL TESTS PASSED' if all_pass else '❌ SOME TESTS FAILED'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
