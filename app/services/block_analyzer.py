"""
Block-level analytics for seats.io rendering_info.

Capabilities:
  • extract_blocks()        — list every block (section/zone) with its center
                              coordinates + free/total counts
  • adjacent_seats_in_block()→ find N consecutive free seats inside a block
  • cross_account_adjacent()→ find N×K seats spread across accounts that all
                              sit on the same row, contiguously
  • geometric_neighbors()   → return blocks ordered by Euclidean distance
                              from a reference block (used when the user's
                              primary + backup blocks are full)
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional


_NUM_RE = re.compile(r"(\d+)")


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    s = str(v)
    m = _NUM_RE.search(s)
    return int(m.group(1)) if m else None


def _walk_objects(rendering_info: Any) -> list[dict]:
    """Best-effort extractor for SeatCloud rendering_info shape."""
    if isinstance(rendering_info, dict):
        for key in ("objects", "items", "selectableObjects", "renderableObjects"):
            v = rendering_info.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in rendering_info.values():
            got = _walk_objects(v)
            if got:
                return got
    elif isinstance(rendering_info, list):
        if rendering_info and isinstance(rendering_info[0], dict):
            sample = rendering_info[0]
            if any(k in sample for k in ("id", "objectId", "labels")):
                return rendering_info
        for v in rendering_info:
            got = _walk_objects(v)
            if got:
                return got
    return []


def _is_free(status: str) -> bool:
    return str(status or "").strip().lower() in {
        "free", "available", "not_booked", "not-booked"
    }


# ════════════════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════════════════
def extract_blocks(rendering_info: Any,
                   statuses: dict[str, str] | None = None) -> list[dict]:
    """Aggregate seats/areas into blocks.

    Supports TWO chart shapes:
      1. Legacy seats.io — one object per seat. We aggregate by section.
      2. seats_planner   — one object per area (block) with capacity.
         Detected via `obj['itemType'] == 'generalAdmission'` or presence
         of `obj['capacity']` field.

    Returns:
        [
          {"name": "S1", "center_x": 500.0, "center_y": 320.5,
           "free": 12, "total": 50, "category": "CAT 1 - S",
           "category_key": 9, "item_type": "generalAdmission",
           "id": "21"},
          ...
        ]
    """
    statuses = statuses or {}
    objs = _walk_objects(rendering_info)
    if not objs:
        return []

    # Detect 'area-based' charts (seats_planner) — each object IS a block
    is_area_based = any(
        isinstance(o, dict) and (
            o.get("itemType") == "generalAdmission"
            or "capacity" in o
        )
        for o in objs
    )

    if is_area_based:
        out: list[dict] = []
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            name = (obj.get("label") or obj.get("displayedLabel")
                    or obj.get("section") or obj.get("name") or "").strip()
            if not name:
                continue
            oid = str(obj.get("id") or obj.get("objectId") or name)
            category = (obj.get("category") or obj.get("ticketType") or "").strip()
            cat_key = obj.get("categoryKey")
            capacity = int(obj.get("capacity") or 0)
            avail = obj.get("isAvailableForSale")
            status = statuses.get(oid) or statuses.get(name) or ""

            # Determine free/total:
            # Check if object is selectable/viewable to avoid hidden seats
            is_selectable = obj.get("selectable", True) is not False
            is_viewable = obj.get("viewable", True) is not False
            
            total = capacity if capacity > 0 else -1
            if not is_selectable or not is_viewable:
                free = 0
            elif status:
                # status takes priority when present
                free = total if _is_free(status) else 0
            elif avail is True:
                free = total
            elif avail is False:
                free = 0
            else:
                free = -1   # unknown

            x = obj.get("x") or obj.get("cx")
            y = obj.get("y") or obj.get("cy")
            if (x is None or y is None) and obj.get("center"):
                c = obj["center"]
                x = x if x is not None else c.get("x")
                y = y if y is not None else c.get("y")

            try:
                cx = float(x) if x is not None else 0.0
                cy = float(y) if y is not None else 0.0
            except (TypeError, ValueError):
                cx, cy = 0.0, 0.0

            out.append({
                "name": name,
                "id": oid,
                "center_x": cx,
                "center_y": cy,
                "free": free,
                "total": total,
                "category": category,
                "category_key": cat_key,
                "item_type": obj.get("itemType") or "generalAdmission",
                "min_occupancy": int(obj.get("minOccupancy") or 1),
                "is_available_for_sale": avail,
            })
        out.sort(key=lambda d: (_to_int(d["name"]) or 0, d["name"]))
        return out

    # Legacy: per-seat objects → aggregate by section
    by_block: dict[str, dict[str, Any]] = {}
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        labels = obj.get("labels") or {}
        section = (labels.get("section") or obj.get("section")
                   or obj.get("category") or obj.get("ticketType") or "").strip()
        if not section:
            continue
        oid = obj.get("id") or obj.get("objectId")
        label = (labels.get("displayedLabel") or obj.get("label")
                 or obj.get("displayedLabel") or oid or "")
        status = statuses.get(str(label)) or statuses.get(str(oid)) or "free"
        category = (obj.get("category") or obj.get("categoryKey")
                    or obj.get("ticketType") or "").strip()

        x = obj.get("x") or obj.get("cx")
        y = obj.get("y") or obj.get("cy")
        if (x is None or y is None) and "center" in obj:
            c = obj["center"] or {}
            x = x if x is not None else c.get("x")
            y = y if y is not None else c.get("y")

        b = by_block.setdefault(section, {
            "name": section,
            "free": 0,
            "total": 0,
            "_xs": [],
            "_ys": [],
            "_cats": {},
        })
        b["total"] += 1
        if _is_free(status):
            b["free"] += 1
        if x is not None:
            try:
                b["_xs"].append(float(x))
            except (TypeError, ValueError):
                pass
        if y is not None:
            try:
                b["_ys"].append(float(y))
            except (TypeError, ValueError):
                pass
        if category:
            b["_cats"][category] = b["_cats"].get(category, 0) + 1

    out2: list[dict] = []
    for name, b in by_block.items():
        cx = sum(b["_xs"]) / len(b["_xs"]) if b["_xs"] else 0.0
        cy = sum(b["_ys"]) / len(b["_ys"]) if b["_ys"] else 0.0
        cat = ""
        if b["_cats"]:
            cat = max(b["_cats"].items(), key=lambda kv: kv[1])[0]
        out2.append({
            "name": name,
            "id": name,
            "center_x": cx,
            "center_y": cy,
            "free": b["free"],
            "total": b["total"],
            "category": cat,
            "category_key": None,
            "item_type": "seat",
        })
    out2.sort(key=lambda d: (_to_int(d["name"]) or 0, d["name"]))
    return out2


def geometric_neighbors(blocks: list[dict], reference: str,
                        exclude: list[str] | None = None,
                        limit: int = 8,
                        same_category_only: bool = False) -> list[str]:
    """Return block names ordered by Euclidean distance from `reference`.

    A block is considered a candidate when free > 0 OR free == -1 (unknown).
    `same_category_only`: when True, only return neighbors that share the
    reference block's category (useful so we don't suggest VIP when the
    user picked a Silver block).
    """
    exclude = set(exclude or [])
    exclude.add(reference)
    ref = next((b for b in blocks if b["name"] == reference), None)
    if not ref:
        return []
    rx, ry = ref["center_x"], ref["center_y"]
    ref_cat = (ref.get("category") or "").strip()
    candidates = []
    for b in blocks:
        if b["name"] in exclude:
            continue
        free = b.get("free", 0)
        if free == 0:        # known empty
            continue
        if same_category_only and ref_cat:
            if (b.get("category") or "").strip() != ref_cat:
                continue
        candidates.append((
            b["name"],
            math.hypot(b["center_x"] - rx, b["center_y"] - ry),
            free if free >= 0 else 1,    # treat unknown as 1
        ))
    candidates.sort(key=lambda t: (t[1], -t[2]))
    return [c[0] for c in candidates[:limit]]


def adjacent_seats_in_block(rendering_info: Any,
                             statuses: dict[str, str],
                             block_name: str,
                             quantity: int) -> list[str]:
    """Find `quantity` consecutive free seats within `block_name`.

    For seats_planner area-based charts (generalAdmission), there are no
    individual seat objects — instead each block has a `capacity`. In
    that case we return [block_id] * quantity (the booking will pass the
    block id as `selected_seats` and webook expands it server-side).

    Returns the seat IDs in row-order, or [] if not possible.
    """
    objs = _walk_objects(rendering_info)
    if not objs:
        return []

    # ── seats_planner path: each object IS a block (area) ──
    target_obj = None
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        name = (obj.get("label") or obj.get("displayedLabel")
                or obj.get("section") or obj.get("name") or "").strip()
        if name == block_name and (
            obj.get("itemType") == "generalAdmission"
            or "capacity" in obj
        ):
            target_obj = obj
            break

    if target_obj is not None:
        # Block is unavailable for sale → cannot pick from it
        avail = target_obj.get("isAvailableForSale")
        if avail is False:
            return []
        oid = str(target_obj.get("id") or target_obj.get("objectId")
                  or block_name)
        capacity = int(target_obj.get("capacity") or 0)
        if capacity > 0 and quantity > capacity:
            return []
        # Status check (live data overrides defaults if present)
        status = statuses.get(oid) or statuses.get(block_name) or ""
        if status and not _is_free(status):
            return []
        # For generalAdmission we book by block-id, repeated `quantity` times.
        # The actual seats are picked by the venue server.
        return [oid] * quantity

    # ── legacy seats.io path: per-seat objects ──
    free_in_block: list[dict] = []
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        labels = obj.get("labels") or {}
        section = (labels.get("section") or obj.get("section") or "").strip()
        if section != block_name:
            continue
        oid = obj.get("id") or obj.get("objectId")
        label = (labels.get("displayedLabel") or obj.get("label") or oid or "")
        status = statuses.get(str(label)) or statuses.get(str(oid)) or "free"
        if not _is_free(status):
            continue
        row = (labels.get("parent") or obj.get("row") or "").strip()
        seat = (labels.get("own") or obj.get("seat") or "").strip()
        seat_no = _to_int(seat) or _to_int(label)
        free_in_block.append({
            "id": str(oid or label),
            "row": row,
            "seat_no": seat_no,
            "label": str(label),
        })

    if len(free_in_block) < quantity:
        return []

    by_row: dict[str, list[dict]] = {}
    for s in free_in_block:
        by_row.setdefault(s["row"], []).append(s)

    for row, arr in by_row.items():
        arr.sort(key=lambda x: (x["seat_no"] is None, x["seat_no"] or 10**9))
        if len(arr) < quantity:
            continue
        for i in range(0, len(arr) - quantity + 1):
            window = arr[i:i + quantity]
            nums = [w["seat_no"] for w in window]
            if all(n is not None for n in nums) and \
               all(nums[j] == nums[j - 1] + 1 for j in range(1, len(nums))):
                return [w["id"] for w in window]

    return [s["id"] for s in free_in_block[:quantity]]


def cross_account_adjacent_block(rendering_info: Any,
                                  statuses: dict[str, str],
                                  block_name: str,
                                  total_qty: int,
                                  per_account: int) -> list[list[str]]:
    """Try to grab total_qty seats in the same block, contiguously,
    then split into chunks of `per_account` for each account.

    Returns: list-of-lists (one per account) or [] if impossible.
    """
    seats = adjacent_seats_in_block(rendering_info, statuses,
                                     block_name, total_qty)
    if not seats:
        return []
    # Slice into per-account chunks while preserving adjacency order
    chunks = [seats[i:i + per_account]
              for i in range(0, len(seats), per_account)]
    if any(len(c) != per_account for c in chunks):
        return []
    return chunks


def find_seats_with_fallback(rendering_info: Any,
                              statuses: dict[str, str],
                              primary_block: str,
                              backup_blocks: list[str],
                              quantity: int,
                              *,
                              expand_geometric: bool = True,
                              expand_limit: int = 8) -> tuple[list[str], str]:
    """High-level finder used by the booking engine.

    Returns (seat_ids, block_used). If nothing is found anywhere, returns
    ([], "") and the caller should distinguish between two cases:
      - chart fully sold out      → engage drop-watcher
      - chart unreachable / empty → transient error, retry/fallback

    Order:
      1. primary_block
      2. backup_blocks (in user order)
      3. geometric neighbors (same category preferred) — only if
         expand_geometric=True AND we have at least one ref block
      4. AUTO-PICK: any free block on the chart, sorted by capacity desc
         (used when the user did NOT choose any block at all)
    """
    if primary_block:
        ids = adjacent_seats_in_block(rendering_info, statuses,
                                       primary_block, quantity)
        if ids:
            return ids, primary_block

    for blk in backup_blocks:
        ids = adjacent_seats_in_block(rendering_info, statuses,
                                       blk, quantity)
        if ids:
            return ids, blk

    if not expand_geometric:
        return [], ""

    blocks = extract_blocks(rendering_info, statuses)
    seen = set([primary_block] + list(backup_blocks))
    refs = [primary_block] + list(backup_blocks)
    # Prefer same-category neighbors first, then any
    for same_cat_pref in (True, False):
        for ref in refs:
            if not ref:
                continue
            for nb in geometric_neighbors(
                blocks, ref, exclude=list(seen),
                limit=expand_limit, same_category_only=same_cat_pref,
            ):
                ids = adjacent_seats_in_block(rendering_info, statuses,
                                               nb, quantity)
                if ids:
                    return ids, nb
                seen.add(nb)

    # ──── AUTO-PICK FALLBACK (v7): the user didn't choose any block, OR ────
    # ──── their chosen blocks plus all geometric neighbors are full.   ────
    # Try EVERY free block on the chart, sorted by descending free capacity
    # so we prefer the spaces with the most availability (faster checkout).
    candidate_blocks = sorted(
        [b for b in blocks if b.get("name") and b.get("name") not in seen
         and b.get("is_available_for_sale") is not False
         and ((b.get("free") or 0) >= quantity
              or (b.get("free") or 0) < 0)],   # -1 = unknown, treat as available
        key=lambda b: -(b.get("free") or 0),
    )
    for b in candidate_blocks:
        nb = b.get("name")
        ids = adjacent_seats_in_block(rendering_info, statuses, nb, quantity)
        if ids:
            return ids, nb
        seen.add(nb)

    return [], ""


def chart_is_sold_out(rendering_info: Any,
                      statuses: dict[str, str] | None = None) -> bool:
    """True ONLY when we have real chart data AND every block reports zero
    free capacity. Returns False if data is missing/unknown — callers must
    treat that as a transient error, not as 'chart full'."""
    statuses = statuses or {}
    blocks = extract_blocks(rendering_info, statuses)
    if not blocks:
        return False  # no chart data → cannot conclude full
    for b in blocks:
        free = b.get("free", 0)
        if free > 0 or free < 0:    # >0 free, or unknown (-1)
            return False
    return True
