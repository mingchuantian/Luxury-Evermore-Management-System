"""Claude新增分析: eleven read-only business analyses.

Everything here is a pure function over item documents that were already read
from MongoDB. Nothing in this module (or its route) writes to the database.

Money is converted to one display currency with the exchange rates chosen on
the page, so RMB / SGD / USD items can be added together. Items whose currency
is not recognised are left out of money totals and reported in the data
quality panel instead of being guessed.
"""

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Callable, Dict, Iterable, List, Optional

BUSINESS_TZ = timezone(timedelta(hours=8))

IN_STOCK_STATUSES = ("RECEIVED", "ON_SHELF", "RESERVED", "REPARING")
PROBLEM_STATUSES = ("MISSING", "DAMAGED", "RETURNED")
STATUS_ZH = {
    "INBOUND": "已录入（未到货）", "REPARING": "护理中", "RECEIVED": "已到货",
    "ON_SHELF": "上架在售", "RESERVED": "已预留", "SOLD": "已售出",
    "MISSING": "缺件/未收到", "DAMAGED": "到货损坏", "RETURNED": "已退货",
}

AGE_BUCKETS = [(0, 30, "0–30天"), (31, 60, "31–60天"), (61, 90, "61–90天"),
               (91, 180, "91–180天"), (181, None, "180天以上")]
SELL_DAY_BUCKETS = [(0, 7, "0–7天"), (8, 14, "8–14天"), (15, 30, "15–30天"),
                    (31, 60, "31–60天"), (61, 90, "61–90天"),
                    (91, 180, "91–180天"), (181, None, "180天以上")]
PRICE_BANDS_SGD = [0, 1000, 3000, 6000, 10000]
MIN_GROUP = 3          # groups smaller than this are too small to compare
TOP_GROUPS = 10

_CCY_ALIASES = {
    "SGD": "SGD", "S$": "SGD", "新币": "SGD",
    "RMB": "RMB", "CNY": "RMB", "人民币": "RMB", "¥": "RMB",
    "USD": "USD", "US$": "USD", "美元": "USD",
}
_CHANNEL_ALIASES = [
    ("店内", ("店内", "店里", "门店", "到店", "线下", "walk", "walkin", "store", "shop", "showroom")),
    ("Instagram", ("ig", "instagram", "ins")),
    ("Carousell", ("carousell", "carou")),
    ("网站", ("网站", "website", "web", "shopify", "官网", "online")),
    ("小红书", ("小红书", "xhs", "red")),
    ("WhatsApp", ("whatsapp", "wa", "wsp")),
    ("微信", ("微信", "wechat", "wx")),
    ("Telegram", ("telegram", "tg")),
    ("直播", ("直播", "live")),
]


# ------------------------------------------------------------------ helpers


