import unittest
import zipfile
from datetime import datetime, timezone
from unittest.mock import MagicMock

from bson import ObjectId, json_util

from luxury_app.routes.dashboard import (
    _annual_totals_from_months,
    _build_database_backup,
    _daily_totals_by_day,
    _inventory_value_totals,
)


class DashboardAggregationTests(unittest.TestCase):
    def test_annual_totals_sum_monthly_values_for_four_years(self):
        annual = _annual_totals_from_months(
            {
                "2026-01": 100, "2026-02": 200,
                "2025-12": 300, "2023-01": 400,
            },
            {
                "2026-01": 1000, "2026-02": 2000,
                "2024-06": 3000, "2023-01": 4000,
            },
            2026,
        )

        self.assertEqual(annual, [
            {"year": 2026, "sales_sgd": 3000, "profit_rmb": 300},
            {"year": 2025, "sales_sgd": 0, "profit_rmb": 300},
            {"year": 2024, "sales_sgd": 3000, "profit_rmb": 0},
            {"year": 2023, "sales_sgd": 4000, "profit_rmb": 400},
        ])

    def test_database_backup_preserves_bson_values_as_extended_json(self):
        item_id = ObjectId()
        created_at = datetime(2026, 8, 22, tzinfo=timezone.utc)
        collection = MagicMock()
        collection.find.return_value = [{
            "_id": item_id,
            "sku": "BACKUP1",
            "created_at": created_at,
        }]
        database = MagicMock()
        database.name = "inventory"
        database.list_collection_names.return_value = ["items"]
        database.__getitem__.return_value = collection

        backup = _build_database_backup(database)

        with zipfile.ZipFile(backup) as archive:
            self.assertEqual(
                sorted(archive.namelist()),
                ["collections/items.json", "manifest.json"],
            )
            restored = json_util.loads(
                archive.read("collections/items.json").decode("utf-8")
            )
        self.assertEqual(restored[0]["_id"], item_id)
        # PyMongo's default decoder returns UTC datetimes without tzinfo, just
        # like ordinary MongoClient reads when tz_aware is not enabled.
        self.assertEqual(restored[0]["created_at"], created_at.replace(tzinfo=None))

    def test_inventory_values_use_requested_buy_in_status_groups(self):
        items = MagicMock()
        items.aggregate.return_value = [{
            "unsold": [{"_id": None, "total": 30000}],
            "caring": [{"_id": None, "total": 12000}],
            "received_unsold": [{"_id": None, "total": 18000}],
        }]

        totals = _inventory_value_totals(items)

        self.assertEqual(totals, {
            "unsold": 30000,
            "caring": 12000,
            "received_unsold": 18000,
        })
        pipeline = items.aggregate.call_args.args[0]
        facets = pipeline[0]["$facet"]
        self.assertEqual(
            facets["unsold"][0]["$match"],
            {"source_type": "BUY_IN", "status": {"$ne": "SOLD"}},
        )
        self.assertEqual(
            facets["caring"][0]["$match"]["status"]["$in"],
            ["INBOUND", "REPARING"],
        )
        self.assertEqual(
            facets["received_unsold"][0]["$match"]["status"]["$in"],
            ["RECEIVED", "ON_SHELF"],
        )

    def test_daily_totals_are_mapped_by_business_date(self):
        items = MagicMock()
        items.aggregate.return_value = [{
            "purchase": [{"_id": "2026-08-22", "total": 10000}],
            "sales": [{"_id": "2026-08-22", "total": 2500}],
            "profit": [{"_id": "2026-08-22", "total": 800}],
        }]
        start_at = datetime(2026, 8, 21, 16, tzinfo=timezone.utc)
        end_at = datetime(2026, 8, 22, 16, tzinfo=timezone.utc)

        totals = _daily_totals_by_day(items, start_at, end_at)

        self.assertEqual(totals["2026-08-22"], {
            "purchase_cost_rmb": 10000,
            "sales_total_sgd": 2500,
            "profit_total_rmb": 800,
        })
        pipeline = items.aggregate.call_args.args[0]
        facets = pipeline[0]["$facet"]
        purchase_group = facets["purchase"][-1]["$group"]
        self.assertEqual(
            purchase_group["_id"]["$dateToString"]["timezone"],
            "+08:00",
        )


if __name__ == "__main__":
    unittest.main()
