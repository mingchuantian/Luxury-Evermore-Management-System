import unittest
from io import BytesIO
from unittest.mock import Mock, patch

from flask import Flask
from werkzeug.datastructures import FileStorage

from luxury_app.routes.shopify_files import register
from luxury_app.shopify_files import (
    FILE_CREATE_MUTATION,
    FILE_STATUS_QUERY,
    MAX_IMAGE_BYTES,
    STAGED_UPLOAD_MUTATION,
    _wait_for_file_status,
    upload_image_to_shopify,
    validate_image_upload,
)


class ShopifyFilesTests(unittest.TestCase):
    def _image(self, name="ABC123-1.jpg", content=b"image-data"):
        return FileStorage(
            stream=BytesIO(content),
            filename=name,
            content_type="image/jpeg",
        )

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
        storage_response = Mock(status_code=204)
        with patch(
            "luxury_app.shopify_files.shopify_gql",
            side_effect=gql_responses,
        ) as gql, patch(
            "luxury_app.shopify_files.requests.post",
            return_value=storage_response,
        ) as storage_post:
            result = upload_image_to_shopify(self._image())

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
        self.assertEqual(storage_post.call_count, 1)

    def test_validation_rejects_bad_type_and_oversize(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            validate_image_upload(FileStorage(
                stream=BytesIO(b"x"),
                filename="document.pdf",
                content_type="application/pdf",
            ))

        oversized = Mock()
        oversized.filename = "large.jpg"
        oversized.mimetype = "image/jpeg"
        oversized.stream = Mock()
        oversized.stream.tell.side_effect = [0, MAX_IMAGE_BYTES + 1]
        with self.assertRaisesRegex(ValueError, "20 MB"):
            validate_image_upload(oversized)

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

    def test_page_and_single_image_endpoint(self):
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret"

        @app.get("/login", endpoint="login")
        def login():
            return "login"

        register(app)
        with app.test_client() as client:
            page = client.get("/shopify/files/bulk-upload")
            with patch(
                "luxury_app.routes.shopify_files.upload_image_to_shopify",
                return_value={
                    "filename": "ABC.jpg",
                    "size": 10,
                    "shopify_file_id": "gid://shopify/MediaImage/1",
                    "file_status": "PROCESSING",
                },
            ):
                response = client.post(
                    "/shopify/files/bulk-upload/one",
                    data={"image": (BytesIO(b"image"), "ABC.jpg")},
                    content_type="multipart/form-data",
                )

        self.assertEqual(page.status_code, 200)
        self.assertIn(b'id="image-input"', page.data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])


if __name__ == "__main__":
    unittest.main()
