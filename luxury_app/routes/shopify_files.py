import logging

from flask import jsonify, render_template, request

from ..shopify_files import ShopifyFileUploadError, upload_image_to_shopify
from ..shopify_maintenance import (
    ShopifyAuthenticationError,
    ShopifyConfigurationError,
)


logger = logging.getLogger(__name__)


def _scope_message(message):
    if (
        "ACCESS_DENIED" in message
        or "access denied" in message.lower()
        or "write_files" in message
    ):
        return (
            "The Shopify app needs the write_files access scope. "
            "Add the scope, release the app configuration, and reinstall "
            "or update the app on the store."
        )
    return message


def register(app):
    @app.get("/shopify/files/bulk-upload")
    def shopify_files_bulk_upload_page():
        return render_template("shopify_files_bulk_upload.html")

    @app.post("/shopify/files/bulk-upload/one")
    def shopify_files_bulk_upload_one():
        image = request.files.get("image")
        if image is None:
            return jsonify({"ok": False, "message": "No image was provided."}), 400
        try:
            result = upload_image_to_shopify(image)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        except (ShopifyConfigurationError, ShopifyAuthenticationError) as exc:
            logger.warning("Shopify file upload authentication failed: %s", exc)
            return jsonify({"ok": False, "message": str(exc)}), 503
        except ShopifyFileUploadError as exc:
            return jsonify({"ok": False, "message": _scope_message(str(exc))}), 502
        except Exception as exc:
            logger.exception("Unexpected Shopify file upload failure.")
            message = str(exc)
            if "ACCESS_DENIED" in message or "write_files" in message:
                message = _scope_message(message)
            else:
                message = "Unexpected Shopify upload error. Please retry this image."
            return jsonify({"ok": False, "message": message}), 502
        return jsonify({"ok": True, **result})