def to_dt(value) -> Optional[datetime]:
    """Aware UTC datetime from a datetime / ISO string; None otherwise."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_num(value) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def norm_ccy(value) -> Optional[str]:
    return _CCY_ALIASES.get(str(value or "").strip().upper())


def norm_channel(value) -> str:
    """Group free-text sale channels ("ig", "IG story", "Instagram") together."""
    text = " ".join(str(value or "").split())
    if not text:
        return "未填"
    lowered = text.casefold()
    words = set(re.findall(r"[a-z]+", lowered))
    for label, needles in _CHANNEL_ALIASES:
        for needle in needles:
            if needle.isascii() and len(needle) <= 4:
                if needle in words:          # short codes must be a whole word
                    return label
            elif needle in lowered:
                return label
    return text


def month_key(dt: datetime) -> str:
    return dt.astimezone(BUSINESS_TZ).strftime("%Y-%m")


def _month_start(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=BUSINESS_TZ)


def _shift_month(year: int, month: int, delta: int):
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _med(values: Iterable[float]) -> Optional[float]:
    values = [v for v in values if v is not None]
    return float(median(values)) if values else None


def _avg(values: Iterable[float]) -> Optional[float]:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _ratio(part: float, whole: float) -> Optional[float]:
    return part / whole if whole else None


def _bucket_label(days: float, buckets) -> str:
    for low, high, label in buckets:
        if days >= low and (high is None or days <= high):
            return label
    return buckets[-1][2]


class Options:
    """Page filters. Rates are parameters, never stored."""

    def __init__(self, *, ownership="admin", months=12, ccy="SGD",
                 sgd_rmb=5.3, usd_sgd=1.3, profit_basis="recorded"):
        self.ownership = ownership if ownership in ("admin", "management", "all") else "admin"
        self.months = months if months in (0, 3, 6, 12, 24) else 12
        self.ccy = ccy if ccy in ("SGD", "RMB") else "SGD"
        self.sgd_rmb = sgd_rmb if 0.1 <= sgd_rmb <= 100 else 5.3
        self.usd_sgd = usd_sgd if 0.1 <= usd_sgd <= 100 else 1.3
        self.profit_basis = profit_basis if profit_basis in ("recorded", "computed") else "recorded"

    def convert(self, amount, currency) -> Optional[float]:
        amount = to_num(amount)
        currency = norm_ccy(currency)
        if amount is None or currency is None:
            return None
        if currency == "SGD":
            sgd = amount
        elif currency == "RMB":
            sgd = amount / self.sgd_rmb
        else:
            sgd = amount * self.usd_sgd
        return sgd if self.ccy == "SGD" else sgd * self.sgd_rmb

    def price_bands(self):
        scale = 1 if self.ccy == "SGD" else 5
        edges = [edge * scale for edge in PRICE_BANDS_SGD]
        bands = []
        for index, low in enumerate(edges):
            high = edges[index + 1] if index + 1 < len(edges) else None
            label = f"{low:,.0f}–{high:,.0f}" if high else f"{low:,.0f}以上"
            if index == 0:
                label = f"{high:,.0f}以下"
            bands.append((low, high, label))
        return bands


# ------------------------------------------------------- per-item facts


def normalize(doc: Dict[str, Any], opts: Options, now: datetime) -> Dict[str, Any]:
    """Flatten one item document into the facts the analyses need."""
    status = str(doc.get("status") or "")
    cost_ccy_raw = doc.get("cost_currency") or doc.get("currency")
    cost_raw = to_num(doc.get("cost"))
    source = str(doc.get("source_type") or "").upper()
    if source not in ("BUY_IN", "CONSIGNMENT"):
        source = "CONSIGNMENT" if doc.get("is_consignment") else "BUY_IN"

    start_at = to_dt(doc.get("purchase_at")) or to_dt(doc.get("created_at"))
    row = {
        "sku": str(doc.get("sku") or ""),
        "name": f"{doc.get('brand') or ''} {doc.get('name') or ''}".strip(),
        "brand": str(doc.get("brand") or "").strip() or "未填品牌",
        "has_brand": bool(str(doc.get("brand") or "").strip()),
        "status": status,
        "ownership": "management" if doc.get("ownership") == "management" else "admin",
        "source": source,
        "seller": " ".join(str(doc.get("seller_name") or "").split()),
        "serial_code": str(doc.get("serial_code") or "").strip(),
        "shopify_linked": doc.get("shopify_sku_exist") is True,
        "start_at": start_at,
        "created_at": to_dt(doc.get("created_at")),
        "cost_raw": cost_raw,
        "cost_ccy_known": norm_ccy(cost_ccy_raw) is not None,
        "cost": opts.convert(cost_raw, cost_ccy_raw) if cost_raw else None,
        "listing": None,
        "in_stock": status in IN_STOCK_STATUSES,
        "is_sold": status == "SOLD",
        "has_sale": False,
        "sold_at": None, "price": None, "channel": "未填", "payment": "未填",
        "sold_by": "未填", "buyer": "",
        "profit": None, "profit_estimated": False, "profit_recorded": False,
        "days_to_sell": None, "age_days": None,
        "received_at": None, "on_shelf_at": None, "inbound_at": None,
        "status_at": None,   # when the item last moved into its current status
        # Exactly what the dashboard's 未售出商品总价值 adds up: source_type is
        # literally "BUY_IN" and cost is a stored number (no currency conversion).
        "dash_buy_in": doc.get("source_type") == "BUY_IN",
        "dash_cost": doc.get("cost") if isinstance(doc.get("cost"), (int, float))
                     and not isinstance(doc.get("cost"), bool) else 0,
    }

    listing_raw = to_num(doc.get("listing_price"))
    listing_ccy = doc.get("listing_currency") or "SGD"
    if listing_raw:
        row["listing"] = opts.convert(listing_raw, listing_ccy)

    # Status timeline from the item's own audit trail.
    for entry in doc.get("audit") or []:
        if not isinstance(entry, dict) or entry.get("action") != "Change_status":
            continue
        at = to_dt(entry.get("at"))
        target = {"INBOUND": "inbound_at", "RECEIVED": "received_at",
                  "ON_SHELF": "on_shelf_at"}.get(entry.get("to"))
        if at and target and (row[target] is None or at < row[target]):
            row[target] = at
        if at and entry.get("to") == status and (row["status_at"] is None or at > row["status_at"]):
            row["status_at"] = at
    row["received_at"] = row["received_at"] or to_dt(doc.get("received_at"))

    sales = doc.get("sold_record") or []
    last = sales[-1] if isinstance(sales, list) and sales and isinstance(sales[-1], dict) else None
    if row["is_sold"] and last:
        sold_at = to_dt(last.get("sold_at"))
        price_raw = to_num(last.get("sold_price"))
        sold_ccy = (last.get("sold_currency") or doc.get("listing_currency")
                    or cost_ccy_raw)
        row.update(
            has_sale=sold_at is not None,
            sold_at=sold_at,
            price_raw=price_raw,
            price=opts.convert(price_raw, sold_ccy) if price_raw else None,
            channel=norm_channel(last.get("sale_channel")),
            payment=str(last.get("payment_method") or "").strip() or "未填",
            sold_by=str(last.get("sold_by_username") or "").strip() or "未填",
            buyer=str(last.get("buyer") or "").strip(),
        )
        if sold_at and start_at:
            days = (sold_at - start_at).total_seconds() / 86400
            row["days_to_sell"] = days if days >= 0 else None
            row["sold_before_start"] = days < 0

    if row["is_sold"]:
        recorded_raw = to_num(doc.get("profit"))
        recorded = None
        if recorded_raw:
            recorded = opts.convert(
                recorded_raw, doc.get("profit_currency") or cost_ccy_raw
            )
            row["profit_recorded"] = True
        computed = None
        if row["price"] is not None and row["cost"] is not None:
            computed = row["price"] - row["cost"]
        if opts.profit_basis == "recorded" and recorded is not None:
            row["profit"] = recorded
        elif computed is not None:
            row["profit"] = computed
            row["profit_estimated"] = opts.profit_basis == "recorded"

    if row["in_stock"] and start_at:
        row["age_days"] = max(0.0, (now - start_at).total_seconds() / 86400)
    return row


def _sales_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate sold rows: revenue, profit, margin on cost, turnover days."""
    paired = [r for r in rows if r["profit"] is not None and r["cost"]]
    profit_on_cost = sum(r["profit"] for r in paired)
    cost_paired = sum(r["cost"] for r in paired)
    margin = _ratio(profit_on_cost, cost_paired)
    days = [r["days_to_sell"] for r in rows if r["days_to_sell"] is not None]
    avg_days = _avg(days)
    annualized = None
    if margin is not None and avg_days is not None and len(paired) >= MIN_GROUP:
        annualized = margin * 365 / max(avg_days, 1.0)
    return {
        "n": len(rows),
        "revenue": sum(r["price"] or 0 for r in rows),
        "cost": sum(r["cost"] or 0 for r in rows),
        "profit": sum(r["profit"] or 0 for r in rows),
        "profit_n": sum(1 for r in rows if r["profit"] is not None),
        "avg_profit": _avg(r["profit"] for r in rows),
        "margin": margin,
        "median_days": _med(days),
        "avg_days": avg_days,
        "annualized": annualized,
    }


