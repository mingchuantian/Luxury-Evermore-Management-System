import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from luxury_app.routes.dashboard import (
    _daily_totals_by_day,
    _inventory_value_totals,
)


class DashboardAggregationTests(unittest.TestCase):
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
