def register(app, items):
    from ..constants import STATUS_ZH  # noqa: F401 (kept for templates if needed later)
    from ..utils import MONGO_BUSINESS_TIMEZONE

    @app.get("/analytics")
    def analytics():
        # 只看已售，并以 sold_at 的月份聚合
        base_match = {"status": "SOLD", "sold_record.0": {"$exists": True}}

        # 利润：优先 profit_currency，没有则回退 cost_currency（再回退 legacy currency）
        profit_pipeline = [
            {"$match": base_match},
            {"$addFields": {"sold_at": {"$arrayElemAt": ["$sold_record.sold_at", -1]}}},
            {"$match": {"sold_at": {"$ne": None}}},
            {"$addFields": {"profit_ccy": {"$ifNull": ["$profit_currency", {"$ifNull": ["$cost_currency", "$currency"]}]}}},
            {"$match": {"profit_ccy": "RMB"}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m", "date": "$sold_at", "timezone": MONGO_BUSINESS_TIMEZONE}},
                "profit_total": {"$sum": {"$ifNull": ["$profit", 0]}},
                "cnt": {"$sum": 1},
            }},
            {"$sort": {"_id": 1}},
        ]

        # 销售额（SGD）：优先 sold_currency，没有则回退 listing_currency，再回退 cost_currency（再回退 legacy currency）
        sales_pipeline = [
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
                "sales_total": {"$sum": {"$ifNull": ["$last_sale.sold_price", {"$ifNull": ["$listing_price", 0]}]}},
                "cnt": {"$sum": 1},
            }},
            {"$sort": {"_id": 1}},
        ]

        profit_rows = list(items.aggregate(profit_pipeline))
        sales_rows = list(items.aggregate(sales_pipeline))

        months = sorted({r["_id"] for r in profit_rows} | {r["_id"] for r in sales_rows})

        profit_map = {r["_id"]: int(r.get("profit_total", 0) or 0) for r in profit_rows}
        profit_cnt_map = {r["_id"]: int(r.get("cnt", 0) or 0) for r in profit_rows}
        sales_map = {r["_id"]: int(r.get("sales_total", 0) or 0) for r in sales_rows}

        profit_total_series = [profit_map.get(m, 0) for m in months]
        profit_avg_series = [
            (profit_map.get(m, 0) / profit_cnt_map.get(m, 1)) if profit_cnt_map.get(m, 0) else 0
            for m in months
        ]
        sales_total_series = [sales_map.get(m, 0) for m in months]

        # --- sold count breakdown (weekday / day-of-month) ---
        weekday_rows = list(items.aggregate([
            {"$match": base_match},
            {"$addFields": {"sold_at": {"$arrayElemAt": ["$sold_record.sold_at", -1]}}},
            {"$match": {"sold_at": {"$ne": None}}},
            {"$group": {"_id": {"$dayOfWeek": {"date": "$sold_at", "timezone": MONGO_BUSINESS_TIMEZONE}}, "cnt": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]))
        weekday_map = {int(r["_id"]): int(r.get("cnt", 0) or 0) for r in weekday_rows}
        # Mongo: 1=Sunday ... 7=Saturday. We display Monday..Sunday.
        weekday_order = [2, 3, 4, 5, 6, 7, 1]
        weekday_labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday_counts = [weekday_map.get(i, 0) for i in weekday_order]

        # day-of-month grouped by 3-day buckets: 1-3, 4-6, ..., 28-30, 31-31
        dom_bucket_rows = list(items.aggregate([
            {"$match": base_match},
            {"$addFields": {"sold_at": {"$arrayElemAt": ["$sold_record.sold_at", -1]}}},
            {"$match": {"sold_at": {"$ne": None}}},
            {"$addFields": {"dom": {"$dayOfMonth": {"date": "$sold_at", "timezone": MONGO_BUSINESS_TIMEZONE}}}},
            {"$addFields": {
                "bucket_start": {"$subtract": ["$dom", {"$mod": [{"$subtract": ["$dom", 1]}, 3]}]}
            }},
            {"$group": {"_id": "$bucket_start", "cnt": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]))
        dom_bucket_map = {int(r["_id"]): int(r.get("cnt", 0) or 0) for r in dom_bucket_rows}
        day_of_month_bucket_starts = list(range(1, 32, 3))
        day_of_month_bucket_labels = [
            f"{s:02d}-{min(s+2, 31):02d}" for s in day_of_month_bucket_starts
        ]
        day_of_month_bucket_counts = [dom_bucket_map.get(s, 0) for s in day_of_month_bucket_starts]

        # --- inventory by month: stacked count by brand + line cost (RMB) ---
        in_stock_statuses = ["INBOUND", "REPARING", "RECEIVED", "RESERVED", "ON_SHELF"]
        inv_rows = list(items.aggregate([
            {"$match": {"status": {"$in": in_stock_statuses}}},
            {"$addFields": {
                "dt": {"$ifNull": ["$purchase_at", "$created_at"]},
                "ccy": {"$ifNull": ["$cost_currency", {"$ifNull": ["$currency", ""]}]},
                "brand_norm": {"$ifNull": ["$brand", "Unknown"]},
                "cost_num": {"$ifNull": ["$cost", 0]},
            }},
            {"$group": {
                "_id": {
                    "month": {"$dateToString": {"format": "%Y-%m", "date": "$dt", "timezone": MONGO_BUSINESS_TIMEZONE}},
                    "brand": "$brand_norm",
                },
                "cnt": {"$sum": 1},
                "cost_rmb": {"$sum": {"$cond": [
                    {"$eq": ["$ccy", "RMB"]},
                    "$cost_num",
                    0
                ]}},
            }},
            {"$sort": {"_id.month": 1}},
        ]))

        inv_months = sorted({r["_id"]["month"] for r in inv_rows})
        brand_total = {}
        month_brand_cnt = {}
        month_cost_rmb = {m: 0 for m in inv_months}
        for r in inv_rows:
            m = r["_id"]["month"]
            b = r["_id"]["brand"]
            c = int(r.get("cnt", 0) or 0)
            brand_total[b] = brand_total.get(b, 0) + c
            month_brand_cnt[(m, b)] = c
            month_cost_rmb[m] = int(month_cost_rmb.get(m, 0) + (r.get("cost_rmb", 0) or 0))

        if len(inv_months) > 24:
            inv_months = inv_months[-24:]

        top_n = 8
        top_brands = [b for b, _ in sorted(brand_total.items(), key=lambda kv: kv[1], reverse=True)[:top_n]]
        inv_brand_labels = top_brands + ["Other"]

        inv_counts_by_brand = []
        for b in inv_brand_labels:
            series = []
            for m in inv_months:
                if b == "Other":
                    v = 0
                    for b2 in brand_total.keys():
                        if b2 in top_brands:
                            continue
                        v += month_brand_cnt.get((m, b2), 0)
                    series.append(v)
                else:
                    series.append(month_brand_cnt.get((m, b), 0))
            inv_counts_by_brand.append(series)

        inv_cost_rmb_series = [month_cost_rmb.get(m, 0) for m in inv_months]

        # --- purchases by month: stacked count by brand + line cost (RMB), ignore status ---
        purchase_rows = list(items.aggregate([
            {"$addFields": {
                "dt": {"$ifNull": ["$purchase_at", "$created_at"]},
                "ccy": {"$ifNull": ["$cost_currency", {"$ifNull": ["$currency", ""]}]},
                "brand_norm": {"$ifNull": ["$brand", "Unknown"]},
                "cost_num": {"$ifNull": ["$cost", 0]},
            }},
            {"$group": {
                "_id": {
                    "month": {"$dateToString": {"format": "%Y-%m", "date": "$dt", "timezone": MONGO_BUSINESS_TIMEZONE}},
                    "brand": "$brand_norm",
                },
                "cnt": {"$sum": 1},
                "cost_rmb": {"$sum": {"$cond": [
                    {"$eq": ["$ccy", "RMB"]},
                    "$cost_num",
                    0
                ]}},
            }},
            {"$sort": {"_id.month": 1}},
        ]))

        purchase_months = sorted({r["_id"]["month"] for r in purchase_rows})
        purchase_brand_total = {}
        purchase_month_brand_cnt = {}
        purchase_month_cost_rmb = {m: 0 for m in purchase_months}
        for r in purchase_rows:
            m = r["_id"]["month"]
            b = r["_id"]["brand"]
            c = int(r.get("cnt", 0) or 0)
            purchase_brand_total[b] = purchase_brand_total.get(b, 0) + c
            purchase_month_brand_cnt[(m, b)] = c
            purchase_month_cost_rmb[m] = int(purchase_month_cost_rmb.get(m, 0) + (r.get("cost_rmb", 0) or 0))

        if len(purchase_months) > 24:
            purchase_months = purchase_months[-24:]

        top_n_purchase = 8
        purchase_top_brands = [b for b, _ in sorted(purchase_brand_total.items(), key=lambda kv: kv[1], reverse=True)[:top_n_purchase]]
        purchase_brand_labels = purchase_top_brands + ["Other"]

        purchase_counts_by_brand = []
        for b in purchase_brand_labels:
            series = []
            for m in purchase_months:
                if b == "Other":
                    v = 0
                    for b2 in purchase_brand_total.keys():
                        if b2 in purchase_top_brands:
                            continue
                        v += purchase_month_brand_cnt.get((m, b2), 0)
                    series.append(v)
                else:
                    series.append(purchase_month_brand_cnt.get((m, b), 0))
            purchase_counts_by_brand.append(series)

        purchase_cost_rmb_series = [purchase_month_cost_rmb.get(m, 0) for m in purchase_months]

        from flask import render_template
        return render_template(
            "analytics.html",
            months=months,
            profit_total_series=profit_total_series,
            profit_avg_series=profit_avg_series,
            sales_total_series=sales_total_series,
            weekday_labels=weekday_labels,
            weekday_counts=weekday_counts,
            day_of_month_labels=day_of_month_bucket_labels,
            day_of_month_counts=day_of_month_bucket_counts,
            inv_months=inv_months,
            inv_brand_labels=inv_brand_labels,
            inv_counts_by_brand=inv_counts_by_brand,
            inv_cost_rmb_series=inv_cost_rmb_series,
            purchase_months=purchase_months,
            purchase_brand_labels=purchase_brand_labels,
            purchase_counts_by_brand=purchase_counts_by_brand,
            purchase_cost_rmb_series=purchase_cost_rmb_series,
        )


