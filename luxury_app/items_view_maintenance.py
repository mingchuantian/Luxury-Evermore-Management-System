"""Daily, failure-isolated maintenance for the staff-facing items_view collection."""

import csv
import logging
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests
from pymongo import ReplaceOne, ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError


logger = logging.getLogger(__name__)

JOB_ID = "items_view_daily_maintenance"
DEFAULT_INTERVAL_HOURS = 24.0
DEFAULT_CUTOFF = "2026-01-01"
DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
HAN_RE = re.compile("[\u3400-\u4dbf\u4e00-\u9fff]")

_scheduler_threads: Dict[str, threading.Thread] = {}
_scheduler_guard = threading.Lock()


class ItemsViewMaintenanceError(RuntimeError):
    pass


class TranslationQualityError(ItemsViewMaintenanceError):
    """The API responded, but the translated name failed local validation."""


PERMANENT_QUOTA_CODES = {
    "credit_balance_exhausted",
    "insufficient_quota",
    "billing_hard_limit_reached",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_enabled() -> bool:
    return (os.getenv("ITEMS_VIEW_MAINTENANCE_ENABLED") or "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _interval_hours() -> float:
    try:
        value = float(
            os.getenv("ITEMS_VIEW_MAINTENANCE_INTERVAL_HOURS")
            or DEFAULT_INTERVAL_HOURS
        )
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_HOURS
    return max(1.0, value)


def _cutoff_datetime() -> datetime:
    raw = (os.getenv("ITEMS_VIEW_MAINTENANCE_CUTOFF") or DEFAULT_CUTOFF).strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ItemsViewMaintenanceError(
            "ITEMS_VIEW_MAINTENANCE_CUTOFF must use YYYY-MM-DD."
        ) from exc


def _safe_error(exc: Exception) -> str:
    message = str(exc) or exc.__class__.__name__
    message = re.sub(
        r"mongodb(?:\+srv)?://[^@\s]+@",
        "mongodb://***@",
        message,
        flags=re.IGNORECASE,
    )
    message = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-***", message)
    return message[:1000]


def _extract_response_text(payload: Dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    chunks: List[str] = []
    for output_item in payload.get("output") or []:
        if not isinstance(output_item, dict):
            continue
        for content in output_item.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") in ("output_text", "text"):
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text.strip())
    return "\n".join(chunks).strip()


def _openai_error(response) -> ItemsViewMaintenanceError:
    detail = ""
    code = ""
    try:
        payload = response.json()
        error = payload.get("error") or {}
        code = error.get("code") or ""
        detail = error.get("message") or error.get("code") or ""
    except Exception:
        detail = (response.text or "").strip()
    detail = re.sub(r"\s+", " ", detail)[:500]
    suffix = f": {detail}" if detail else ""
    result = ItemsViewMaintenanceError(
        f"OpenAI API request failed (HTTP {response.status_code}){suffix}"
    )
    result.status_code = response.status_code
    result.error_code = code
    return result


def _retry_delay(response, attempt: int) -> Optional[float]:
    retry_after = (response.headers.get("Retry-After") or "").strip()
    if retry_after:
        try:
            seconds = float(retry_after)
            return seconds if 0 <= seconds <= 30 else None
        except ValueError:
            pass
    return min(2 ** attempt + random.random(), 10.0)


def translate_product_name(original_name: str) -> str:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise ItemsViewMaintenanceError(
            "OPENAI_API_KEY is not set; Chinese product names remain pending."
        )

    model = (
        os.getenv("ITEMS_VIEW_MAINTENANCE_OPENAI_MODEL")
        or DEFAULT_OPENAI_MODEL
    ).strip()
    instructions = (
        "你是一个专业的二手奢侈品商家。把用户提供的中文商品名称翻译为英文。"
        "注意行业称呼差异：迪奥五格对应 Lady Medium，三格对应 Lady Mini，"
        "七格对应 Lady Large，四格对应 Lady Small；CF 对应 Classic Flap。"
        "只输出翻译后的英文商品名称，不要解释。"
    )
    last_quality_error = ""
    for attempt in range(3):
        retry_instructions = instructions
        if attempt:
            retry_instructions += (
                " 上一次输出未通过校验。重新翻译，并确保输出非空且完全不包含中文字符。"
            )
        try:
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "instructions": retry_instructions,
                    "input": original_name,
                    "max_output_tokens": 200,
                    "store": False,
                },
                timeout=(10, 60),
            )
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(min(2 ** attempt + random.random(), 10.0))
                continue
            raise ItemsViewMaintenanceError(
                f"OpenAI API connection failed after 3 attempts: {_safe_error(exc)}"
            ) from exc

        if response.status_code >= 400:
            error = _openai_error(response)
            code = getattr(error, "error_code", "")
            retryable = (
                response.status_code >= 500
                or (response.status_code == 429 and code not in PERMANENT_QUOTA_CODES)
            )
            if retryable and attempt < 2:
                delay = _retry_delay(response, attempt)
                if delay is not None:
                    time.sleep(delay)
                    continue
            raise error

        try:
            translated = _extract_response_text(response.json())
        except ValueError as exc:
            if attempt < 2:
                continue
            raise ItemsViewMaintenanceError(
                "OpenAI API returned invalid JSON after 3 attempts."
            ) from exc
        if translated and not HAN_RE.search(translated):
            return translated
        last_quality_error = (
            "OpenAI API returned an empty result or a result that still contains Chinese."
        )

    raise TranslationQualityError(f"{last_quality_error} Retried 3 times.")