def _group(rows, key: Callable[[Dict[str, Any]], str]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return groups


def _top_groups(groups, *, limit=TOP_GROUPS, min_n=MIN_GROUP):
    """Largest groups first; prefer groups with at least min_n rows."""
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    big = [kv for kv in ordered if len(kv[1]) >= min_n]
    return (big or ordered)[:limit]


def _sample(rows, limit=8):
    return [{"sku": r["sku"], "name": r["name"]} for r in rows[:limit] if r["sku"]]


# ------------------------------------------------------------ the report


def build_report(docs: Iterable[Dict[str, Any]], opts: Options,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows_all = [normalize(doc, opts, now) for doc in docs]
    rows = [r for r in rows_all if opts.ownership == "all" or r["ownership"] == opts.ownership]

    cutoff = None
    if opts.months:
        cutoff = now - timedelta(days=round(opts.months * 30.4375))

    def in_period(dt):
        return dt is not None and (cutoff is None or dt >= cutoff)

    sold = [r for r in rows if r["is_sold"] and r["has_sale"]]
    sold_p = [r for r in sold if in_period(r["sold_at"])]
    stock = [r for r in rows if r["in_stock"]]
    bands = opts.price_bands()

    def band_of(row):
        cost = row["cost"]
        if cost is None:
            return "成本未知"
        for low, high, label in bands:
            if cost >= low and (high is None or cost < high):
                return label
        return bands[-1][2]

    band_order = [label for _, _, label in bands]

    return {
        "now": now,
        "cutoff": cutoff,
        "item_count": len(rows),
        "sold_count": len(sold),
        "sold_in_period": len(sold_p),
        "profit_estimated_count": sum(1 for r in sold_p if r["profit_estimated"]),
        "profit_missing_count": sum(1 for r in sold_p if r["profit"] is None),
        "aging": _aging(stock, rows),
        "turnover": _turnover(sold_p),
        "returns": _returns(sold_p, band_of, band_order),
        "sourcing": _sourcing(rows, in_period),
        "discount": _discount(sold_p),
        "channels": _channels(sold_p),
        "flow": _flow(rows, in_period),
        "kpi": _kpi(rows, stock, now),
        "cash": _cash(rows, now, 24 if opts.months in (0, 24) else 12),
        "consignment": _consignment(rows, now),
        "stock_value": _stock_value(rows_all, now),
        "quality": _quality(rows),
    }


# 1 ---------------------------------------------------------------- aging


def _aging(stock, rows):
    aged = [r for r in stock if r["age_days"] is not None]
    labels = [label for _, _, label in AGE_BUCKETS]
    counts = Counter(_bucket_label(r["age_days"], AGE_BUCKETS) for r in aged)
    cost = Counter()
    for r in aged:
        if r["source"] == "BUY_IN" and r["cost"]:
            cost[_bucket_label(r["age_days"], AGE_BUCKETS)] += r["cost"]
    old = [r for r in aged if r["age_days"] > 90]
    buy_in_cost = sum(r["cost"] or 0 for r in stock if r["source"] == "BUY_IN")
    old_cost = sum(r["cost"] or 0 for r in old if r["source"] == "BUY_IN")
    oldest = sorted(aged, key=lambda r: -r["age_days"])[:20]
    return {
        "labels": labels,
        "counts": [counts.get(label, 0) for label in labels],
        "costs": [round(cost.get(label, 0)) for label in labels],
        "stock_count": len(stock),
        "consignment_count": sum(1 for r in stock if r["source"] == "CONSIGNMENT"),
        "buy_in_cost": buy_in_cost,
        "cost_unknown": sum(1 for r in stock if r["source"] == "BUY_IN" and r["cost"] is None),
        "old_count": len(old),
        "old_share": _ratio(len(old), len(aged)),
        "old_cost": old_cost,
        "old_cost_share": _ratio(old_cost, buy_in_cost),
        "median_age": _med(r["age_days"] for r in aged),
        "inbound_count": sum(1 for r in rows if r["status"] == "INBOUND"),
        "oldest": [{
            "sku": r["sku"], "name": r["name"], "days": round(r["age_days"]),
            "status": STATUS_ZH.get(r["status"], r["status"]),
            "source": "寄卖" if r["source"] == "CONSIGNMENT" else "收货",
            "cost": r["cost"], "listing": r["listing"],
            "shopify_linked": r["shopify_linked"],
        } for r in oldest],
    }


# 2 ------------------------------------------------------------- turnover


def _turnover(sold_p):
    timed = [r for r in sold_p if r["days_to_sell"] is not None]
    labels = [label for _, _, label in SELL_DAY_BUCKETS]
    hist = Counter(_bucket_label(r["days_to_sell"], SELL_DAY_BUCKETS) for r in timed)
    brands = [{
        "label": brand, "n": len(group),
        "median": _med(r["days_to_sell"] for r in group),
        "avg": _avg(r["days_to_sell"] for r in group),
    } for brand, group in _top_groups(_group(timed, lambda r: r["brand"]))]
    brands.sort(key=lambda g: g["median"])
    by_month = _group(timed, lambda r: month_key(r["sold_at"]))
    months = sorted(by_month)
    return {
        "n": len(timed),
        "median": _med(r["days_to_sell"] for r in timed),
        "avg": _avg(r["days_to_sell"] for r in timed),
        "within_30": _ratio(sum(1 for r in timed if r["days_to_sell"] <= 30), len(timed)),
        "over_90": _ratio(sum(1 for r in timed if r["days_to_sell"] > 90), len(timed)),
        "hist_labels": labels,
        "hist_counts": [hist.get(label, 0) for label in labels],
        "brands": brands,
        "months": months,
        "month_median": [round(_med(r["days_to_sell"] for r in by_month[m]), 1) for m in months],
        "month_n": [len(by_month[m]) for m in months],
    }


# 3 -------------------------------------------------------------- returns


def _returns(sold_p, band_of, band_order):
    def table(groups, order=None):
        result = []
        keys = order if order is not None else [k for k, _ in sorted(
            groups.items(), key=lambda kv: -sum(r["profit"] or 0 for r in kv[1]))]
        for key in keys:
            if key in groups:
                result.append(dict(_sales_summary(groups[key]), label=key))
        return result

    brand_groups = _group(sold_p, lambda r: r["brand"])
    brand_rows = table(brand_groups)[:15]
    band_groups = _group(sold_p, band_of)
    band_rows = table(band_groups, band_order + ["成本未知"])
    source_groups = _group(sold_p, lambda r: "寄卖" if r["source"] == "CONSIGNMENT" else "收货")
    points = [{
        "label": g["label"], "x": round(g["median_days"], 1),
        "y": round(g["margin"] * 100, 1), "n": g["n"],
    } for g in table(brand_groups)
        if g["n"] >= MIN_GROUP and g["margin"] is not None and g["median_days"] is not None][:12]
    return {
        "overall": _sales_summary(sold_p),
        "brands": brand_rows,
        "bands": band_rows,
        "sources": table(source_groups, ["收货", "寄卖"]),
        "points": points,
    }


# 4 ------------------------------------------------------------- sourcing


def _sourcing(rows, in_period):
    scoped = [r for r in rows if in_period(r["start_at"])]
    named = [r for r in scoped if r["seller"]]
    display = {}
    for r in named:
        display.setdefault(r["seller"].casefold(), Counter())[r["seller"]] += 1
    groups = _group(named, lambda r: r["seller"].casefold())
    table = []
    for key, group in groups.items():
        sold_rows = [r for r in group if r["is_sold"] and r["has_sale"]]
        summary = _sales_summary(sold_rows)
        table.append({
            "label": display[key].most_common(1)[0][0],
            "n": len(group),
            "sold": len(sold_rows),
            "in_stock": sum(1 for r in group if r["in_stock"]),
            "problems": sum(1 for r in group if r["status"] in PROBLEM_STATUSES),
            "cost": sum(r["cost"] or 0 for r in group),
            "profit": summary["profit"],
            "margin": summary["margin"],
            "median_days": summary["median_days"],
            "consignment": sum(1 for r in group if r["source"] == "CONSIGNMENT"),
        })
    table.sort(key=lambda g: (-g["profit"], -g["n"]))
    chart = [g for g in table if g["sold"]][:TOP_GROUPS]
    return {
        "total": len(scoped),
        "unnamed": len(scoped) - len(named),
        "seller_count": len(table),
        "repeat_sellers": sum(1 for g in table if g["n"] >= 2),
        "rows": table[:20],
        "chart_labels": [g["label"] for g in chart],
        "chart_profit": [round(g["profit"]) for g in chart],
    }


# 5 ------------------------------------------------------------- discount


def _discount(sold_p):
    usable = [r for r in sold_p if r["listing"] and r["price"]]
    for r in usable:
        r["_disc"] = (r["listing"] - r["price"]) / r["listing"]
    # Ignore obvious data mistakes (listing price typed in another unit).
    usable = [r for r in usable if -0.5 <= r["_disc"] <= 0.9]
    timed = [r for r in usable if r["days_to_sell"] is not None]
    by_age = _group(timed, lambda r: _bucket_label(r["days_to_sell"], AGE_BUCKETS))
    age_labels = [label for _, _, label in AGE_BUCKETS]
    brands = [{
        "label": brand, "n": len(group),
        "median": _med(r["_disc"] for r in group),
        "avg": _avg(r["_disc"] for r in group),
    } for brand, group in _top_groups(_group(usable, lambda r: r["brand"]))]
    brands.sort(key=lambda g: -g["avg"])
    return {
        "n": len(usable),
        "excluded": len(sold_p) - len(usable),
        "avg": _avg(r["_disc"] for r in usable),
        "median": _med(r["_disc"] for r in usable),
        "at_list": _ratio(sum(1 for r in usable if abs(r["_disc"]) < 0.005), len(usable)),
        "over_10": _ratio(sum(1 for r in usable if r["_disc"] > 0.10), len(usable)),
        "above_list": _ratio(sum(1 for r in usable if r["_disc"] < -0.005), len(usable)),
        "age_labels": age_labels,
        "age_avg": [round((_avg(r["_disc"] for r in by_age.get(label, [])) or 0) * 100, 1)
                    for label in age_labels],
        "age_n": [len(by_age.get(label, [])) for label in age_labels],
        "brands": brands,
    }


# 6 ------------------------------------------------------------- channels


def _channels(sold_p):
    total_n = len(sold_p)
    total_rev = sum(r["price"] or 0 for r in sold_p)

    def breakdown(key):
        result = []
        for label, group in _group(sold_p, key).items():
            revenue = sum(r["price"] or 0 for r in group)
            result.append({
                "label": label, "n": len(group), "revenue": revenue,
                "share_n": _ratio(len(group), total_n),
                "share_revenue": _ratio(revenue, total_rev),
                "profit": sum(r["profit"] or 0 for r in group),
                "avg_price": _avg(r["price"] for r in group),
            })
        result.sort(key=lambda g: (g["label"] == "未填", -g["revenue"], -g["n"]))
        return result

    def filled(key):
        return _ratio(sum(1 for r in sold_p if key(r) != "未填"), total_n)

    return {
        "n": total_n,
        "channel": breakdown(lambda r: r["channel"]),
        "payment": breakdown(lambda r: r["payment"]),
        "staff": breakdown(lambda r: r["sold_by"]),
        "channel_filled": filled(lambda r: r["channel"]),
        "payment_filled": filled(lambda r: r["payment"]),
        "staff_filled": filled(lambda r: r["sold_by"]),
        "buyer_filled": _ratio(sum(1 for r in sold_p if r["buyer"]), total_n),
    }


# 7 ----------------------------------------------------------------- flow


def _flow(rows, in_period):
    def step(label, pairs, note=""):
        days = []
        for start, end in pairs:
            if start and end and in_period(end):
                gap = (end - start).total_seconds() / 86400
                if gap >= 0:
                    days.append(gap)
        ordered = sorted(days)
        p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))] if ordered else None
        return {"label": label, "n": len(days), "median": _med(days),
                "avg": _avg(days), "p90": p90, "note": note}

    steps = [
        step("录入 → 到货",
             [(r["inbound_at"], r["received_at"]) for r in rows if r["inbound_at"]],
             "只统计先录入、后到货的商品"),
        step("到货 → 上架",
             [(r["received_at"], r["on_shelf_at"]) for r in rows],
             "只统计已经上架过的商品；老板间的同款备货会拉长这个数字"),
        step("上架 → 售出",
             [(r["on_shelf_at"], r["sold_at"]) for r in rows if r["is_sold"]],
             "只统计上架后售出的商品"),
        step("收货 → 售出（全程）",
             [(r["start_at"], r["sold_at"]) for r in rows if r["is_sold"]]),
    ]
    sold_rows = [r for r in rows if r["is_sold"] and r["has_sale"] and in_period(r["sold_at"])]
    return {
        "steps": steps,
        "tracked": sum(1 for r in rows if r["received_at"] or r["on_shelf_at"]),
        "sold_never_listed": sum(1 for r in sold_rows if not r["on_shelf_at"]),
        "sold_n": len(sold_rows),
    }


