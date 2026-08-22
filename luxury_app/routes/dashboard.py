import io
import json
import re
import zipfile

from bson import json_util
from flask import render_template, request, send_file
from pymongo import DESCENDING

from models import now

from ..auth import ROLE_ADMIN, require_roles
from ..constants import STATUS_ZH
from ..utils import BUSINESS_TZ, MONGO_BUSINESS_TIMEZONE, parse_date_yyyy_mm_dd
from datetime import timedelta


def _build_database_backup(database):
    """Build a ZIP of BSON-safe Extended JSON files, one per collection."""
    output = io.BytesIO()
    manifest = {
        "database": database.name,
        "exported_at": now().isoformat(),
        "format": "MongoDB Extended JSON (PyMongo bson.json_util)",
        "collections": {},
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for collection_name in sorted(database.list_collection_names()):
            if collection_name.startswith("system."):
                continue
            documents = list(database[collection_name].find({}))
            safe_name = re.sub(r"[^0-9A-Za-z._-]+", "_", collection_name)
            archive.writestr(
                f"collections/{safe_name}.json",
                json_util.dumps(documents, ensure_ascii=False, indent=2),
            )
            manifest["collections"][collection_name] = len(documents)
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
    output.seek(0)
    return output


def _inventory_value_totals(items):
    """Return BUY_IN cost totals using the requested MongoDB rules."""
    rows = list(items.aggregate([{
        "$facet": {
            "unsold": [
                {"$match": {"source_type": "BUY_IN", "status": {"$ne": "SOLD"}}},
                {"$group": {"_id": None, "total": {"$sum": "$cost"}}},
            ],
            "caring": [
                {"$match": {
                    "source_type": "BUY_IN",
                    "status": {"$in": ["INBOUND", "REPARING"]},
                }},
                {"$group": {"_id": None, "total": {"$sum": "$cost"}}},
            ],
            "received_unsold": [
                {"$match": {
                    "source_type": "BUY_IN",
                    "status": {"$in": ["RECEIVED", "ON_SHELF"]},
                }},
                {"$group": {"_id": None, "total": {"$sum": "$cost"}}},
            ],
        }
    }]))
    payload = rows[0] if rows else {}

    def total(name):
        values = payload.get(name) or []
        return int(values[0].get("total", 0) or 0) if values else 0

    return {
        "unsold": total("unsold"),
        "caring": total("caring"),
        "received_unsold": total("received_unsold"),
    }


def _daily_totals_by_day(items, start_at, end_at):
    """Aggregate daily totals using the existing UTC+8/currency rules."""
    rows = list(items.aggregate([{
        "$facet": {
            "purchase": [
                {"$match": {"status": "INBOUND"}},
                {"$addFields": {
                    "dt": {"$ifNull": ["$purchase_at", "$created_at"]},
                    "ccy": {"$ifNull": ["$cost_currency", {"$ifNull": ["$currency", ""]}]},
                    "amount": {"$ifNull": ["$cost", 0]},
                }},
                {"$match": {"dt": {"$gte": start_at, "$lt": end_at}, "ccy": "RMB"}},
                {"$group": {
                    "_id": {"$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$dt",
                        "timezone": MONGO_BUSINESS_TIMEZONE,
                    }},
                    "total": {"$sum": "$amount"},
                }},
            ],
            "sales": [
                {"$match": {"status": "SOLD", "sold_record.0": {"$exists": True}}},
                {"$addFields": {
                    "last_sale": {"$arrayElemAt": ["$sold_record", -1]},
                    "sold_at": {"$arrayElemAt": ["$sold_record.sold_at", -1]},
                }},
                {"$match": {"sold_at": {"$gte": start_at, "$lt": end_at}}},
                {"$addFields": {
                    "ccy": {"$ifNull": ["$last_sale.sold_currency", {"$ifNull": ["$listing_currency", {"$ifNull": ["$cost_currency", "$currency"]}]}]},
                    "amount": {"$ifNull": ["$last_sale.sold_price", {"$ifNull": ["$listing_price", 0]}]},
                }},
                {"$match": {"ccy": "SGD"}},
                {"$group": {
                    "_id": {"$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$sold_at",
                        "timezone": MONGO_BUSINESS_TIMEZONE,
                    }},
                    "total": {"$sum": "$amount"},
                }},
            ],
            "profit": [
                {"$match": {"status": "SOLD", "sold_record.0": {"$exists": True}}},
                {"$addFields": {
                    "sold_at": {"$arrayElemAt": ["$sold_record.sold_at", -1]},
                    "ccy": {"$ifNull": ["$profit_currency", {"$ifNull": ["$cost_currency", "$currency"]}]},
                    "amount": {"$ifNull": ["$profit", 0]},
                }},
                {"$match": {
                    "sold_at": {"$gte": start_at, "$lt": end_at},
                    "ccy": "RMB",
                }},
                {"$group": {
                    "_id": {"$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$sold_at",
                        "timezone": MONGO_BUSINESS_TIMEZONE,
                    }},
                    "total": {"$sum": "$amount"},
                }},
            ],
        }
    }]))
    payload = rows[0] if rows else {}
    day_totals = {}
    for source, target in (
        ("purchase", "purchase_cost_rmb"),
        ("sales", "sales_total_sgd"),
        ("profit", "profit_total_rmb"),
    ):
        for row in payload.get(source) or []:
            day = row.get("_id")
            if day:
                day_totals.setdefault(day, {})[target] = int(row.get("total", 0) or 0)
    return day_totals