def _load_seller_names() -> List[str]:
    csv_path = (
        Path(__file__).resolve().parents[1]
        / "name_database"
        / "name_database.csv"
    )
    with csv_path.open(encoding="utf-8-sig", newline="") as file:
        names = list(dict.fromkeys(
            name
            for row in csv.DictReader(file)
            if len(name := (row.get("name") or "").strip()) >= 3
        ))
    if not names:
        raise ItemsViewMaintenanceError(
            "name_database/name_database.csv has no valid names."
        )
    return names


def _update_sold_prices(items_view) -> int:
    """Set the latest sale to cost + profit once for each maintained SOLD item."""
    result = items_view.update_many(
        {
            "status": "SOLD",
            "changed_sold_price": {"$ne": True},
            "sold_record.0": {"$exists": True},
            "$expr": {"$and": [
                {"$isNumber": "$cost"},
                {"$isNumber": "$profit"},
            ]},
        },
        [{"$set": {
            "sold_record": {"$concatArrays": [
                {"$slice": [
                    "$sold_record",
                    {"$subtract": [{"$size": "$sold_record"}, 1]},
                ]},
                [{"$mergeObjects": [
                    {"$arrayElemAt": ["$sold_record", -1]},
                    {
                        "sold_price": {"$add": ["$cost", "$profit"]},
                        "sold_currency": "SGD",
                    },
                ]}],
            ]},
            "changed_sold_price": True,
        }}],
    )
    return result.modified_count