# 8 ------------------------------------------------------------------ kpi


def _kpi(rows, stock, now):
    local = now.astimezone(BUSINESS_TZ)
    this_start = _month_start(local.year, local.month)
    elapsed = local - this_start

    def window(delta_months):
        year, month = _shift_month(local.year, local.month, delta_months)
        start = _month_start(year, month)
        next_year, next_month = _shift_month(year, month, 1)
        end = min(start + elapsed, _month_start(next_year, next_month))
        return start, end

    def measure(start, end):
        sold_rows = [r for r in rows if r["is_sold"] and r["sold_at"]
                     and start <= r["sold_at"] <= end]
        bought = [r for r in rows if r["source"] == "BUY_IN" and r["start_at"]
                  and start <= r["start_at"] <= end]
        return {
            "revenue": sum(r["price"] or 0 for r in sold_rows),
            "profit": sum(r["profit"] or 0 for r in sold_rows),
            "sold": len(sold_rows),
            "purchase": sum(r["cost"] or 0 for r in bought),
            "purchase_n": len(bought),
        }

    current = measure(this_start, local)
    last_month = measure(*window(-1))
    last_year = measure(*window(-12))

    def delta(key, base):
        return _ratio(current[key] - base[key], base[key]) if base[key] else None

    tiles = []
    for key, label, money, good_up in (
        ("revenue", "销售额", True, True), ("profit", "利润", True, True),
        ("sold", "售出件数", False, True), ("purchase", "收货支出", True, None),
    ):
        tiles.append({
            "key": key, "label": label, "money": money, "good_up": good_up,
            "value": current[key],
            "last_month": last_month[key], "vs_last_month": delta(key, last_month),
            "last_year": last_year[key], "vs_last_year": delta(key, last_year),
        })
    return {
        "month": local.strftime("%Y-%m"),
        "through": local.strftime("%m-%d"),
        "day": local.day,
        "tiles": tiles,
        "stock_cost": sum(r["cost"] or 0 for r in stock if r["source"] == "BUY_IN"),
        "stock_count": len(stock),
    }