def _annual_totals_from_months(profit_map, sales_map, current_year):
    """Sum monthly currency-specific totals for the current and prior 3 years."""
    annual = []
    for year in range(current_year, current_year - 4, -1):
        prefix = f"{year}-"
        annual.append({
            "year": year,
            "sales_sgd": sum(
                amount for month, amount in sales_map.items()
                if month.startswith(prefix)
            ),
            "profit_rmb": sum(
                amount for month, amount in profit_map.items()
                if month.startswith(prefix)
            ),
        })
    return annual


def register(app, items):
    @app.get("/admin/database-backup")
    @require_roles(ROLE_ADMIN)
    def download_database_backup():
        backup = _build_database_backup(items.database)
        filename = (
            "inventory_backup_"
            + now().astimezone(BUSINESS_TZ).strftime("%Y-%m-%d_%H%M%S")
            + ".zip"
        )
        response = send_file(
            backup,
            mimetype="application/zip",
            as_attachment=True,
            download_name=filename,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        total = items.count_documents({})
        on_shelf = items.count_documents({"status": "ON_SHELF"})
        in_stock = items.count_documents({"status": {"$in": ["INBOUND", "RECEIVED", "ON_SHELF", "RESERVED", "REPARING"]}})
        sold = items.count_documents({"status": "SOLD"})
        inventory_values = _inventory_value_totals(items)

        # NOTE: do NOT mix currencies. Keep consistent with analytics/monthly tables:
        # - sales_total_sgd: only items where sold_currency (fallback listing/cost currency) == SGD
        # - profit_total_rmb: only items where profit_currency (fallback cost_currency) == RMB
        # - cost_total_rmb: only items where cost_currency == RMB
        pipeline = [
            {"$match": {"status": "SOLD", "sold_record.0": {"$exists": True}}},
            {"$addFields": {
                "last_sale": {"$arrayElemAt": ["$sold_record", -1]},
                "sold_at": {"$arrayElemAt": ["$sold_record.sold_at", -1]},
            }},
            {"$match": {"sold_at": {"$ne": None}}},
            {"$addFields": {
                "sold_ccy": {"$ifNull": ["$last_sale.sold_currency", {"$ifNull": ["$listing_currency", "$cost_currency"]}]},
                "profit_ccy": {"$ifNull": ["$profit_currency", {"$ifNull": ["$cost_currency", "$currency"]}]},
                "cost_ccy": {"$ifNull": ["$cost_currency", {"$ifNull": ["$currency", ""]}]},
                "sold_price_num": {"$ifNull": ["$last_sale.sold_price", {"$ifNull": ["$listing_price", 0]}]},
                "profit_num": {"$ifNull": ["$profit", 0]},
                "cost_num": {"$ifNull": ["$cost", 0]},
            }},
            {"$group": {
                "_id": None,
                "sold_cnt": {"$sum": 1},
                "sales_total_sgd": {"$sum": {"$cond": [{"$eq": ["$sold_ccy", "SGD"]}, "$sold_price_num", 0]}},
                "profit_total_rmb": {"$sum": {"$cond": [{"$eq": ["$profit_ccy", "RMB"]}, "$profit_num", 0]}},
                "cost_total_rmb": {"$sum": {"$cond": [{"$eq": ["$cost_ccy", "RMB"]}, "$cost_num", 0]}},
            }}
        ]
        agg = list(items.aggregate(pipeline))
        stats = agg[0] if agg else {"sold_cnt": 0, "sales_total_sgd": 0, "profit_total_rmb": 0, "cost_total_rmb": 0}

        # --- monthly breakdown (profit RMB, sales SGD) ---
        base_match = {"status": "SOLD", "sold_record.0": {"$exists": True}}

        count_month_pipeline = [
            {"$match": base_match},
            {"$addFields": {"sold_at": {"$arrayElemAt": ["$sold_record.sold_at", -1]}}},
            {"$match": {"sold_at": {"$ne": None}}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m", "date": "$sold_at", "timezone": MONGO_BUSINESS_TIMEZONE}},
                "sold_cnt": {"$sum": 1},
            }},
            {"$sort": {"_id": -1}},
        ]

        profit_month_pipeline = [
            {"$match": base_match},
            {"$addFields": {"sold_at": {"$arrayElemAt": ["$sold_record.sold_at", -1]}}},
            {"$match": {"sold_at": {"$ne": None}}},
            {"$addFields": {"profit_ccy": {"$ifNull": ["$profit_currency", {"$ifNull": ["$cost_currency", "$currency"]}]}}},
            {"$match": {"profit_ccy": "RMB"}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m", "date": "$sold_at", "timezone": MONGO_BUSINESS_TIMEZONE}},
                "profit_rmb": {"$sum": {"$ifNull": ["$profit", 0]}},
                "profit_cnt_rmb": {"$sum": 1},
            }},
            {"$sort": {"_id": -1}},
        ]

        sales_month_pipeline = [
            {"$match": base_match},
            {"$addFields": {
                "last_sale": {"$arrayElemAt": ["$sold_record", -1]},
                "sold_at": {"$arrayElemAt": ["$sold_record.sold_at", -1]},
            }},
            {"$match": {"sold_at": {"$ne": None}}},
            {"$addFields": {"sold_ccy": {"$ifNull": ["$last_sale.sold_currency", {"$ifNull": ["$listing_currency", {"$ifNull": ["$cost_currency", "$currency"]}]}]}}},
            {"$match": {"sold_ccy": "SGD"}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m", "date": "$sold_at", "timezone": MONGO_BUSINESS_TIMEZONE}},
                "sales_sgd": {"$sum": {"$ifNull": ["$last_sale.sold_price", {"$ifNull": ["$listing_price", 0]}]}},
            }},
            {"$sort": {"_id": -1}},
        ]

        count_rows = list(items.aggregate(count_month_pipeline))
        profit_rows = list(items.aggregate(profit_month_pipeline))
        sales_rows = list(items.aggregate(sales_month_pipeline))

        sold_cnt_map = {r["_id"]: int(r.get("sold_cnt", 0) or 0) for r in count_rows}
        profit_map = {r["_id"]: int(r.get("profit_rmb", 0) or 0) for r in profit_rows}
        profit_cnt_map = {r["_id"]: int(r.get("profit_cnt_rmb", 0) or 0) for r in profit_rows}
        sales_map = {r["_id"]: int(r.get("sales_sgd", 0) or 0) for r in sales_rows}

        current_business_year = now().astimezone(BUSINESS_TZ).year
        annual = _annual_totals_from_months(
            profit_map, sales_map, current_business_year
        )

        months = sorted(set(sold_cnt_map.keys()) | set(profit_map.keys()) | set(sales_map.keys()), reverse=True)
        monthly = []
        for m in months[:36]:
            pr = profit_map.get(m, 0)
            pr_cnt = profit_cnt_map.get(m, 0)
            avg_pr = (pr / pr_cnt) if pr_cnt else 0
            monthly.append({
                "month": m,
                "sold_cnt": sold_cnt_map.get(m, 0),
                "profit_rmb": pr,
                "avg_profit_rmb": avg_pr,
                "sales_sgd": sales_map.get(m, 0),
            })

        # --- automatic recent three-day summary in the UTC+8 business timezone ---
        business_today = now().astimezone(BUSINESS_TZ).date()
        recent_dates = [business_today - timedelta(days=offset) for offset in range(3)]
        recent_start = parse_date_yyyy_mm_dd(recent_dates[-1].isoformat())
        recent_end = parse_date_yyyy_mm_dd(
            (recent_dates[0] + timedelta(days=1)).isoformat()
        )
        recent_totals_map = _daily_totals_by_day(items, recent_start, recent_end)
        recent_daily = []
        for business_date in recent_dates:
            day_label = business_date.isoformat()
            values = recent_totals_map.get(day_label, {})
            recent_daily.append({
                "day": day_label,
                "purchase_cost_rmb": values.get("purchase_cost_rmb", 0),
                "sales_total_sgd": values.get("sales_total_sgd", 0),
                "profit_total_rmb": values.get("profit_total_rmb", 0),
            })

        # --- daily breakdown (similar to searching YYYY-MM-DD in /items) ---
        day_str = (request.args.get("day") or "").strip()
        day_dt0 = parse_date_yyyy_mm_dd(day_str)
        daily = None
        if day_dt0:
            day_dt1 = day_dt0 + timedelta(days=1)

            daily_filter = {"$or": [
                {"purchase_at": {"$gte": day_dt0, "$lt": day_dt1}},
                {"sold_record.sold_at": {"$gte": day_dt0, "$lt": day_dt1}},
                {"created_at": {"$gte": day_dt0, "$lt": day_dt1}},
            ]}
            daily_items = list(items.find(daily_filter).sort("created_at", DESCENDING).limit(1000))
            for d in daily_items:
                sr = d.get("sold_record") or []
                d["_sold"] = sr[-1] if sr else {}

            selected_totals = _daily_totals_by_day(items, day_dt0, day_dt1).get(
                day_str, {}
            )

            daily = {
                "day": day_str,
                "items": daily_items,
                "purchase_cost_rmb": selected_totals.get("purchase_cost_rmb", 0),
                "sales_total_sgd": selected_totals.get("sales_total_sgd", 0),
                "profit_total_rmb": selected_totals.get("profit_total_rmb", 0),
            }

        return render_template(
            "index.html",
            total=total,
            in_stock=in_stock,
            on_shelf=on_shelf,
            sold=sold,
            inventory_values=inventory_values,
            stats=stats,
            annual=annual,
            STATUS_ZH=STATUS_ZH,
            daily=daily,
            recent_daily=recent_daily,
            monthly=monthly,
        )


