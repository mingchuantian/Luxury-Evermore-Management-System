import unittest
from unittest.mock import patch

from flask import Flask

from luxury_app.routes.shopify_files import register
from luxury_app.shopify_files import (
    FILE_CREATE_MUTATION,
    FILE_STATUS_QUERY,
    MAX_IMAGE_BYTES,
    STAGED_UPLOAD_MUTATION,
    _wait_for_file_status,
    create_shopify_content_file,
    create_staged_image_upload,
    validate_image_metadata,
)


class ShopifyFilesTests(unittest.TestCase):
    def test_staged_upload_then_creates_content_file(self):
        gql_responses = [
            {
                "stagedUploadsCreate": {
                    "stagedTargets": [{
                        "url": "https://upload.example.test",
                        "resourceUrl": "https://shopify.example.test/staged.jpg",
                        "parameters": [{"name": "key", "value": "tmp/key"}],
                    }],
                    "userErrors": [],
                }
            },
            {
                "fileCreate": {
                    "files": [{
                        "id": "gid://shopify/MediaImage/1",
                        "fileStatus": "READY",
                    }],
                    "userErrors": [],
                }
            },
        ]
        with patch(
            "luxury_app.shopify_files.shopify_gql",
            side_effect=gql_responses,
        ) as gql:
            staged = create_staged_image_upload(
                "ABC123-1.jpg", "image/jpeg", 10
            )
            result = create_shopify_content_file(
                staged["target"]["resourceUrl"], staged["metadata"]
            )

        self.assertEqual(result["file_status"], "READY")
        self.assertEqual(result["shopify_file_id"], "gid://shopify/MediaImage/1")
        self.assertEqual(gql.call_args_list[0].args[0], STAGED_UPLOAD_MUTATION)
        self.assertEqual(
            gql.call_args_list[0].args[1]["input"][0]["resource"], "IMAGE"
        )
        self.assertEqual(gql.call_args_list[1].args[0], FILE_CREATE_MUTATION)
        self.assertEqual(
            gql.call_args_list[1].args[1]["files"][0]["contentType"], "IMAGE"
        )
        self.assertEqual(
            gql.call_args_list[1].args[1]["files"][0]["originalSource"],
            "https://shopify.example.test/staged.jpg",
        )

    def test_validation_rejects_bad_type_and_oversize(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            validate_image_metadata("document.pdf", "application/pdf", 1)

        with self.assertRaisesRegex(ValueError, "20 MB"):
            validate_image_metadata(
                "large.jpg", "image/jpeg", MAX_IMAGE_BYTES + 1
            )

    def test_processing_file_is_briefly_polled_until_ready(self):
        with patch(
            "luxury_app.shopify_files.time.sleep"
        ), patch(
            "luxury_app.shopify_files.shopify_gql",
            return_value={
                "node": {
                    "id": "gid://shopify/MediaImage/1",
                    "fileStatus": "READY",
                }
            },
        ) as gql:
            result = _wait_for_file_status({
                "id": "gid://shopify/MediaImage/1",
                "fileStatus": "PROCESSING",
            })

        self.assertEqual(result["fileStatus"], "READY")
        self.assertEqual(gql.call_args.args[0], FILE_STATUS_QUERY)

    def test_page_and_direct_upload_endpoints(self):
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret"

        @app.get("/login", endpoint="login")
        def login():
            return "login"

        register(app)
        with app.test_client() as client:
            page = client.get("/shopify/files/bulk-upload")
            with patch(
                "luxury_app.routes.shopify_files.create_staged_image_upload",
                return_value={
                    "metadata": {
                        "filename": "ABC.jpg",
                        "mime_type": "image/jpeg",
                        "size": 10,
                    },
                    "target": {
                        "url": "https://upload.example.test",
                        "resourceUrl": "https://shopify.example.test/staged.jpg",
                        "parameters": [{"name": "key", "value": "tmp/key"}],
                    },
                },
            ):
                stage_response = client.post(
                    "/shopify/files/bulk-upload/stage",
                    json={
                        "filename": "ABC.jpg",
                        "mime_type": "image/jpeg",
                        "size": 10,
                    },
                )
            with patch(
                "luxury_app.routes.shopify_files.create_shopify_content_file",
                return_value={
                    "filename": "ABC.jpg",
                    "size": 10,
                    "shopify_file_id": "gid://shopify/MediaImage/1",
                    "file_status": "PROCESSING",
                },
            ):
                complete_response = client.post(
                    "/shopify/files/bulk-upload/complete",
                    json={"upload_token": stage_response.json["upload_token"]},
                )

        self.assertEqual(page.status_code, 200)
        self.assertIn(b'id="image-input"', page.data)
        self.assertIn(b"/shopify/files/bulk-upload/stage", page.data)
        self.assertEqual(stage_response.status_code, 200)
        self.assertTrue(stage_response.json["ok"])
        self.assertEqual(complete_response.status_code, 200)
        self.assertTrue(complete_response.json["ok"])


if __name__ == "__main__":
    unittest.main()