# 9 ----------------------------------------------------------------- cash


def _cash(rows, now, span):
    local = now.astimezone(BUSINESS_TZ)
    months = []
    for back in range(span - 1, -1, -1):
        year, month = _shift_month(local.year, local.month, -back)
        months.append(f"{year:04d}-{month:02d}")
    index = {m: i for i, m in enumerate(months)}
    income = [0.0] * span
    purchase = [0.0] * span
    payout = [0.0] * span
    for r in rows:
        if r["source"] == "BUY_IN" and r["start_at"] and r["cost"]:
            i = index.get(month_key(r["start_at"]))
            if i is not None:
                purchase[i] += r["cost"]
        if r["is_sold"] and r["sold_at"]:
            i = index.get(month_key(r["sold_at"]))
            if i is not None:
                income[i] += r["price"] or 0
                if r["source"] == "CONSIGNMENT":
                    payout[i] += r["cost"] or 0
    net = [income[i] - purchase[i] - payout[i] for i in range(span)]
    cumulative, running = [], 0.0
    for value in net:
        running += value
        cumulative.append(round(running))
    return {
        "months": months,
        "income": [round(v) for v in income],
        "outflow": [round(purchase[i] + payout[i]) for i in range(span)],
        "purchase": [round(v) for v in purchase],
        "payout": [round(v) for v in payout],
        "net": [round(v) for v in net],
        "cumulative": cumulative,
        "total_income": sum(income), "total_out": sum(purchase) + sum(payout),
        "total_net": sum(net),
    }