def maintain_items_view(
    items,
    items_view,
    *,
    translator: Callable[[str], str] = translate_product_name,
) -> Dict[str, Any]:
    """Run the notebook's idempotent maintenance steps and return a summary."""
    cutoff = _cutoff_datetime()
    query = {
        "status": {"$in": ["RECEIVED", "SOLD", "ON_SHELF"]},
        "received_at": {"$gte": cutoff},
        "updated_at": {"$gte": cutoff},
    }
    source_docs = list(items.find(query))
    source_ids = [doc["_id"] for doc in source_docs]
    view_status = {
        doc["_id"]: doc.get("status")
        for doc in items_view.find(
            {"_id": {"$in": source_ids}}, {"status": 1}
        )
    } if source_ids else {}

    new_docs = [doc for doc in source_docs if doc["_id"] not in view_status]
    changed_docs = [
        doc for doc in source_docs
        if doc["_id"] in view_status
        and view_status[doc["_id"]] != doc.get("status")
    ]
    refreshed_docs = new_docs + changed_docs
    if refreshed_docs:
        items_view.bulk_write(
            [
                ReplaceOne({"_id": doc["_id"]}, doc, upsert=True)
                for doc in refreshed_docs
            ],
            ordered=False,
        )

    summary: Dict[str, Any] = {
        "source_matched": len(source_docs),
        "new_imported": len(new_docs),
        "status_refreshed": len(changed_docs),
        "unchanged": len(source_docs) - len(refreshed_docs),
        "name_translated": 0,
        "name_failed": 0,
        "translation_fatal": False,
        "translation_error": "",
    }

    name_pending = items_view.find(
        {"changed_name": {"$ne": True}, "name": HAN_RE},
        {"name": 1},
    )
    for doc in name_pending:
        original_name = doc.get("name") or ""
        try:
            translated = translator(original_name)
            result = items_view.update_one(
                {
                    "_id": doc["_id"],
                    "name": original_name,
                    "changed_name": {"$ne": True},
                },
                {"$set": {"name": translated, "changed_name": True}},
            )
            summary["name_translated"] += result.modified_count
        except TranslationQualityError as exc:
            # A single difficult name should not fail the whole maintenance run.
            # It remains unmarked and will be retried on the next run.
            summary["name_failed"] += 1
            if not summary["translation_error"]:
                summary["translation_error"] = _safe_error(exc)
            if summary["name_failed"] >= 5:
                break
        except Exception as exc:
            # Authentication, billing and connection failures are systemic;
            # stop API calls immediately so we do not hammer a broken service.
            summary["name_failed"] += 1
            summary["translation_fatal"] = True
            summary["translation_error"] = _safe_error(exc)
            break

    consign = items_view.update_many(
        {"changed_to_consign": {"$ne": True}},
        {"$set": {
            "is_buy_in": False,
            "is_consignment": True,
            "source_type": "CONSIGNMENT",
            "changed_to_consign": True,
        }},
    )
    cost = items_view.update_many(
        {"changed_cost": {"$ne": True}, "$expr": {"$isNumber": "$cost"}},
        [{"$set": {
            "cost": {"$toLong": {"$round": [{"$divide": ["$cost", 5.3]}, 0]}},
            "cost_currency": "SGD",
            "changed_cost": True,
        }}],
    )

    profit_docs = list(items_view.find(
        {
            "status": "SOLD",
            "changed_profit": {"$ne": True},
            "$expr": {"$and": [
                {"$isNumber": "$cost"},
                {"$isNumber": "$profit"},
            ]},
        },
        {"cost": 1},
    ))
    profit_ops = []
    for doc in profit_docs:
        rate = random.randint(8, 15) / 100
        profit_value = round(doc["cost"] * rate * random.choice((-1, 1)))
        profit_ops.append(UpdateOne(
            {
                "_id": doc["_id"],
                "status": "SOLD",
                "changed_profit": {"$ne": True},
            },
            {"$set": {
                "profit": profit_value,
                "profit_currency": "SGD",
                "changed_profit": True,
            }},
        ))
    profit_result = (
        items_view.bulk_write(profit_ops, ordered=False)
        if profit_ops else None
    )
    sold_prices_changed = _update_sold_prices(items_view)
    note = items_view.update_many(
        {"changed_note": {"$ne": True}},
        {"$set": {"note": "", "changed_note": True}},
    )

    names = _load_seller_names()
    seller_docs = list(items_view.find(
        {"changed_seller_name": {"$ne": True}},
        {"seller_name": 1},
    ))
    seller_ops = []
    for doc in seller_docs:
        original = doc.get("seller_name")
        new_name = random.choice(names)
        while len(names) > 1 and new_name == original:
            new_name = random.choice(names)
        seller_ops.append(UpdateOne(
            {
                "_id": doc["_id"],
                "seller_name": original,
                "changed_seller_name": {"$ne": True},
            },
            {"$set": {
                "seller_name": new_name,
                "changed_seller_name": True,
            }},
        ))
    seller_result = (
        items_view.bulk_write(seller_ops, ordered=False)
        if seller_ops else None
    )

    summary.update({
        "converted_to_consignment": consign.modified_count,
        "cost_converted": cost.modified_count,
        "profit_recalculated": profit_result.modified_count if profit_result else 0,
        "sold_prices_changed": sold_prices_changed,
        "notes_cleared": note.modified_count,
        "seller_names_changed": seller_result.modified_count if seller_result else 0,
    })
    return summary


def _summary_message(summary: Dict[str, Any]) -> str:
    message = (
        f"符合条件 {summary.get('source_matched', 0)}；"
        f"新增 {summary.get('new_imported', 0)}；"
        f"状态变化刷新 {summary.get('status_refreshed', 0)}；"
        f"商品名翻译 {summary.get('name_translated', 0)}；"
        f"转寄售 {summary.get('converted_to_consignment', 0)}；"
        f"成本转换 {summary.get('cost_converted', 0)}；"
        f"利润重算 {summary.get('profit_recalculated', 0)}；"
        f"售价重算 {summary.get('sold_prices_changed', 0)}；"
        f"清空备注 {summary.get('notes_cleared', 0)}；"
        f"卖家名修改 {summary.get('seller_names_changed', 0)}"
    )
    if summary.get("translation_error"):
        message += (
            f"；商品名待下次重试 {summary.get('name_failed', 0)}："
            f"{summary['translation_error']}"
        )
    return message


