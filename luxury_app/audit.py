from typing import Any, Dict, List, Optional

from flask import session

from models import now


ACTION_CHANGE_STATUS = "Change_status"
ACTION_CHANGE_DETAILS = "Change_details"


def _by() -> Dict[str, Any]:
    # requirement: at least user_id; we also keep username for display convenience
    return {"user_id": session.get("user_id"), "username": session.get("username")}


def make_change_status(*, sku: str, from_status: Any, to_status: Any) -> Optional[Dict[str, Any]]:
    f = from_status if from_status is not None else ""
    t = to_status if to_status is not None else ""
    if f == t:
        return None
    return {
        "at": now(),
        "action": ACTION_CHANGE_STATUS,
        "sku": sku,
        "from": f,
        "to": t,
        "by": _by(),
    }


def make_change_detail(*, sku: str, target: str, from_value: Any, to_value: Any) -> Optional[Dict[str, Any]]:
    f = from_value if from_value is not None else ""
    t = to_value if to_value is not None else ""
    # Skip noisy logs like "" -> "" (common during creation / hidden defaults)
    if f == "" and t == "":
        return None
    if f == t:
        return None
    return {
        "at": now(),
        "action": ACTION_CHANGE_DETAILS,
        "sku": sku,
        "target": target,
        "from": f,
        "to": t,
        "by": _by(),
    }


def _compact(entries: List[Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    return [e for e in entries if isinstance(e, dict)]


def push_item_audits(items_coll, item_oid, entries: List[Optional[Dict[str, Any]]]):
    entries2 = _compact(entries)
    if not entries2:
        return
    items_coll.update_one({"_id": item_oid}, {"$push": {"audit": {"$each": entries2}}})


def insert_audit_logs(audit_logs_coll, entries: List[Optional[Dict[str, Any]]]):
    entries2 = _compact(entries)
    if audit_logs_coll is None or not entries2:
        return
    audit_logs_coll.insert_many(entries2)


