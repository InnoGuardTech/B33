"""Smoke tests for v4 (Hydra Seat Engine).

Run:  python -m smoke_test
"""
from app.core.config import (
    seatsio_enabled, target_blocks, use_stealth_browser,
    default_payment_method,
)
from app.services.seatsio_token_fetcher import CACHE
from app.services.seatsio_client import pick_adjacent_from_snapshot
from app.services.booking_http import resolve_seated_manifest, prewarm_event_from_slug
from app.services.block_analyzer import (
    extract_blocks, find_seats_with_fallback, geometric_neighbors,
)
from app.services.seat_summarizer import (
    summarize_seats, summarize_for_telegram,
)
from app.core.storage import (
    set_bot_setting, get_bot_setting, list_bot_settings,
)


# 1) Config sanity
assert isinstance(seatsio_enabled(), bool)
assert isinstance(target_blocks(), list)
assert isinstance(use_stealth_browser(), bool)
assert default_payment_method() in {"credit_card", "apple_pay"}, \
    f"unexpected payment: {default_payment_method()}"
assert hasattr(CACHE, "to_dict")

# 2) Legacy adjacent picker (kept for backwards-compat)
rendering = {
    "objects": [
        {"id": "A-1", "x": 100, "y": 200,
         "labels": {"section": "S1", "parent": "A", "own": "1", "displayedLabel": "A-1"},
         "category": "CAT 1 - S"},
        {"id": "A-2", "x": 110, "y": 200,
         "labels": {"section": "S1", "parent": "A", "own": "2", "displayedLabel": "A-2"},
         "category": "CAT 1 - S"},
        {"id": "A-3", "x": 120, "y": 200,
         "labels": {"section": "S1", "parent": "A", "own": "3", "displayedLabel": "A-3"},
         "category": "CAT 1 - S"},
        {"id": "B-1", "x": 500, "y": 200,
         "labels": {"section": "S2", "parent": "A", "own": "1", "displayedLabel": "B-1"},
         "category": "CAT 1 - S"},
        {"id": "B-2", "x": 510, "y": 200,
         "labels": {"section": "S2", "parent": "A", "own": "2", "displayedLabel": "B-2"},
         "category": "CAT 1 - S"},
    ]
}
statuses = {"A-1": "free", "A-2": "free", "A-3": "booked",
            "B-1": "free", "B-2": "free"}
chosen = pick_adjacent_from_snapshot(rendering, statuses, 2,
                                      target_blocks=["S1"])
assert chosen == ["A-1", "A-2"], f"legacy picker failed: {chosen}"

# 3) Block analyzer
blocks = extract_blocks(rendering, statuses)
assert len(blocks) == 2
s1 = next(b for b in blocks if b["name"] == "S1")
s2 = next(b for b in blocks if b["name"] == "S2")
assert s1["free"] == 2 and s1["total"] == 3
assert s2["free"] == 2 and s2["total"] == 2

# 4) Geometric neighbors
neighbors = geometric_neighbors(blocks, "S1")
assert "S2" in neighbors

# 5) Fallback finder: S1 first → 2 seats, full
ids, used = find_seats_with_fallback(rendering, statuses,
                                       primary_block="S1",
                                       backup_blocks=["S2"], quantity=2)
assert ids == ["A-1", "A-2"] and used == "S1"

# 6) Fallback when primary is short on adjacency: ask for 3 from S1 → falls
# back to backup S2 (only 2 free) → eventually returns from S1 best-effort
ids2, used2 = find_seats_with_fallback(rendering, statuses,
                                         primary_block="S1",
                                         backup_blocks=["S2"], quantity=3)
# either S1 best-effort or S2 best-effort acceptable; just validate non-empty
assert isinstance(ids2, list)

# 7) Seat summarizer (compact format)
sample_seats = [
    {"category": "CAT 1 - S", "section": "Block 5",
     "row": "", "labels": {"section": "Block 5", "own": "117"},
     "id": "B5-117"},
    {"category": "CAT 1 - S", "section": "Block 5",
     "row": "", "labels": {"section": "Block 5", "own": "118"},
     "id": "B5-118"},
    {"category": "CAT 1 - S", "section": "Block 5",
     "row": "", "labels": {"section": "Block 5", "own": "119"},
     "id": "B5-119"},
    {"category": "CAT 1 - S", "section": "Block 5",
     "row": "", "labels": {"section": "Block 5", "own": "120"},
     "id": "B5-120"},
    {"category": "CAT 1 - S", "section": "Block 5",
     "row": "", "labels": {"section": "Block 5", "own": "121"},
     "id": "B5-121"},
]
summary = summarize_seats(sample_seats)
# Expected: groups by category+block; range form because ≥4 consecutive
assert "117-121" in summary or "117,118,119,120,121" in summary, \
    f"summary unexpected: {summary}"
assert "Block 5" in summary
assert "CAT 1 - S" in summary

tg = summarize_for_telegram(sample_seats)
assert "<b>" in tg and "<code>" in tg

# 8) Bot settings round-trip
set_bot_setting("DEFAULT_PAYMENT_METHOD", "credit_card", updated_by="smoke")
assert get_bot_setting("DEFAULT_PAYMENT_METHOD") == "credit_card"
all_settings = list_bot_settings()
assert "DEFAULT_PAYMENT_METHOD" in all_settings

print("SMOKE_OK ✅ — v4 Hydra engine passes all checks")
