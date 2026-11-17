"""盘查记录 for items, derived live from existing inventory check sessions.

Read-only: nothing new is stored. Each session already keeps, per scanned SKU,
the time it was scanned (scans[].scanned_at); older records without it fall
back to the session's end (or start) time.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

# Same labels as the inventory check pages.
LOCATION_LABELS = {"store": "店内陈列", "room": "老板间"}
LEGACY_LABEL = "旧盘查（未分地点）"

# Where in the back room a bag sits. Only the key is stored on the scan
# (scans[].spot); labels are for display. Order = dropdown order.
ROOM_SPOTS = (
    ("shelf_a", "货架A", "Shelf A"),
    ("shelf_b", "货架B", "Shelf B"),
    ("shelf_c", "货架C", "Shelf C"),
    ("shelf_1", "货架1", "Shelf 1"),
    ("shelf_2", "货架2", "Shelf 2"),
    ("shelf_3", "货架3", "Shelf 3"),
    ("shelf_4", "货架4", "Shelf 4"),
    ("shelf_5", "货架5", "Shelf 5"),
    ("floor", "地面", "Floor"),
    ("sofa", "沙发", "Sofa"),
)
SPOT_ZH = {key: zh for key, zh, _ in ROOM_SPOTS}
SPOT_EN = {key: en for key, _, en in ROOM_SPOTS}


def _aware(value):
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value if isinstance(value, datetime) else None


def scan_history_for_skus(sessions, skus: Iterable[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Return {sku: [record, ...]} newest first. Missing SKUs have no key.

    record = {"at", "location", "location_key", "spot", "spot_label",
              "spot_label_en", "session_id", "active"}
    location is the Chinese label; location_key is "store" / "room" / "legacy".
    spot is the back-room position key ("" when not recorded).
    """
    wanted = {sku for sku in skus if sku}
    if sessions is None or not wanted:
        return {}
    history: Dict[str, List[Dict[str, Any]]] = {}
    cursor = sessions.find(
        {"scanned_skus": {"$in": sorted(wanted)}},
        {"check_type": 1, "status": 1, "started_at": 1, "completed_at": 1, "scans": 1},
    )
    for check in cursor:
        fallback = _aware(check.get("completed_at")) or _aware(check.get("started_at"))
        location_key = check.get("check_type") if check.get("check_type") in LOCATION_LABELS else "legacy"
        location = LOCATION_LABELS.get(location_key, LEGACY_LABEL)
        seen_in_session = set()
        for scan in check.get("scans") or []:
            sku = scan.get("sku")
            if sku not in wanted or sku in seen_in_session:
                continue
            seen_in_session.add(sku)
            spot = scan.get("spot") if scan.get("spot") in SPOT_ZH else ""
            history.setdefault(sku, []).append({
                "at": _aware(scan.get("scanned_at")) or fallback,
                "location": location,
                "location_key": location_key,
                "spot": spot,
                "spot_label": SPOT_ZH.get(spot, ""),
                "spot_label_en": SPOT_EN.get(spot, ""),
                "session_id": str(check.get("_id")),
                "active": check.get("status") == "active",
            })
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    for records in history.values():
        records.sort(key=lambda record: record["at"] or oldest, reverse=True)
    return history
