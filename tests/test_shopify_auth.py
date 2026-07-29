import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from luxury_app import shopify_maintenance


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected HTTP request")
        return self.responses.pop(0)


class FakeAuditLogs:
    def __init__(self):
        self.entries = []

    def insert_one(self, entry):
        self.entries.append(deepcopy(entry))


class FakeShopifyItems:
    def __init__(self, docs):
        self.docs = {doc["_id"]: deepcopy(doc) for doc in docs}

    def find(self, query, projection):
        results = []
        for doc in self.docs.values():
            if query.get("shopify_sku_exist") is True:
                if doc.get("shopify_sku_exist") is not True:
                    continue
                excluded = set(query["status"].get("$nin", []))
                if doc.get("status") in excluded:
                    continue
            results.append(deepcopy(doc))
        return results

    def update_one(self, query, update):
        doc = self.docs.get(query.get("_id"))
        if doc is None:
            return SimpleNamespace(matched_count=0, modified_count=0)
        if "status" in query and doc.get("status") != query["status"]:
            return SimpleNamespace(matched_count=0, modified_count=0)

        for key, value in update.get("$set", {}).items():
            doc[key] = deepcopy(value)
        for key in update.get("$unset", {}):
            doc.pop(key, None)
        for key, value in update.get("$push", {}).items():
            doc.setdefault(key, []).append(deepcopy(value))
        return SimpleNamespace(matched_count=1, modified_count=1)


