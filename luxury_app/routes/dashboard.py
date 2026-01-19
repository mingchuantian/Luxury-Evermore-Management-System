from flask import render_template, request
from pymongo import DESCENDING

from models import now

from ..constants import STATUS_ZH
from ..utils import parse_date_yyyy_mm_dd
from datetime import timedelta


def register(app, items):
    @app.get("/")
    def index():
        total = items.count_documents({})
        on_shelf = items.count_documents({"status": "ON_SHELF"})
        in_stock = items.count_documents({"status": {"$in": ["INBOUND", "RECEIVED", "ON_SHELF", "RESERVED", "REPARING"]}})
        sold = items.count_documents({"status": "SOLD"})

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
                "_id": {"$dateToString": {"format": "%Y-%m", "date": "$sold_at"}},
                "sold_cnt": {"$sum": 1},
            }},
            {"$sort": {"_id": -1}},
            {"$limit": 36},
        ]

        profit_month_pipeline = [
            {"$match": base_match},
            {"$addFields": {"sold_at": {"$arrayElemAt": ["$sold_record.sold_at", -1]}}},
            {"$match": {"sold_at": {"$ne": None}}},
            {"$addFields": {"profit_ccy": {"$ifNull": ["$profit_currency", {"$ifNull": ["$cost_currency", "$currency"]}]}}},
            {"$match": {"profit_ccy": "RMB"}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m", "date": "$sold_at"}},
                "profit_rmb": {"$sum": {"$ifNull": ["$profit", 0]}},
                "profit_cnt_rmb": {"$sum": 1},
            }},
            {"$sort": {"_id": -1}},
            {"$limit": 36},
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
                "_id": {"$dateToString": {"format": "%Y-%m", "date": "$sold_at"}},
                "sales_sgd": {"$sum": {"$ifNull": ["$last_sale.sold_price", {"$ifNull": ["$listing_price", 0]}]}},
            }},
            {"$sort": {"_id": -1}},
            {"$limit": 36},
        ]

        count_rows = list(items.aggregate(count_month_pipeline))
        profit_rows = list(items.aggregate(profit_month_pipeline))
        sales_rows = list(items.aggregate(sales_month_pipeline))

        sold_cnt_map = {r["_id"]: int(r.get("sold_cnt", 0) or 0) for r in count_rows}
        profit_map = {r["_id"]: int(r.get("profit_rmb", 0) or 0) for r in profit_rows}
        profit_cnt_map = {r["_id"]: int(r.get("profit_cnt_rmb", 0) or 0) for r in profit_rows}
        sales_map = {r["_id"]: int(r.get("sales_sgd", 0) or 0) for r in sales_rows}

        months = sorted(set(sold_cnt_map.keys()) | set(profit_map.keys()) | set(sales_map.keys()), reverse=True)
        monthly = []
        for m in months:
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

            purchase_cost_rmb = list(items.aggregate([
                {"$match": {"status": "INBOUND"}},
                {"$addFields": {
                    "dt": {"$ifNull": ["$purchase_at", "$created_at"]},
                    "ccy": {"$ifNull": ["$cost_currency", {"$ifNull": ["$currency", ""]}]},
                    "cost_num": {"$ifNull": ["$cost", 0]},
                }},
                {"$match": {"dt": {"$gte": day_dt0, "$lt": day_dt1}}},
                {"$match": {"ccy": "RMB"}},
                {"$group": {"_id": None, "total": {"$sum": "$cost_num"}}},
            ]))
            daily_purchase_cost_rmb = int(purchase_cost_rmb[0]["total"]) if purchase_cost_rmb else 0

            sales_sgd = list(items.aggregate([
                {"$match": {"status": "SOLD", "sold_record.0": {"$exists": True}}},
                {"$addFields": {
                    "last_sale": {"$arrayElemAt": ["$sold_record", -1]},
                    "sold_at": {"$arrayElemAt": ["$sold_record.sold_at", -1]},
                }},
                {"$match": {"sold_at": {"$gte": day_dt0, "$lt": day_dt1}}},
                {"$addFields": {"sold_ccy": {"$ifNull": ["$last_sale.sold_currency", {"$ifNull": ["$listing_currency", {"$ifNull": ["$cost_currency", "$currency"]}]}]}}},
                {"$match": {"sold_ccy": "SGD"}},
                {"$group": {"_id": None, "total": {"$sum": {"$ifNull": ["$last_sale.sold_price", {"$ifNull": ["$listing_price", 0]}]}}}},
            ]))
            daily_sales_total_sgd = int(sales_sgd[0]["total"]) if sales_sgd else 0

            profit_rmb = list(items.aggregate([
                {"$match": {"status": "SOLD", "sold_record.0": {"$exists": True}}},
                {"$addFields": {"sold_at": {"$arrayElemAt": ["$sold_record.sold_at", -1]}}},
                {"$match": {"sold_at": {"$gte": day_dt0, "$lt": day_dt1}}},
                {"$addFields": {"profit_ccy": {"$ifNull": ["$profit_currency", {"$ifNull": ["$cost_currency", "$currency"]}]}}},
                {"$match": {"profit_ccy": "RMB"}},
                {"$group": {"_id": None, "total": {"$sum": {"$ifNull": ["$profit", 0]}}}},
            ]))
            daily_profit_total_rmb = int(profit_rmb[0]["total"]) if profit_rmb else 0

            daily = {
                "day": day_str,
                "items": daily_items,
                "purchase_cost_rmb": daily_purchase_cost_rmb,
                "sales_total_sgd": daily_sales_total_sgd,
                "profit_total_rmb": daily_profit_total_rmb,
            }

        return render_template(
            "index.html",
            total=total,
            in_stock=in_stock,
            on_shelf=on_shelf,
            sold=sold,
            stats=stats,
            STATUS_ZH=STATUS_ZH,
            daily=daily,
            monthly=monthly,
        )


