import re
import secrets
from datetime import date, datetime, timedelta, timezone


BUSINESS_TZ = timezone(timedelta(hours=8))
MONGO_BUSINESS_TIMEZONE = "+08:00"


def money_int(x):
    if x is None or x == "":
        return 0
    return int(float(x))


_BASE32_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉 I O 0 1 等易混淆字符


def _short_code(n=7) -> str:
    return "".join(secrets.choice(_BASE32_ALPHABET) for _ in range(n))


def parse_date_yyyy_mm_dd(s: str):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = date.fromisoformat(s)  # YYYY-MM-DD
        local_midnight = datetime(d.year, d.month, d.day, tzinfo=BUSINESS_TZ)
        return local_midnight.astimezone(timezone.utc)
    except Exception:
        return None


def find_date_yyyy_mm_dd_in_text(s: str):
    """从任意文本中提取 YYYY-MM-DD（用于搜索）。"""
    s = (s or "").strip()
    if not s:
        return None
    m = re.search(r"\b\d{4}-\d{2}-\d{2}\b", s)
    if not m:
        return None
    return parse_date_yyyy_mm_dd(m.group(0))


def parse_datetime_local_to_utc(dt_local_str: str, tz_offset_min: str):
    """
    dt_local_str: 'YYYY-MM-DDTHH:MM[:SS]' from <input type="datetime-local">
    tz_offset_min: JS Date().getTimezoneOffset() (minutes to add to local to get UTC)
    """
    s = (dt_local_str or "").strip()
    if not s:
        return None
    try:
        offset = int(tz_offset_min)
    except Exception:
        offset = 0
    try:
        dt_local = datetime.fromisoformat(s)  # naive local time
    except Exception:
        return None
    return dt_local.replace(tzinfo=timezone.utc) + timedelta(minutes=offset)


def format_datetime_local(value) -> str:
    """Format a stored UTC datetime for a UTC+8 datetime-local input."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return ""
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BUSINESS_TZ).strftime("%Y-%m-%dT%H:%M:%S")


def datetimes_same_instant(left, right) -> bool:
    """Compare Mongo naive-UTC and timezone-aware datetimes safely."""
    if not isinstance(left, datetime) or not isinstance(right, datetime):
        return left == right

    def as_utc(value):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    # datetime-local is second-precision, while imported/Mongo values may
    # contain microseconds that the form cannot display or preserve.
    return (
        as_utc(left).replace(microsecond=0)
        == as_utc(right).replace(microsecond=0)
    )


def _brand_prefix(brand: str) -> str:
    b = (brand or "").strip()
    if not b:
        return "ITEM"

    mapping = {
        "Louis Vuitton": "LV",
        "Van Cleef & Arpels": "VCA",
        "Saint Laurent": "YSL",
        "Bottega Veneta": "BV",
    }
    if b in mapping:
        return mapping[b]

    cleaned = re.sub(r"[^A-Za-z0-9]+", "", b).upper()
    if cleaned:
        return cleaned[:8]

    return "ITEM"


def gen_sku_unique(items, brand: str) -> str:
    # prefix currently unused but keep for compatibility if needed later
    _ = _brand_prefix(brand)
    for _ in range(20):
        sku = f"{_short_code(7)}"
        if items.count_documents({"sku": sku}, limit=1) == 0:
            return sku
    return f"{_short_code(9)}"


def is_object_id(s: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{24}", s or ""))