class ShopifyAuthenticationTests(unittest.TestCase):
    def test_historical_shopify_link_is_reconciled_to_on_shelf(self):
        items = FakeShopifyItems(
            [
                {
                    "_id": "linked",
                    "sku": "ABC1234",
                    "status": "RECEIVED",
                    "shopify_sku_exist": True,
                    "audit": [],
                },
                {
                    "_id": "sold",
                    "sku": "SOLD123",
                    "status": "SOLD",
                    "shopify_sku_exist": True,
                    "audit": [],
                },
            ]
        )
        audit_logs = FakeAuditLogs()

        updated = shopify_maintenance._reconcile_existing_shopify_links(
            items,
            audit_logs=audit_logs,
        )

        self.assertEqual(updated, 1)
        self.assertEqual(items.docs["linked"]["status"], "ON_SHELF")
        self.assertEqual(items.docs["sold"]["status"], "SOLD")
        self.assertEqual(
            items.docs["linked"]["audit"][0]["by"]["username"],
            "shopify-sync",
        )
        self.assertEqual(len(audit_logs.entries), 1)

    def test_live_shopify_match_does_not_overwrite_concurrent_sale(self):
        items = FakeShopifyItems(
            [
                {
                    "_id": "item",
                    "sku": "ABC1234",
                    "status": "SOLD",
                    "audit": [],
                }
            ]
        )
        stale_doc = {
            "_id": "item",
            "sku": "ABC1234",
            "status": "RECEIVED",
        }

        changed = shopify_maintenance._save_shopify_match(
            items,
            stale_doc,
            {"featured_image": "https://cdn.shopify.com/test.jpg"},
            shopify_maintenance._utc_now(),
        )

        self.assertFalse(changed)
        self.assertEqual(items.docs["item"]["status"], "SOLD")
        self.assertNotIn("shopify_details", items.docs["item"])

    def test_live_shopify_match_moves_item_to_on_shelf(self):
        doc = {
            "_id": "item",
            "sku": "ABC1234",
            "status": "INBOUND",
            "audit": [],
        }
        items = FakeShopifyItems([doc])
        audit_logs = FakeAuditLogs()

        changed = shopify_maintenance._save_shopify_match(
            items,
            doc,
            {"featured_image": "https://cdn.shopify.com/test.jpg"},
            shopify_maintenance._utc_now(),
            audit_logs=audit_logs,
        )

        self.assertTrue(changed)
        self.assertEqual(items.docs["item"]["status"], "ON_SHELF")
        self.assertTrue(items.docs["item"]["shopify_sku_exist"])
        self.assertEqual(len(items.docs["item"]["audit"]), 1)
        self.assertEqual(len(audit_logs.entries), 1)

    def test_non_json_token_error_is_reported_without_leaking_secret(self):
        secret = "super-secret-value"
        session = FakeSession(
            [
                FakeResponse(
                    400,
                    text=(
                        "<html><body>Oauth error shop_not_permitted: "
                        f"Client credentials cannot be performed. {secret}"
                        "</body></html>"
                    ),
                    headers={"Content-Type": "text/html"},
                )
            ]
        )
        provider = shopify_maintenance.ShopifyTokenProvider(
            shop="example.myshopify.com/admin",
            client_id="client-id",
            client_secret=secret,
            session=session,
        )

        with self.assertRaises(
            shopify_maintenance.ShopifyAuthenticationError
        ) as raised:
            provider.get_token()

        message = str(raised.exception)
        self.assertIn("shop_not_permitted", message)
        self.assertIn("same Shopify organization", message)
        self.assertNotIn(secret, message)
        self.assertEqual(
            session.calls[0][0],
            "https://example.myshopify.com/admin/oauth/access_token",
        )

    def test_app_not_installed_error_omits_html_and_css(self):
        session = FakeSession(
            [
                FakeResponse(
                    400,
                    text=(
                        "<html><head><title>400 - Oauth error app_not_installed"
                        "</title><style>body { font-family: sans-serif; }</style>"
                        "</head><body>Install the app.</body></html>"
                    ),
                )
            ]
        )
        provider = shopify_maintenance.ShopifyTokenProvider(
            shop="example",
            client_id="client-id",
            client_secret="client-secret",
            session=session,
        )

        with self.assertRaises(
            shopify_maintenance.ShopifyAuthenticationError
        ) as raised:
            provider.get_token()

        message = str(raised.exception)
        self.assertIn("Oauth error app_not_installed", message)
        self.assertIn("Install this Dev Dashboard app", message)
        self.assertNotIn("font-family", message)

    def test_client_credentials_token_is_cached_and_refreshed_before_expiry(self):
        clock_value = [1000.0]
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "access_token": "token-one",
                        "scope": "read_products",
                        "expires_in": 86399,
                    },
                ),
                FakeResponse(
                    200,
                    {
                        "access_token": "token-two",
                        "scope": "read_products",
                        "expires_in": 86399,
                    },
                ),
            ]
        )
        provider = shopify_maintenance.ShopifyTokenProvider(
            shop="example.myshopify.com",
            client_id="client-id",
            client_secret="client-secret",
            session=session,
            clock=lambda: clock_value[0],
        )

        self.assertEqual(provider.get_token(), "token-one")
        self.assertEqual(provider.get_token(), "token-one")
        self.assertEqual(len(session.calls), 1)
        token_call = session.calls[0]
        self.assertEqual(
            token_call[0],
            "https://example.myshopify.com/admin/oauth/access_token",
        )
        self.assertEqual(token_call[1]["data"]["grant_type"], "client_credentials")

        clock_value[0] += 86100
        self.assertEqual(provider.get_token(), "token-two")
        self.assertEqual(len(session.calls), 2)

    def test_write_products_implicitly_satisfies_product_read_access(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "access_token": "token-with-write-products",
                        "scope": "write_inventory write_products,write_orders",
                        "expires_in": 86399,
                    },
                )
            ]
        )
        provider = shopify_maintenance.ShopifyTokenProvider(
            shop="luxuryevermore.com",
            client_id="client-id",
            client_secret="client-secret",
            session=session,
        )

        self.assertEqual(provider.get_token(), "token-with-write-products")

    def test_graphql_401_forces_one_token_refresh(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "access_token": "expired-token",
                        "scope": "read_products",
                        "expires_in": 86399,
                    },
                ),
                FakeResponse(401, {"errors": "Unauthorized"}),
                FakeResponse(
                    200,
                    {
                        "access_token": "fresh-token",
                        "scope": "read_products",
                        "expires_in": 86399,
                    },
                ),
                FakeResponse(
                    200,
                    {"data": {"shop": {"name": "Example"}}},
                    headers={"X-Shopify-API-Version": shopify_maintenance.API_VERSION},
                ),
            ]
        )
        provider = shopify_maintenance.ShopifyTokenProvider(
            shop="example.myshopify.com",
            client_id="client-id",
            client_secret="client-secret",
            session=session,
        )

        result = shopify_maintenance.shopify_gql(
            "query { shop { name } }",
            {},
            token_provider=provider,
        )

        self.assertEqual(result["shop"]["name"], "Example")
        graphql_calls = [
            call for call in session.calls if "/graphql.json" in call[0]
        ]
        self.assertEqual(len(graphql_calls), 2)
        self.assertEqual(
            graphql_calls[0][1]["headers"]["X-Shopify-Access-Token"],
            "expired-token",
        )
        self.assertEqual(
            graphql_calls[1][1]["headers"]["X-Shopify-Access-Token"],
            "fresh-token",
        )

    def test_sku_match_is_exact_and_keeps_featured_image(self):
        gql_result = {
            "productVariants": {
                "edges": [
                    {
                        "node": {
                            "id": "gid://shopify/ProductVariant/1",
                            "sku": "ABC1234-OTHER",
                            "product": {},
                        }
                    },
                    {
                        "node": {
                            "id": "gid://shopify/ProductVariant/2",
                            "sku": "ABC1234",
                            "price": "100.00",
                            "compareAtPrice": None,
                            "inventoryQuantity": 1,
                            "product": {
                                "id": "gid://shopify/Product/2",
                                "title": "Test product",
                                "handle": "test-product",
                                "featuredImage": {
                                    "url": "https://cdn.shopify.com/test.jpg"
                                },
                            },
                        }
                    },
                ]
            }
        }
        with patch.object(
            shopify_maintenance,
            "shopify_gql",
            return_value=gql_result,
        ) as gql:
            result = shopify_maintenance.fetch_shopify_details_by_sku("ABC1234")

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["details"]["featured_image"],
            "https://cdn.shopify.com/test.jpg",
        )
        self.assertEqual(gql.call_args.args[1]["q"], 'sku:"ABC1234"')


if __name__ == "__main__":
    unittest.main()