# 10 --------------------------------------------------------- consignment


def _consignment(rows, now):
    cons = [r for r in rows if r["source"] == "CONSIGNMENT"]
    in_stock = [r for r in cons if r["in_stock"]]
    sold_rows = [r for r in cons if r["is_sold"] and r["has_sale"]]
    recent_cut = now - timedelta(days=60)
    recent = sorted((r for r in sold_rows if r["sold_at"] >= recent_cut),
                    key=lambda r: r["sold_at"], reverse=True)
    display = {}
    for r in cons:
        if r["seller"]:
            display.setdefault(r["seller"].casefold(), Counter())[r["seller"]] += 1
    people = []
    for key, group in _group([r for r in cons if r["seller"]],
                             lambda r: r["seller"].casefold()).items():
        g_stock = [r for r in group if r["in_stock"]]
        g_sold = [r for r in group if r["is_sold"] and r["has_sale"]]
        last_sold = max((r["sold_at"] for r in g_sold), default=None)
        people.append({
            "label": display[key].most_common(1)[0][0],
            "in_stock": len(g_stock),
            "in_stock_payout": sum(r["cost"] or 0 for r in g_stock),
            "sold": len(g_sold),
            "sold_payout": sum(r["cost"] or 0 for r in g_sold),
            "fee": sum((r["price"] or 0) - (r["cost"] or 0) for r in g_sold
                       if r["price"] is not None and r["cost"] is not None),
            "median_days": _med(r["days_to_sell"] for r in g_sold),
            "last_sold": last_sold,
            "recent_sold": sum(1 for r in g_sold if r["sold_at"] >= recent_cut),
        })
    people.sort(key=lambda p: (-p["recent_sold"], -p["in_stock"], -p["sold"]))
    fee_rows = [r for r in sold_rows if r["price"] is not None and r["cost"] is not None]
    return {
        "total": len(cons),
        "in_stock": len(in_stock),
        "in_stock_payout": sum(r["cost"] or 0 for r in in_stock),
        "sold": len(sold_rows),
        "recent_n": len(recent),
        "recent_payout": sum(r["cost"] or 0 for r in recent),
        "fee_total": sum(r["price"] - r["cost"] for r in fee_rows),
        "fee_rate": _ratio(sum(r["price"] - r["cost"] for r in fee_rows),
                           sum(r["price"] for r in fee_rows)),
        "median_days": _med(r["days_to_sell"] for r in sold_rows),
        "median_age": _med(r["age_days"] for r in in_stock),
        "people": people[:20],
        "recent": [{
            "sku": r["sku"], "name": r["name"], "seller": r["seller"],
            "sold_at": r["sold_at"], "price": r["price"], "payout": r["cost"],
        } for r in recent[:30]],
    }


