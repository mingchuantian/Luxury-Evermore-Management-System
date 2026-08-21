from pymongo import ASCENDING, DESCENDING
from pymongo.errors import OperationFailure


def _safe_create_index(coll, keys, **kwargs):
    """
    Create index but never crash app boot if an equivalent/conflicting index already exists.
    This protects production from startup failures caused by index option/name conflicts.
    """
    try:
        return coll.create_index(keys, **kwargs)
    except OperationFailure as e:
        # Common when an index with same key pattern exists but options/name differ.
        # We prefer the app to boot; index can be reconciled manually if needed.
        code_name = getattr(e, "code_name", None) or (e.details or {}).get("codeName") if hasattr(e, "details") else None
        if code_name in ("IndexKeySpecsConflict", "IndexOptionsConflict", "IndexAlreadyExists"):
            return None
        # Also tolerate code 86 (IndexKeySpecsConflict) defensively
        if getattr(e, "code", None) == 86:
            return None
        raise


def ensure_indexes(items, users, audit_logs, notes):
    _safe_create_index(items, [("created_at", DESCENDING)])
    _safe_create_index(items, [("purchase_at", DESCENDING)])
    _safe_create_index(items, [("status", ASCENDING)])
    _safe_create_index(items, [("sku", ASCENDING)], unique=True, sparse=True)
    _safe_create_index(items, [("brand", ASCENDING)])
    _safe_create_index(items, [("seller_name", ASCENDING)])
    _safe_create_index(items, [("seller_contact", ASCENDING)])
    _safe_create_index(items, [("ownership", ASCENDING), ("created_at", DESCENDING)])
    _safe_create_index(items, [("ownership", ASCENDING), ("status", ASCENDING)])
    _safe_create_index(items, [
        ("ownership", ASCENDING),
        ("status", ASCENDING),
        ("sold_record.sold_at", DESCENDING),
    ])

    _safe_create_index(users, [("username", ASCENDING)], unique=True)

    _safe_create_index(audit_logs, [("at", DESCENDING)])
    _safe_create_index(audit_logs, [("action", ASCENDING)])
    _safe_create_index(audit_logs, [("sku", ASCENDING)])
    _safe_create_index(audit_logs, [("item_id", ASCENDING)])
    _safe_create_index(audit_logs, [("by.username", ASCENDING)])

    _safe_create_index(notes, [("created_at", DESCENDING)])
    _safe_create_index(notes, [("username", ASCENDING)])


