import logging

from flask import jsonify, render_template, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..shopify_files import (
    ShopifyFileUploadError,
    create_shopify_content_file,
    create_staged_image_upload,
)
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
    signer = URLSafeTimedSerializer(
        app.secret_key, salt="shopify-staged-image-upload"
    )

    @app.get("/shopify/files/bulk-upload")
    def shopify_files_bulk_upload_page():
        return render_template("shopify_files_bulk_upload.html")

    @app.post("/shopify/files/bulk-upload/stage")
    def shopify_files_bulk_upload_stage():
        payload = request.get_json(silent=True) or {}
        try:
            staged = create_staged_image_upload(
                payload.get("filename"),
                payload.get("mime_type"),
                payload.get("size"),
            )
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
        token = signer.dumps({
            "resource_url": staged["target"]["resourceUrl"],
            "metadata": staged["metadata"],
        })
        return jsonify({
            "ok": True,
            "upload_token": token,
            "target": {
                "url": staged["target"]["url"],
                "parameters": staged["target"].get("parameters") or [],
            },
        })

    @app.post("/shopify/files/bulk-upload/complete")
    def shopify_files_bulk_upload_complete():
        payload = request.get_json(silent=True) or {}
        try:
            signed = signer.loads(
                payload.get("upload_token") or "", max_age=30 * 60
            )
        except (BadSignature, SignatureExpired):
            return jsonify({
                "ok": False,
                "message": "The temporary Shopify upload token expired. Retry this image.",
            }), 400
        try:
            result = create_shopify_content_file(
                signed.get("resource_url"), signed.get("metadata") or {}
            )
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        except (ShopifyConfigurationError, ShopifyAuthenticationError) as exc:
            logger.warning("Shopify file creation authentication failed: %s", exc)
            return jsonify({"ok": False, "message": str(exc)}), 503
        except ShopifyFileUploadError as exc:
            return jsonify({"ok": False, "message": _scope_message(str(exc))}), 502
        except Exception as exc:
            logger.exception("Unexpected Shopify file creation failure.")
            message = _scope_message(str(exc))
            if message == str(exc):
                message = "Unexpected Shopify file creation error. Retry this image."
            return jsonify({"ok": False, "message": message}), 502
        return jsonify({"ok": True, **result})
