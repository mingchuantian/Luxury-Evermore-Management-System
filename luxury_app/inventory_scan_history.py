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


def _aware(value):
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value if isinstance(value, datetime) else None


def scan_history_for_skus(sessions, skus: Iterable[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Return {sku: [record, ...]} newest first. Missing SKUs have no key.

    record = {"at", "location", "location_key", "session_id", "active"}
    location is the Chinese label; location_key is "store" / "room" / "legacy".
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
            history.setdefault(sku, []).append({
                "at": _aware(scan.get("scanned_at")) or fallback,
                "location": location,
                "location_key": location_key,
                "session_id": str(check.get("_id")),
                "active": check.get("status") == "active",
            })
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    for records in history.values():
        records.sort(key=lambda record: record["at"] or oldest, reverse=True)
    return history
