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
    """Aggregate every seat by its containing block/section.

    Returns a list of dicts:
        {
            "name":     "S1",
            "center_x": 500.0,
            "center_y": 320.5,
            "free":     12,
            "total":    50,
            "category": "CAT 1 - S",   # most common category in the block
        }
    """
    statuses = statuses or {}
    objs = _walk_objects(rendering_info)
    if not objs:
        return []

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

        # Coordinates may live in different keys depending on chart version
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

    out: list[dict] = []
    for name, b in by_block.items():
        cx = sum(b["_xs"]) / len(b["_xs"]) if b["_xs"] else 0.0
        cy = sum(b["_ys"]) / len(b["_ys"]) if b["_ys"] else 0.0
        cat = ""
        if b["_cats"]:
            cat = max(b["_cats"].items(), key=lambda kv: kv[1])[0]
        out.append({
            "name": name,
            "center_x": cx,
            "center_y": cy,
            "free": b["free"],
            "total": b["total"],
            "category": cat,
        })
    # Stable sort: by name (alphanumeric-aware)
    out.sort(key=lambda d: (_to_int(d["name"]) or 0, d["name"]))
    return out


def geometric_neighbors(blocks: list[dict], reference: str,
                        exclude: list[str] | None = None,
                        limit: int = 8) -> list[str]:
    """Return block names ordered by Euclidean distance from `reference`."""
    exclude = set(exclude or [])
    exclude.add(reference)
    ref = next((b for b in blocks if b["name"] == reference), None)
    if not ref:
        return []
    rx, ry = ref["center_x"], ref["center_y"]
    candidates = [
        (b["name"],
         math.hypot(b["center_x"] - rx, b["center_y"] - ry),
         b["free"])
        for b in blocks if b["name"] not in exclude and b["free"] > 0
    ]
    candidates.sort(key=lambda t: (t[1], -t[2]))  # closest first, free desc tiebreak
    return [c[0] for c in candidates[:limit]]


def adjacent_seats_in_block(rendering_info: Any,
                             statuses: dict[str, str],
                             block_name: str,
                             quantity: int) -> list[str]:
    """Find `quantity` consecutive free seats within `block_name`.

    Returns the seat IDs in row-order, or [] if not possible.
    """
    objs = _walk_objects(rendering_info)
    if not objs:
        return []

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

    # group by row, then look for contiguous run
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

    # Fallback: return any N free seats from the block (best-effort)
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
    ([], "") and the caller should engage the drop-watcher.

    Order:
      1. primary_block
      2. backup_blocks (in user order)
      3. geometric neighbors of (primary, then each backup) — only if
         expand_geometric=True
    """
    # 1) primary
    if primary_block:
        ids = adjacent_seats_in_block(rendering_info, statuses,
                                       primary_block, quantity)
        if ids:
            return ids, primary_block

    # 2) backups
    for blk in backup_blocks:
        ids = adjacent_seats_in_block(rendering_info, statuses,
                                       blk, quantity)
        if ids:
            return ids, blk

    if not expand_geometric:
        return [], ""

    # 3) geometric neighbors
    blocks = extract_blocks(rendering_info, statuses)
    seen = set([primary_block] + list(backup_blocks))
    refs = [primary_block] + list(backup_blocks)
    for ref in refs:
        if not ref:
            continue
        for nb in geometric_neighbors(blocks, ref, exclude=list(seen),
                                       limit=expand_limit):
            ids = adjacent_seats_in_block(rendering_info, statuses,
                                           nb, quantity)
            if ids:
                return ids, nb
            seen.add(nb)

    return [], ""