# 11 ------------------------------------------------------------- quality


def _quality(rows):
    sold_rows = [r for r in rows if r["is_sold"]]
    active = [r for r in rows if r["status"] not in PROBLEM_STATUSES]
    checks = [
        ("已售出，但利润没填", "利润、利润率分析只能用“成交价 − 成本”估算",
         [r for r in sold_rows if not r["profit_recorded"]], len(sold_rows)),
        ("已售出，但没有售出记录", "不会进入任何销售统计",
         [r for r in sold_rows if not r["has_sale"]], len(sold_rows)),
        ("已售出，但成交价是 0", "销售额、让价、利润率都会算错",
         [r for r in sold_rows if r["has_sale"] and not r.get("price_raw")], len(sold_rows)),
        ("售出时间早于收货时间", "周转天数无法计算",
         [r for r in sold_rows if r.get("sold_before_start")], len(sold_rows)),
        ("成本是 0 或没填", "占用资金、利润率会偏离实际",
         [r for r in active if not r["cost_raw"]], len(active)),
        ("成本的币种认不出来", "这些商品的金额没有计入任何合计",
         [r for r in active if r["cost_raw"] and not r["cost_ccy_known"]], len(active)),
        ("没有填品牌", "按品牌的分析会归到“未填品牌”",
         [r for r in rows if not r["has_brand"]], len(rows)),
        ("没有填卖家 / 寄卖人", "货源排行、寄卖专页统计不到",
         [r for r in active if not r["seller"]], len(active)),
        ("已售出，但没填销售渠道", "渠道分析会归到“未填”",
         [r for r in sold_rows if r["has_sale"] and r["channel"] == "未填"], len(sold_rows)),
        ("已售出，但没填买家", "以后做买家档案、回头客分析用不上",
         [r for r in sold_rows if r["has_sale"] and not r["buyer"]], len(sold_rows)),
        ("在库，但没有包身编码", "盘查和防止调包时无法核对",
         [r for r in rows if r["in_stock"] and not r["serial_code"]],
         sum(1 for r in rows if r["in_stock"])),
        ("没有收货日期", "库龄、周转天数无法计算",
         [r for r in rows if r["start_at"] is None], len(rows)),
    ]
    result = []
    for label, impact, hits, base in checks:
        hits = sorted(hits, key=lambda r: r["created_at"] or datetime.min.replace(tzinfo=timezone.utc),
                      reverse=True)
        result.append({
            "label": label, "impact": impact, "count": len(hits), "base": base,
            "share": _ratio(len(hits), base), "samples": _sample(hits),
        })
    return {
        "checks": result,
        "issue_total": sum(c["count"] for c in result),
        "clean": sum(1 for c in result if c["count"] == 0),
    }


