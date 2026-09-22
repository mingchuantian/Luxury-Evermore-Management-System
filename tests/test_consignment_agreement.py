import hashlib
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from zipfile import ZipFile

from bson import ObjectId
from flask import Flask

from consignment_receipt_template.consignment_agreement_generation import (
    DOCUMENT_PART,
    generate_consignment_agreement_docx_bytes,
    template_path,
)
from luxury_app.routes.items import register as register_items


class FakeItems:
    def __init__(self, item):
        self.item = deepcopy(item)

    def find_one(self, query):
        if query.get("_id") == self.item["_id"]:
            return deepcopy(self.item)
        return None


class ConsignmentAgreementTests(unittest.TestCase):
    def test_generation_fills_template_and_preserves_other_package_parts(self):
        item = {
            "sku": "CN12345",
            "purchase_at": datetime(2026, 8, 22, 2, 30, tzinfo=timezone.utc),
            "seller_name": "Jane & John",
            "name_in_EN": "Dior Medium D-Joy Bag",
            "cost_currency": "SGD",
            "cost": 12500,
            "additional_notes_for_agreements": "Includes strap <and> dust bag",
        }

        generated = generate_consignment_agreement_docx_bytes(item=item)
        with ZipFile(template_path(), "r") as source, ZipFile(generated, "r") as result:
            self.assertEqual(source.namelist(), result.namelist())
            xml = result.read(DOCUMENT_PART).decode("utf-8")
            self.assertIn("CN12345", xml)
            self.assertIn("22 Aug 2026", xml)
            self.assertIn("Jane &amp; John", xml)
            self.assertIn("Dior Medium D-Joy Bag", xml)
            self.assertIn("SGD $12,500", xml)
            self.assertIn("Includes strap &lt;and&gt; dust bag", xml)
            self.assertNotIn("[AGREEMENT NUMBER]", xml)
            self.assertNotIn("[TARGET PAYOUT]", xml)

            for name in source.namelist():
                if name == DOCUMENT_PART:
                    continue
                self.assertEqual(
                    hashlib.sha256(source.read(name)).digest(),
                    hashlib.sha256(result.read(name)).digest(),
                    name,
                )

    def test_non_sgd_currency_replaces_the_template_currency_too(self):
        generated = generate_consignment_agreement_docx_bytes(
            item={"cost_currency": "RMB", "cost": 8800}
        )
        with ZipFile(generated, "r") as result:
            xml = result.read(DOCUMENT_PART).decode("utf-8")
        self.assertIn("RMB 8,800", xml)
        self.assertNotIn("SGD $[TARGET PAYOUT]", xml)

    def test_existing_button_endpoint_downloads_the_new_word_agreement(self):
        item_id = ObjectId()
        items = FakeItems(
            {
                "_id": item_id,
                "sku": "CN12345",
                "source_type": "CONSIGNMENT",
                "cost_currency": "SGD",
                "cost": 12500,
            }
        )
        app = Flask(__name__)
        app.secret_key = "test-secret"
        register_items(app, items)

        with app.test_client() as client:
            response = client.get(f"/items/{item_id}/agreement/consignment")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.mimetype,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertIn(
            "CN12345_consignment_agreement.docx",
            response.headers.get("Content-Disposition", ""),
        )


if __name__ == "__main__":
    unittest.main()
