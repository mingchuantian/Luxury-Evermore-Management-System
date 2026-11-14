"""Shared pytest safety net.

Route tests mark items as SOLD. Never let them reach the live Shopify store,
even when SHOPIFY_* credentials happen to be set in the developer's shell.
Tests that exercise the Shopify inventory call patch it explicitly.
"""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _no_live_shopify_inventory_writes():
    with patch(
        "luxury_app.routes.sales.zero_shopify_inventory_for_sku",
        return_value={"status": "not_found", "ok": True, "message": ""},
    ):
        yield