# 12 ------------------------------------------------- unsold stock value


MAX_HISTORY_MONTHS = 240   # guard against a bad date (e.g. 1970) blowing up the chart


def _stock_value(rows, now):
    """仪表盘“未售出商品总价值”, looked back month by month.

    Same rule as the dashboard (BUY_IN items not yet sold, cost added up as
    stored), but for each month end: an item counts if its 收货/寄卖日期 is on
    or before that month end and it had not been sold by then. The last bar is
    today and equals the dashboard figure. Uses all items, whatever the page
    filters say, exactly like the dashboard. Nothing is stored.
    Starts at the month of the earliest such item, so the whole history shows.
    """
    local = now.astimezone(BUSINESS_TZ)
    starts = [r["start_at"] for r in rows if r["dash_buy_in"] and r["start_at"] is not None]
    span = 1
    if starts:
        first = min(starts).astimezone(BUSINESS_TZ)
        span = (local.year - first.year) * 12 + (local.month - first.month) + 1
        span = max(1, min(span, MAX_HISTORY_MONTHS))
    months, points = [], []
    for back in range(span - 1, -1, -1):
        year, month = _shift_month(local.year, local.month, -back)
        months.append(f"{year:04d}-{month:02d}")
        next_year, next_month = _shift_month(year, month, 1)
        points.append(now if back == 0 else _month_start(next_year, next_month))

    value = [0.0] * span
    count = [0] * span
    no_date = no_sale_date = 0
    now_value = 0.0
    for r in rows:
        if not r["dash_buy_in"]:
            continue
        sold = r["status"] == "SOLD"
        if not sold:
            now_value += r["dash_cost"]
            count[-1] += 1
        start = r["start_at"]
        if start is None:
            no_date += 1
            continue
        until = None
        if sold:
            until = r["sold_at"]
            if until is None:
                no_sale_date += 1
                continue
        for i, point in enumerate(points[:-1]):
            if start <= point and (until is None or until > point):
                value[i] += r["dash_cost"]
                count[i] += 1
    value[-1] = now_value   # today: identical to the dashboard

    def change(back):
        if span <= back:
            return None
        base = value[-1 - back]
        return {"base": base, "diff": value[-1] - base, "pct": _ratio(value[-1] - base, base) if base else None}

    return {
        "months": months,
        "value": [round(v) for v in value],
        "count": count,
        "now_value": now_value,
        "now_count": count[-1],
        "vs_last_month": change(1),
        "vs_last_year": change(12),
        "no_date": no_date,
        "no_sale_date": no_sale_date,
    }
