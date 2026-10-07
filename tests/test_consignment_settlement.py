import hashlib
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from bson import ObjectId
from flask import Flask

from consignment_receipt_template.consignment_settlement_generation import (
    DOCUMENT_PART,
    build_consignment_settlement_mapping,
    generate_consignment_settlement_docx_bytes,
    template_path,
)
from luxury_app.auth import ROLE_STAFF
from luxury_app.collection_scope import RoleScopedItems
from luxury_app.routes.items import register as register_items


class ConsignmentSettlementTests(unittest.TestCase):
    def setUp(self):
        self.item = {
            "sku": "CN12345",
            "name_in_EN": "Chanel Classic Flap Medium",
            "source_type": "CONSIGNMENT",
            "status": "SOLD",
            "sold_record": [{
                "sold_at": datetime(2026, 9, 10, tzinfo=timezone.utc),
                "sold_currency": "SGD",
                "sold_price": 10200,
                "receipt_no": "LE-2026-01892",
            }],
        }

    def test_mapping_uses_entered_customer_payout(self):
        mapping = build_consignment_settlement_mapping(
            self.item,
            payout_for_customer="8500",
            settlement_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(mapping["[SALE PRICE]"], "SGD $10,200.00")
        self.assertEqual(mapping["[CONSIGNMENT FEE]"], "SGD $1,700.00")
        self.assertEqual(mapping["[AMOUNT DUE]"], "SGD $8,500.00")
        self.assertEqual(mapping["[SETTLEMENT RECEIPT ID]"], "LE-2026-01892")
        self.assertEqual(mapping["[SOURCE SALES RECEIPT]"], "LE-2026-01892")

    def test_payout_must_be_non_negative_and_not_exceed_sale_price(self):
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            build_consignment_settlement_mapping(
                self.item, payout_for_customer="-1"
            )
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            build_consignment_settlement_mapping(
                self.item, payout_for_customer="10200.01"
            )

    def test_generation_fills_template_and_preserves_other_parts(self):
        generated = generate_consignment_settlement_docx_bytes(
            item=self.item,
            payout_for_customer="8500",
            settlement_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
        )
        with ZipFile(template_path(), "r") as source, ZipFile(
            generated, "r"
        ) as result:
            xml = result.read(DOCUMENT_PART).decode("utf-8")
            self.assertIn("SGD $10,200.00", xml)
            self.assertIn("SGD $1,700.00", xml)
            self.assertIn("SGD $8,500.00", xml)
            self.assertNotIn("[CONSIGNMENT FEE]", xml)
            self.assertEqual(source.namelist(), result.namelist())
            for name in source.namelist():
                if name == DOCUMENT_PART:
                    continue
                self.assertEqual(
                    hashlib.sha256(source.read(name)).digest(),
                    hashlib.sha256(result.read(name)).digest(),
                    name,
                )

    def test_customer_payout_version_hides_sale_price_and_commission_rows(self):
        generated = generate_consignment_settlement_docx_bytes(
            item=self.item,
            payout_for_customer="8500",
            settlement_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
            payout_only=True,
        )
        with ZipFile(generated, "r") as result:
            xml = result.read(DOCUMENT_PART).decode("utf-8")

        self.assertNotIn("Sale Price</w:t>", xml)
        self.assertNotIn("LE Commission / Adjustment", xml)
        self.assertNotIn("[SALE PRICE]", xml)
        self.assertNotIn("[CONSIGNMENT FEE]", xml)
        self.assertIn("Amount Due to Consignor", xml)
        self.assertIn("SGD $8,500.00", xml)

    def test_admin_and_staff_use_their_role_scoped_inventory(self):
        class FakeItems:
            def __init__(self, item):
                self.item = deepcopy(item)

            def find_one(self, query):
                if all(self.item.get(key) == value for key, value in query.items()):
                    return deepcopy(self.item)
                return None

        admin_id = ObjectId()
        staff_id = ObjectId()
        admin_item = dict(self.item, _id=admin_id, sku="ADMINSET")
        staff_item = dict(self.item, _id=staff_id, sku="STAFFSET")
        scoped = RoleScopedItems(FakeItems(admin_item), FakeItems(staff_item))
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret"
        register_items(app, scoped)

        with app.test_client() as client, patch(
            "luxury_app.routes.items."
            "generate_consignment_settlement_docx_bytes",
            return_value=BytesIO(b"docx"),
        ):
            admin_response = client.post(
                f"/items/{admin_id}/settlement/consignment",
                data={"payout_for_customer": "8500"},
            )
            with client.session_transaction() as session:
                session["role"] = ROLE_STAFF
            staff_response = client.post(
                f"/items/{staff_id}/settlement/consignment",
                data={"payout_for_customer": "8500"},
            )
            payout_only_response = client.post(
                f"/items/{staff_id}/settlement/consignment",
                data={
                    "payout_for_customer": "8500",
                    "payout_only": "1",
                },
            )
            staff_cannot_read_admin = client.post(
                f"/items/{admin_id}/settlement/consignment",
                data={"payout_for_customer": "8500"},
            )

        self.assertEqual(admin_response.status_code, 200)
        self.assertEqual(staff_response.status_code, 200)
        self.assertEqual(payout_only_response.status_code, 200)
        self.assertIn(
            "_customer_payout.docx",
            payout_only_response.headers["Content-Disposition"],
        )
        self.assertEqual(staff_cannot_read_admin.status_code, 404)

    def test_purchase_agreement_uses_current_address(self):
        template = (
            Path(__file__).absolute().parents[1]
            / "purchase_agreement_template"
            / "purchase_agreement.html"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "03-28, 10 Anson Road, Singapore 079903", template
        )
        self.assertNotIn(
            "18-06, 2 Marina Blvd, Singapore 018987", template
        )


if __name__ == "__main__":
    unittest.main()