def _acquire_job_lease(job_locks, *, force: bool) -> Optional[str]:
    now_at = _utc_now()
    owner = uuid.uuid4().hex
    conditions = [{"$or": [
        {"locked_until": {"$exists": False}},
        {"locked_until": {"$lte": now_at}},
    ]}]
    if not force:
        conditions.append({"$or": [
            {"next_run_at": {"$exists": False}},
            {"next_run_at": {"$lte": now_at}},
        ]})
    try:
        acquired = job_locks.find_one_and_update(
            {"_id": JOB_ID, "$and": conditions},
            {
                "$set": {
                    "locked_until": now_at + timedelta(hours=6),
                    "lock_owner": owner,
                    "last_started_at": now_at,
                },
                "$setOnInsert": {"created_at": now_at},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return None
    return owner if acquired and acquired.get("lock_owner") == owner else None


def _release_job_lease(job_locks, owner: str, status: str) -> None:
    finished_at = _utc_now()
    job_locks.update_one(
        {"_id": JOB_ID, "lock_owner": owner},
        {"$set": {
            "locked_until": finished_at,
            "next_run_at": finished_at + timedelta(hours=_interval_hours()),
            "last_finished_at": finished_at,
            "last_status": status,
        }, "$unset": {"lock_owner": ""}},
    )


def run_items_view_maintenance(
    items,
    items_view,
    maintenance_logs,
    job_locks,
    *,
    triggered_by: str = "scheduled",
    force: bool = False,
    acquired_owner: Optional[str] = None,
) -> Dict[str, Any]:
    """Run once. Every failure is captured and returned instead of escaping."""
    owner = acquired_owner or _acquire_job_lease(job_locks, force=force)
    if not owner:
        return {"status": "skipped", "message": "维护尚未到时间或已有任务运行中。"}

    started_at = _utc_now()
    log_id = None
    try:
        inserted = maintenance_logs.insert_one({
            "job": JOB_ID,
            "status": "running",
            "triggered_by": triggered_by,
            "started_at": started_at,
            "message": "维护运行中",
        })
        log_id = inserted.inserted_id
    except Exception:
        logger.exception("Failed to create items_view maintenance log.")

    status = "failed"
    summary: Dict[str, Any] = {}
    message = ""
    try:
        summary = maintain_items_view(items, items_view)
        message = _summary_message(summary)
        if summary.get("translation_fatal"):
            status = "failed"
        elif summary.get("name_failed"):
            status = "warning"
        else:
            status = "success"
        if status == "success":
            logger.info("items_view maintenance completed: %s", message)
        elif status == "warning":
            logger.warning("items_view maintenance completed with warnings: %s", message)
        else:
            logger.error("items_view maintenance completed with errors: %s", message)
    except Exception as exc:
        message = _safe_error(exc)
        logger.error("items_view maintenance failed: %s", message, exc_info=True)
    finally:
        finished_at = _utc_now()
        if log_id is not None:
            try:
                maintenance_logs.update_one(
                    {"_id": log_id},
                    {"$set": {
                        "status": status,
                        "finished_at": finished_at,
                        "message": message or "未知错误",
                        "summary": summary,
                    }},
                )
            except Exception:
                logger.exception("Failed to finish items_view maintenance log.")
        try:
            _release_job_lease(job_locks, owner, status)
        except Exception:
            logger.exception("Failed to release items_view maintenance lease.")

    return {"status": status, "message": message, "summary": summary}


def trigger_items_view_maintenance(
    items,
    items_view,
    maintenance_logs,
    job_locks,
    *,
    triggered_by: str,
) -> bool:
    """Acquire first, then run in a daemon thread so the HTTP request stays fast."""
    try:
        owner = _acquire_job_lease(job_locks, force=True)
    except Exception:
        logger.exception("Failed to acquire manual items_view maintenance lease.")
        return False
    if not owner:
        return False
    try:
        thread = threading.Thread(
            target=run_items_view_maintenance,
            kwargs={
                "items": items,
                "items_view": items_view,
                "maintenance_logs": maintenance_logs,
                "job_locks": job_locks,
                "triggered_by": triggered_by,
                "force": True,
                "acquired_owner": owner,
            },
            name="ItemsViewMaintenanceManual",
            daemon=True,
        )
        thread.start()
        return True
    except Exception:
        logger.exception("Failed to start manual items_view maintenance thread.")
        _release_job_lease(job_locks, owner, "failed")
        return False


def start_items_view_maintenance_scheduler(
    items,
    items_view,
    maintenance_logs,
    job_locks,
):
    """Start one lightweight local scheduler; MongoDB prevents multi-worker duplication."""
    if not _is_enabled():
        logger.info("items_view maintenance scheduler disabled by configuration.")
        return None

    scheduler_key = f"{items_view.database.name}:{items_view.name}"
    with _scheduler_guard:
        current = _scheduler_threads.get(scheduler_key)
        if current and current.is_alive():
            return current

        def scheduler_loop():
            while True:
                try:
                    run_items_view_maintenance(
                        items,
                        items_view,
                        maintenance_logs,
                        job_locks,
                    )
                except Exception:
                    # Defensive outer boundary: a scheduler bug must never stop Flask.
                    logger.exception("Unexpected items_view scheduler error.")
                time.sleep(60)

        thread = threading.Thread(
            target=scheduler_loop,
            name=f"ItemsViewMaintenance-{scheduler_key}",
            daemon=True,
        )
        _scheduler_threads[scheduler_key] = thread
        thread.start()
        logger.info(
            "items_view maintenance scheduler started (interval: %s hours).",
            _interval_hours(),
        )
        return thread
