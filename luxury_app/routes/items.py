import re
from math import ceil
from datetime import timedelta, timezone

from bson import ObjectId
from flask import Response, flash, redirect, render_template, request, send_file, url_for
from pymongo import DESCENDING

from models import STATUS, now

from ..audit import (
    insert_audit_logs,
    make_change_detail,
    make_change_status,
    push_item_audits,
)
from ..constants import BRAND_OPTIONS, SELLABLE_STATUSES, STATUS_ZH
from ..utils import (
    find_date_yyyy_mm_dd_in_text,
    gen_sku_unique,
    is_object_id,
    money_int,
    parse_date_yyyy_mm_dd,
    parse_datetime_local_to_utc,
)
from receipt_template.receipt_generation import generate_receipt_docx_bytes


def register(app, items, audit_logs=None):
    def _dt8_date_str(value) -> str:
        """
        Format a datetime (aware/naive/iso str) as YYYY-MM-DD in UTC+8.
        """
        from datetime import datetime

        if value is None or value == "":
            return ""
        dt = None
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                return value
        else:
            return str(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt8_tz = timezone(timedelta(hours=8))
        return dt.astimezone(dt8_tz).strftime("%Y-%m-%d")

    def _fmt_money(ccy: str, amount) -> str:
        try:
            amt = int(amount or 0)
        except Exception:
            amt = 0
        c = (ccy or "").strip().upper()
        if not c and amt == 0:
            return ""
        if not c:
            return f"{amt:,}"
        return f"{c} {amt:,}"

    def _render_agreement_html(*, template_relpath: str, mapping: dict) -> str:
        """
        Render a static HTML template by string-replacing placeholder tokens.
        These templates are NOT Jinja (they use placeholders like {{ITEM NAME}}).
        """
        from pathlib import Path

        project_root = Path(__file__).resolve().parents[2]
        tpl_path = (project_root / template_relpath).resolve()
        html = tpl_path.read_text(encoding="utf-8")
        for k, v in (mapping or {}).items():
            html = html.replace(k, "" if v is None else str(v))
        return html

    def _attach_sold_view_fields(doc: dict) -> dict:
        """
        Attach a computed view-only field `_sold` (last sold_record entry) for templates.
        This keeps templates simple after removing legacy top-level sale fields.
        """
        sr = doc.get("sold_record") or []
        doc["_sold"] = sr[-1] if sr else {}
        return doc

    # ---------------- Items 列表（全文搜索）----------------
    @app.get("/items")
    def list_items():
        q = request.args.get("q", "").strip()
        status = request.args.get("status", "").strip()
        source_type = request.args.get("source_type", "").strip()  # BUY_IN / CONSIGNMENT

        # pagination (cap at 1000 per page)
        try:
            page = int(request.args.get("page", "1"))
        except Exception:
            page = 1
        if page < 1:
            page = 1
        per_page = 1000

        filt = {}
        if status:
            filt["status"] = status
        if source_type:
            filt["source_type"] = source_type

        if q:
            q_esc = re.escape(q[:80])
            filt["$or"] = [
                {"sku": {"$regex": q_esc, "$options": "i"}},
                {"name": {"$regex": q_esc, "$options": "i"}},
                {"name_in_EN": {"$regex": q_esc, "$options": "i"}},
                {"brand": {"$regex": q_esc, "$options": "i"}},
                {"seller_name": {"$regex": q_esc, "$options": "i"}},
                {"seller_contact": {"$regex": q_esc, "$options": "i"}},
                {"note": {"$regex": q_esc, "$options": "i"}},
                {"additional_notes_for_agreements": {"$regex": q_esc, "$options": "i"}},
                {"serial_code": {"$regex": q_esc, "$options": "i"}},
                {"tracking_number": {"$regex": q_esc, "$options": "i"}},
                {"accessories": {"$regex": q_esc, "$options": "i"}},
            ]

            # 支持按“状态中文/英文”搜索（匹配 status 字段）
            status_kw = {
                "已售出": "SOLD",
                "售出": "SOLD",
                "护理中": "REPARING",
                "已到货": "RECEIVED",
                "到货": "RECEIVED",
                "上架": "ON_SHELF",
                "预留": "RESERVED",
                "缺件": "MISSING",
                "损坏": "DAMAGED",
                "退货": "RETURNED",
                "已录入": "INBOUND",
                "未到货": "INBOUND",
            }
            for kw, st in status_kw.items():
                if kw in q:
                    filt["$or"].append({"status": st})

            q_upper = q.strip().upper()
            if q_upper in STATUS:
                filt["$or"].append({"status": q_upper})

            # 支持按日期（YYYY-MM-DD）搜索：purchase_at / sold_at，并兼容旧数据回退到 created_at
            dt0 = find_date_yyyy_mm_dd_in_text(q)
            if dt0:
                dt1 = dt0 + timedelta(days=1)
                filt["$or"].append({"purchase_at": {"$gte": dt0, "$lt": dt1}})
                # sold_at is stored in sold_record[*].sold_at
                filt["$or"].append({"sold_record.sold_at": {"$gte": dt0, "$lt": dt1}})
                filt["$or"].append({"created_at": {"$gte": dt0, "$lt": dt1}})

        total = items.count_documents(filt)
        total_pages = max(1, ceil(total / per_page)) if total else 1
        if page > total_pages:
            page = total_pages

        skip = (page - 1) * per_page
        docs = list(
            items.find(filt)
            .sort("created_at", DESCENDING)
            .skip(skip)
            .limit(per_page)
        )
        for d in docs:
            _attach_sold_view_fields(d)
        return render_template(
            "items.html",
            items=docs,
            STATUS=STATUS,
            STATUS_ZH=STATUS_ZH,
            q=q,
            status=status,
            source_type=source_type,
            page=page,
            per_page=per_page,
            total=total,
            total_pages=total_pages,
        )

    # ---------------- 新增：收货/寄卖 两个入口 ----------------
    @app.get("/items/new/buyin")
    def item_new_buyin_form():
        return render_template(
            "item_new.html",
            entry_type="BUY_IN",
            entry_type_zh="新增收货",
            BRAND_OPTIONS=BRAND_OPTIONS,
        )

    @app.get("/items/new/consignment")
    def item_new_consignment_form():
        return render_template(
            "item_new.html",
            entry_type="CONSIGNMENT",
            entry_type_zh="新增寄卖",
            BRAND_OPTIONS=BRAND_OPTIONS,
        )

    @app.post("/items/new")
    def item_new_create():
        entry_type = request.form.get("entry_type", "BUY_IN").strip().upper()
        if entry_type not in ["BUY_IN", "CONSIGNMENT"]:
            entry_type = "BUY_IN"

        name = request.form.get("name", "").strip()
        name_in_EN = request.form.get("name_in_EN", "").strip()
        additional_notes_for_agreements = request.form.get("additional_notes_for_agreements", "").strip()
        # cost currency (legacy field name: currency)
        cost_currency = (request.form.get("cost_currency") or request.form.get("currency") or "SGD").strip().upper()
        cost = money_int(request.form.get("cost"))
        note = request.form.get("note", "").strip()
        serial_code = request.form.get("serial_code", "").strip()
        tracking_number = request.form.get("tracking_number", "").strip()
        accessories = request.form.get("accessories", "").strip()

        purchase_at = parse_datetime_local_to_utc(
            request.form.get("purchase_at", ""),
            request.form.get("tz_offset_min", "0"),
        )
        if not purchase_at:
            purchase_at = parse_date_yyyy_mm_dd(request.form.get("purchase_date", ""))

        brand_select = request.form.get("brand_select", "").strip()
        brand_custom = request.form.get("brand_custom", "").strip()
        brand = brand_custom if brand_select == "其他" else brand_select

        seller_name = request.form.get("seller_name", "").strip()
        seller_contact = request.form.get("seller_contact", "").strip()

        if not name:
            flash("商品名称不能为空", "error")
            back = "item_new_buyin_form" if entry_type == "BUY_IN" else "item_new_consignment_form"
            return redirect(url_for(back))

        sku = gen_sku_unique(items, brand)
        doc = {
            "sku": sku,
            "name": name,
            "name_in_EN": name_in_EN,
            "brand": brand,
            "seller_name": seller_name,
            "seller_contact": seller_contact,
            "cost_currency": cost_currency,
            "cost": cost,
            "status": "INBOUND",
            "note": note,
            "additional_notes_for_agreements": additional_notes_for_agreements,
            "created_at": now(),
            "updated_at": now(),
            "purchase_at": purchase_at or now(),
            "received_at": None,
            "serial_code": serial_code,
            "tracking_number": tracking_number,
            "accessories": accessories,
            "source_type": entry_type,
            "is_buy_in": entry_type == "BUY_IN",
            "is_consignment": entry_type == "CONSIGNMENT",
            "audit": [],
        }

        res = items.insert_one(doc)
        item_oid = res.inserted_id
        # Audit: creation is represented as a status set + detail sets (from empty -> initial value)
        entries = [make_change_status(sku=sku, from_status="", to_status="INBOUND")]
        for k in [
            "name", "name_in_EN", "brand", "seller_name", "seller_contact",
            "cost_currency", "cost",
            "purchase_at", "source_type",
            "note", "serial_code", "tracking_number", "accessories",
            "additional_notes_for_agreements",
        ]:
            entries.append(make_change_detail(sku=sku, target=k, from_value="", to_value=doc.get(k, "")))
        push_item_audits(items, item_oid, entries)
        insert_audit_logs(audit_logs, entries)

        flash("已新增商品", "ok")
        return redirect(url_for("item_detail", item_key=sku))

    # ---------------- 商品详情 / 更新 ----------------  
    @app.get("/items/<item_key>")
    def item_detail(item_key):
        it = None
        if is_object_id(item_key):
            it = items.find_one({"_id": ObjectId(item_key)})
        if not it:
            it = items.find_one({"sku": item_key})
        if not it:
            return "未找到该商品", 404

        _attach_sold_view_fields(it)

        return render_template(
            "item_detail.html",
            item=it,
            STATUS=STATUS,
            STATUS_ZH=STATUS_ZH,
            BRAND_OPTIONS=BRAND_OPTIONS,
        )

    @app.get("/items/<item_id>/receipt")
    def item_receipt(item_id):
        """
        Regenerate a receipt docx for SOLD items from the latest sold_record entry.
        This is download-only and does NOT mutate MongoDB.
        """
        try:
            oid = ObjectId(item_id)
        except Exception:
            return "invalid id", 400

        it = items.find_one({"_id": oid})
        if not it:
            return "not found", 404
        if it.get("status") != "SOLD":
            return "invalid status", 400

        sr = it.get("sold_record") or []
        if not sr:
            return "no sold_record", 400
        sold = sr[-1] or {}

        buf = generate_receipt_docx_bytes(item=it, sold=sold)

        sku = (it.get("sku") or "").strip()
        receipt_no = (sold.get("receipt_no") or "").strip()
        filename_base = receipt_no or (f"{sku}_receipt" if sku else "receipt")
        filename_base = re.sub(r"[^0-9A-Za-z._-]+", "_", filename_base).strip("_") or "receipt"
        download_name = f"{filename_base}.docx"
        try:
            return send_file(
                buf,
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                as_attachment=True,
                download_name=download_name,
            )
        except TypeError:
            # Flask < 2.0 compatibility
            return send_file(
                buf,
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                as_attachment=True,
                attachment_filename=download_name,
            )

    @app.get("/items/<item_id>/agreement/purchase")
    def item_purchase_agreement(item_id):
        try:
            oid = ObjectId(item_id)
        except Exception:
            return "invalid id", 400

        it = items.find_one({"_id": oid})
        if not it:
            return "not found", 404
        if (it.get("source_type") or "").upper() != "BUY_IN":
            return "invalid source_type", 400

        sku = (it.get("sku") or "").strip()
        doc_no = sku
        mapping = {
            "{{REF NUMBER}}": doc_no,
            "{{DATE}}": _dt8_date_str(it.get("purchase_at") or it.get("created_at")),
            "{{NAME}}": (it.get("seller_name") or "").strip(),
            "{{PHONE OR EMAIL}}": (it.get("seller_contact") or "").strip(),
            # Per requirement: item name uses name_in_EN
            "{{ITEM NAME}}": (it.get("name_in_EN") or "").strip(),
            "{{PURCHASE PRICE}}": _fmt_money(it.get("cost_currency") or it.get("currency") or "", it.get("cost")),
            "{{ADDITIONAL NOTE}}": (it.get("additional_notes_for_agreements") or "").strip() or "N.A.",
            "{{METHOD OF PAYMENT}}": "N.A.",
            "{{PAYMENT STATUS}}": "Paid",
        }
        html = _render_agreement_html(
            template_relpath="purchase_agreement_template/purchase_agreement.html",
            mapping=mapping,
        )
        return Response(html, mimetype="text/html; charset=utf-8")

    @app.get("/items/<item_id>/agreement/consignment")
    def item_consignment_agreement(item_id):
        try:
            oid = ObjectId(item_id)
        except Exception:
            return "invalid id", 400

        it = items.find_one({"_id": oid})
        if not it:
            return "not found", 404
        if (it.get("source_type") or "").upper() != "CONSIGNMENT":
            return "invalid source_type", 400

        sku = (it.get("sku") or "").strip()
        agreement_no = sku
        mapping = {
            "{{AGREEMENT NUMBER}}": agreement_no,
            "{{DATE}}": _dt8_date_str(it.get("purchase_at") or it.get("created_at")),
            "{{NAME}}": (it.get("seller_name") or "").strip(),
            "{{PHONE OR EMAIL}}": (it.get("seller_contact") or "").strip(),
            # Per requirement: item name uses name_in_EN
            "{{ITEM NAME}}": (it.get("name_in_EN") or "").strip(),
            # Reuse cost as consignment payout quote (existing schema)
            "{{CONSIGNMENT PAYOUT QUOTE}}": _fmt_money(it.get("cost_currency") or it.get("currency") or "", it.get("cost")),
            "{{Additional NOTE}}": (it.get("additional_notes_for_agreements") or "").strip() or "N.A.",
        }
        # Support both spellings/cases if template changes
        mapping["{{ADDITIONAL NOTE}}"] = mapping["{{Additional NOTE}}"]

        html = _render_agreement_html(
            template_relpath="consignment_receipt_template/consignment_receipt.html",
            mapping=mapping,
        )
        return Response(html, mimetype="text/html; charset=utf-8")

    @app.post("/items/<item_id>/update")
    def item_update(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "未找到该商品", 404

        form = request.form

        cost = money_int(form.get("cost"))

        # Only allow status change when the select exists on the page.
        status = (form.get("status") or it.get("status") or "").strip()
        name = request.form.get("name", "").strip()
        name_in_EN = request.form.get("name_in_EN", None)
        if name_in_EN is not None:
            name_in_EN = name_in_EN.strip()
        note = request.form.get("note", "").strip()
        additional_notes_for_agreements = request.form.get("additional_notes_for_agreements", None)
        if additional_notes_for_agreements is not None:
            additional_notes_for_agreements = additional_notes_for_agreements.strip()
        serial_code = request.form.get("serial_code", "").strip()
        tracking_number = request.form.get("tracking_number", "").strip()
        accessories = request.form.get("accessories", "").strip()

        brand_select = request.form.get("brand_select", "").strip()
        brand_custom = request.form.get("brand_custom", "").strip()
        brand = brand_custom if brand_select == "其他" else brand_select

        seller_name = request.form.get("seller_name", "").strip()
        seller_contact = request.form.get("seller_contact", "").strip()

        if status not in STATUS:
            flash("无效状态", "error")
            return redirect(url_for("item_detail", item_key=item_id))

        # Build $set only from fields that are actually present on the page.
        # This prevents "hidden defaults" from being written when the UI doesn't show the field.
        set_fields = {
            "name": name,
            "brand": brand,
            "seller_name": seller_name,
            "seller_contact": seller_contact,
            "cost": cost,
            "status": status,
            "note": note,
            "serial_code": serial_code,
            "tracking_number": tracking_number,
            "accessories": accessories,
            "updated_at": now(),
        }
        if name_in_EN is not None:
            set_fields["name_in_EN"] = name_in_EN
        if additional_notes_for_agreements is not None:
            set_fields["additional_notes_for_agreements"] = additional_notes_for_agreements

        # Optional fields only if present in form
        profit_raw = form.get("profit", None)
        if profit_raw is not None and profit_raw.strip() != "":
            set_fields["profit"] = money_int(profit_raw)
        profit_ccy_raw = form.get("profit_currency", None)
        if profit_ccy_raw is not None and profit_ccy_raw.strip() != "":
            set_fields["profit_currency"] = profit_ccy_raw.strip().upper()

        listing_price_raw = form.get("listing_price", None)
        if listing_price_raw is not None and listing_price_raw.strip() != "":
            set_fields["listing_price"] = money_int(listing_price_raw)
        listing_ccy_raw = form.get("listing_currency", None)
        if listing_ccy_raw is not None and listing_ccy_raw.strip() != "":
            set_fields["listing_currency"] = listing_ccy_raw.strip().upper()

        # NOTE: sale details are stored in sold_record; do NOT allow writing legacy top-level sold_* fields.

        sku = it.get("sku") or ""
        entries = []

        old_status = it.get("status", "")
        if status != old_status:
            entries.append(make_change_status(sku=sku, from_status=old_status, to_status=status))

        # Detail diffs: one entry per changed field in the same "Save"
        for k, v in set_fields.items():
            if k in ("updated_at", "status"):
                continue
            old_v = it.get(k, "")
            if old_v != v:
                entries.append(make_change_detail(sku=sku, target=k, from_value=old_v, to_value=v))

        items.update_one({"_id": ObjectId(item_id)}, {"$set": set_fields})
        push_item_audits(items, ObjectId(item_id), entries)
        insert_audit_logs(audit_logs, entries)

        flash("商品已更新", "ok")
        return redirect(url_for("item_detail", item_key=item_id))

    # ---------------- 状态动作：到货/删除/快捷售出 ----------------
    @app.post("/items/<item_id>/action/receive")
    def action_receive(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "not found", 404
        if it.get("status") != "INBOUND":
            return "invalid status", 400

        sku = it.get("sku") or ""
        t = now()
        entries = [
            make_change_status(sku=sku, from_status="INBOUND", to_status="RECEIVED"),
            make_change_detail(sku=sku, target="received_at", from_value=it.get("received_at", ""), to_value=t),
        ]
        items.update_one(
            {"_id": ObjectId(item_id)},
            {"$set": {"status": "RECEIVED", "received_at": t, "updated_at": t}}
        )
        push_item_audits(items, ObjectId(item_id), entries)
        insert_audit_logs(audit_logs, entries)
        return "", 204

    @app.post("/items/<item_id>/action/delete")
    def action_delete(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        # Item is deleted, so only write to global audit logs.
        if audit_logs is not None and it and it.get("sku"):
            insert_audit_logs(audit_logs, [
                make_change_detail(sku=it.get("sku"), target="deleted", from_value="", to_value=True)
            ])
        items.delete_one({"_id": ObjectId(item_id)})
        return "", 204

    @app.post("/items/<item_id>/action/set_tracking_number")
    def action_set_tracking_number(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "not found", 404
        if it.get("status") != "INBOUND":
            return "invalid status", 400

        data = request.get_json(silent=True) or {}
        tracking_number = (data.get("tracking_number") or "").strip()
        if not tracking_number:
            return "missing tracking_number", 400

        sku = it.get("sku") or ""
        old_v = it.get("tracking_number", "")
        if old_v == tracking_number:
            return "", 204

        items.update_one(
            {"_id": ObjectId(item_id)},
            {"$set": {"tracking_number": tracking_number, "updated_at": now()}}
        )

        entry = make_change_detail(sku=sku, target="tracking_number", from_value=old_v, to_value=tracking_number)
        push_item_audits(items, ObjectId(item_id), [entry])
        insert_audit_logs(audit_logs, [entry])
        return "", 204

    @app.post("/items/<item_id>/action/set_serial_code")
    def action_set_serial_code(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "not found", 404

        data = request.get_json(silent=True) or {}
        serial_code = (data.get("serial_code") or "").strip()
        if not serial_code:
            return "missing serial_code", 400

        sku = it.get("sku") or ""
        old_v = it.get("serial_code", "")
        if old_v == serial_code:
            return "", 204

        items.update_one(
            {"_id": ObjectId(item_id)},
            {"$set": {"serial_code": serial_code, "updated_at": now()}}
        )

        entry = make_change_detail(sku=sku, target="serial_code", from_value=old_v, to_value=serial_code)
        push_item_audits(items, ObjectId(item_id), [entry])
        insert_audit_logs(audit_logs, [entry])
        return "", 204

    @app.post("/items/<item_id>/action/quick_arrival_repair")
    def action_quick_arrival_repair(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "not found", 404
        if it.get("status") != "INBOUND":
            return "invalid status", 400

        sku = it.get("sku") or ""
        t = now()
        entries = [
            make_change_status(sku=sku, from_status="INBOUND", to_status="REPARING"),
            make_change_detail(sku=sku, target="start_repair_at", from_value=it.get("start_repair_at", ""), to_value=t),
        ]
        items.update_one(
            {"_id": ObjectId(item_id)},
            {"$set": {"status": "REPARING", "start_repair_at": t, "updated_at": t}}
        )
        push_item_audits(items, ObjectId(item_id), entries)
        insert_audit_logs(audit_logs, entries)
        return "", 204

    @app.post("/items/<item_id>/action/quick_arrival_store")
    def action_quick_arrival_store(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "not found", 404
        if it.get("status") not in ("INBOUND", "REPARING"):
            return "invalid status", 400

        sku = it.get("sku") or ""
        from_status = it.get("status") or ""
        t = now()
        entries = [
            make_change_status(sku=sku, from_status=from_status, to_status="RECEIVED"),
            make_change_detail(sku=sku, target="received_at", from_value=it.get("received_at", ""), to_value=t),
        ]
        items.update_one(
            {"_id": ObjectId(item_id)},
            {"$set": {"status": "RECEIVED", "received_at": t, "updated_at": t}}
        )
        push_item_audits(items, ObjectId(item_id), entries)
        insert_audit_logs(audit_logs, entries)
        return "", 204

    @app.post("/items/<item_id>/action/sale")
    def action_sale(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "not found", 404
        if it.get("status") not in SELLABLE_STATUSES:
            return "invalid status", 400

        sku = it.get("sku") or ""
        t = now()
        sold_price = int(it.get("listing_price", 0) or 0)
        if sold_price <= 0:
            return "invalid sold price", 400
        sold_currency = (it.get("listing_currency") or it.get("cost_currency") or it.get("currency") or "SGD")
        # Receipt default uses UTC+8 date for operator friendliness
        t8 = t.astimezone(timezone(timedelta(hours=8)))
        receipt_no = f"{t8.strftime('%Y-%m-%d')}_{sku}" if sku else t8.strftime("%Y-%m-%d")
        record = {
            "sold_at": t,
            "buyer": "",
            "sale_note": "",
            "sold_currency": sold_currency,
            "sold_price": sold_price,
            "sale_channel": "",
            "payment_method": "",
            "receipt_no": receipt_no,
        }
        entries = [
            make_change_status(sku=sku, from_status=it.get("status", ""), to_status="SOLD"),
            make_change_detail(sku=sku, target="sold_at", from_value="", to_value=t),
            make_change_detail(sku=sku, target="sold_price", from_value="", to_value=sold_price),
            make_change_detail(sku=sku, target="sold_currency", from_value="", to_value=sold_currency),
        ]
        old_status = it.get("status")
        result = items.update_one(
            {"_id": ObjectId(item_id), "status": old_status},
            {"$set": {"status": "SOLD", "updated_at": t},
             "$push": {"sold_record": record}}
        )
        if result.modified_count != 1:
            return "item status changed", 409

        push_item_audits(items, ObjectId(item_id), entries)
        insert_audit_logs(audit_logs, entries)
        return "", 204

    # ---------------- 到达/回滚工作流 ----------------
    @app.route("/items/<item_id>/arrival_confirm", methods=["GET", "POST"])
    def arrival_confirm(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "未找到该商品", 404

        if request.method == "GET":
            mode = (request.args.get("mode") or "").strip().lower()
            if mode not in ("repair", "store"):
                return "invalid mode", 400

            st = it.get("status")
            if mode == "repair" and st != "INBOUND":
                return "invalid status", 400
            if mode == "store" and st not in ("INBOUND", "REPARING"):
                return "invalid status", 400

            if mode == "repair":
                title = "确认到达护理时间"
                submit_text = "确认到达护理"
            else:
                title = "确认到店时间"
                submit_text = "确认到店"

            return render_template(
                "arrival_confirm.html",
                item=it,
                mode=mode,
                title=title,
                submit_text=submit_text,
            )

        mode = (request.form.get("mode") or "").strip().lower()
        if mode not in ("repair", "store"):
            return "invalid mode", 400

        st = it.get("status")
        if mode == "repair" and st != "INBOUND":
            return "invalid status", 400
        if mode == "store" and st not in ("INBOUND", "REPARING"):
            return "invalid status", 400

        confirmed_at = parse_datetime_local_to_utc(
            request.form.get("confirmed_at", ""),
            request.form.get("tz_offset_min", "0"),
        )
        if not confirmed_at:
            return "invalid confirmed_at", 400

        if mode == "repair":
            sku = it.get("sku") or ""
            entries = [
                make_change_status(sku=sku, from_status="INBOUND", to_status="REPARING"),
                make_change_detail(sku=sku, target="start_repair_at", from_value=it.get("start_repair_at", ""), to_value=confirmed_at),
            ]
            items.update_one(
                {"_id": ObjectId(item_id)},
                {"$set": {"status": "REPARING", "start_repair_at": confirmed_at, "updated_at": now()}}
            )
            push_item_audits(items, ObjectId(item_id), entries)
            insert_audit_logs(audit_logs, entries)
        else:
            sku = it.get("sku") or ""
            entries = [
                make_change_status(sku=sku, from_status=it.get("status", ""), to_status="RECEIVED"),
                make_change_detail(sku=sku, target="received_at", from_value=it.get("received_at", ""), to_value=confirmed_at),
            ]
            items.update_one(
                {"_id": ObjectId(item_id)},
                {"$set": {"status": "RECEIVED", "received_at": confirmed_at, "updated_at": now()}}
            )
            push_item_audits(items, ObjectId(item_id), entries)
            insert_audit_logs(audit_logs, entries)

        return redirect(url_for("item_detail", item_key=item_id))

    @app.post("/items/<item_id>/action/restore_inbound")
    def action_restore_inbound(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "not found", 404
        if it.get("status") != "REPARING":
            return "invalid status", 400

        sku = it.get("sku") or ""
        entries = [
            make_change_status(sku=sku, from_status="REPARING", to_status="INBOUND"),
            make_change_detail(sku=sku, target="start_repair_at", from_value=it.get("start_repair_at", ""), to_value=""),
        ]
        items.update_one(
            {"_id": ObjectId(item_id)},
            {"$set": {"status": "INBOUND", "updated_at": now(), "received_at": None},
             "$unset": {"start_repair_at": 1}}
        )
        push_item_audits(items, ObjectId(item_id), entries)
        insert_audit_logs(audit_logs, entries)
        return "", 204

    @app.post("/items/<item_id>/action/restore_reparing")
    def action_restore_reparing(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "not found", 404
        if it.get("status") != "RECEIVED":
            return "invalid status", 400

        sku = it.get("sku") or ""
        entries = [
            make_change_status(sku=sku, from_status="RECEIVED", to_status="REPARING"),
            make_change_detail(sku=sku, target="received_at", from_value=it.get("received_at", ""), to_value=""),
        ]
        items.update_one(
            {"_id": ObjectId(item_id)},
            {"$set": {"status": "REPARING", "updated_at": now(), "received_at": None}}
        )
        push_item_audits(items, ObjectId(item_id), entries)
        insert_audit_logs(audit_logs, entries)
        return "", 204

    @app.post("/items/<item_id>/action/restore_inbound_from_received")
    def action_restore_inbound_from_received(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "not found", 404
        if it.get("status") != "RECEIVED":
            return "invalid status", 400

        sku = it.get("sku") or ""
        entries = [
            make_change_status(sku=sku, from_status="RECEIVED", to_status="INBOUND"),
            make_change_detail(sku=sku, target="received_at", from_value=it.get("received_at", ""), to_value=""),
            make_change_detail(sku=sku, target="start_repair_at", from_value=it.get("start_repair_at", ""), to_value=""),
        ]
        items.update_one(
            {"_id": ObjectId(item_id)},
            {"$set": {"status": "INBOUND", "updated_at": now(), "received_at": None},
             "$unset": {"start_repair_at": 1}}
        )
        push_item_audits(items, ObjectId(item_id), entries)
        insert_audit_logs(audit_logs, entries)
        return "", 204

    @app.post("/items/<item_id>/action/restore_received_from_sold")
    def action_restore_received_from_sold(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "not found", 404
        if it.get("status") != "SOLD":
            return "invalid status", 400

        # Return/refund workflow: restore to "never sold" state.
        # - remove sold_record entirely (including the field itself)
        # - remove profit/profit_currency
        sku = it.get("sku") or ""
        entries = [make_change_status(sku=sku, from_status="SOLD", to_status="RECEIVED")]
        sr = it.get("sold_record") or []
        if sr:
            entries.append(make_change_detail(
                sku=sku,
                target="sold_record",
                from_value=f"{len(sr)} record(s)",
                to_value="",
            ))

        for k in ["profit", "profit_currency"]:
            if it.get(k, "") not in ("", None):
                entries.append(make_change_detail(sku=sku, target=k, from_value=it.get(k, ""), to_value=""))

        items.update_one(
            {"_id": ObjectId(item_id)},
            {"$set": {"status": "RECEIVED", "updated_at": now()},
             "$unset": {"sold_record": 1, "profit": 1, "profit_currency": 1}}
        )
        push_item_audits(items, ObjectId(item_id), entries)
        insert_audit_logs(audit_logs, entries)
        return "", 204


