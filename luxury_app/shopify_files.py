"""Upload images to Shopify Content > Files through staged uploads."""

import os
import re
import threading
import time
from typing import Any, Dict

import requests

from .shopify_maintenance import shopify_gql


MAX_IMAGE_BYTES = 20_000_000
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
}
MIME_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".heic": "image/heic",
}

# A single browser also limits itself to three. This server-side guard protects
# the store if several tabs start uploading at the same time in one worker.
SHOPIFY_FILE_UPLOAD_SLOTS = threading.BoundedSemaphore(3)


STAGED_UPLOAD_MUTATION = """
mutation CreateImageStagedUpload($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets {
      url
      resourceUrl
      parameters { name value }
    }
    userErrors { field message }
  }
}
"""


FILE_CREATE_MUTATION = """
mutation CreateShopifyFile($files: [FileCreateInput!]!) {
  fileCreate(files: $files) {
    files { id fileStatus alt createdAt }
    userErrors { field message code }
  }
}
"""


FILE_STATUS_QUERY = """
query ShopifyFileStatus($id: ID!) {
  node(id: $id) {
    ... on MediaImage { id fileStatus }
  }
}
"""


class ShopifyFileUploadError(RuntimeError):
    pass


def _clean_filename(value: str) -> str:
    filename = os.path.basename((value or "").replace("\\", "/")).strip()
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)
    if not filename or filename.startswith("."):
        raise ValueError("Invalid image filename.")
    if len(filename) > 200:
        raise ValueError("Image filename is too long (maximum 200 characters).")
    return filename


def validate_image_upload(file_storage) -> Dict[str, Any]:
    filename = _clean_filename(getattr(file_storage, "filename", ""))
    extension = os.path.splitext(filename)[1].lower()
    supplied_mime = (getattr(file_storage, "mimetype", "") or "").lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError(
            "Unsupported image type. Use JPEG, PNG, WEBP, HEIC, or GIF."
        )
    # Some mobile browsers send HEIC as application/octet-stream. Shopify
    # validates the actual file, so use the known MIME for an allowed suffix.
    mime_type = (
        supplied_mime
        if supplied_mime in ALLOWED_MIME_TYPES
        else MIME_BY_EXTENSION[extension]
    )

    stream = file_storage.stream
    current_position = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(current_position)
    if size <= 0:
        raise ValueError("The image is empty.")
    if size > MAX_IMAGE_BYTES:
        raise ValueError("The image exceeds Shopify's 20 MB limit.")
    return {"filename": filename, "mime_type": mime_type, "size": size}


def _first_user_error(payload: Dict[str, Any]) -> str:
    errors = payload.get("userErrors") or []
    if not errors:
        return ""
    return "; ".join(
        (error.get("message") or "Shopify rejected the image")
        for error in errors
    )[:800]


def _create_staged_target(filename: str, mime_type: str) -> Dict[str, Any]:
    data = shopify_gql(
        STAGED_UPLOAD_MUTATION,
        {
            "input": [{
                "filename": filename,
                "mimeType": mime_type,
                "httpMethod": "POST",
                "resource": "IMAGE",
            }]
        },
    )
    payload = data.get("stagedUploadsCreate") or {}
    error = _first_user_error(payload)
    if error:
        raise ShopifyFileUploadError(error)
    targets = payload.get("stagedTargets") or []
    if len(targets) != 1:
        raise ShopifyFileUploadError(
            "Shopify did not return an image upload target."
        )
    return targets[0]


def _send_to_staged_target(target, file_storage, metadata):
    parameters = target.get("parameters") or []
    form_fields = {
        str(parameter.get("name")): str(parameter.get("value"))
        for parameter in parameters
        if parameter.get("name") is not None
    }
    upload_url = target.get("url")
    if not upload_url or not target.get("resourceUrl"):
        raise ShopifyFileUploadError("Shopify returned an incomplete upload target.")

    file_storage.stream.seek(0)
    try:
        response = requests.post(
            upload_url,
            data=form_fields,
            files={
                "file": (
                    metadata["filename"],
                    file_storage.stream,
                    metadata["mime_type"],
                )
            },
            timeout=(20, 180),
        )
    except requests.RequestException as exc:
        raise ShopifyFileUploadError(
            "The image could not be transferred to Shopify storage."
        ) from exc
    if response.status_code not in (200, 201, 204):
        raise ShopifyFileUploadError(
            "Shopify storage rejected the image "
            f"(HTTP {response.status_code})."
        )


def _create_shopify_file(target, metadata) -> Dict[str, Any]:
    data = shopify_gql(
        FILE_CREATE_MUTATION,
        {
            "files": [{
                "contentType": "IMAGE",
                "originalSource": target["resourceUrl"],
                "filename": metadata["filename"],
            }]
        },
    )
    payload = data.get("fileCreate") or {}
    error = _first_user_error(payload)
    if error:
        raise ShopifyFileUploadError(error)
    files = payload.get("files") or []
    if len(files) != 1 or not files[0]:
        raise ShopifyFileUploadError(
            "Shopify accepted the upload but did not create a Content file."
        )
    created = files[0]
    if created.get("fileStatus") == "FAILED":
        raise ShopifyFileUploadError("Shopify could not process the image.")
    return created


def _wait_for_file_status(created: Dict[str, Any]) -> Dict[str, Any]:
    """Briefly confirm asynchronous processing without turning polling into load."""
    if created.get("fileStatus") in {"READY", "FAILED"}:
        return created
    file_id = created.get("id")
    if not file_id:
        return created
    for _ in range(3):
        time.sleep(0.6)
        try:
            node = shopify_gql(FILE_STATUS_QUERY, {"id": file_id}).get("node")
        except Exception:
            # fileCreate already succeeded. A status-query failure must not
            # encourage the user to retry and create a duplicate file.
            return created
        if isinstance(node, dict):
            created.update(node)
        if created.get("fileStatus") == "FAILED":
            raise ShopifyFileUploadError("Shopify could not process the image.")
        if created.get("fileStatus") == "READY":
            break
    return created


def upload_image_to_shopify(file_storage) -> Dict[str, Any]:
    """Upload one image without retaining a server-side copy."""
    metadata = validate_image_upload(file_storage)
    with SHOPIFY_FILE_UPLOAD_SLOTS:
        target = _create_staged_target(
            metadata["filename"], metadata["mime_type"]
        )
        _send_to_staged_target(target, file_storage, metadata)
        created = _create_shopify_file(target, metadata)
        created = _wait_for_file_status(created)
    return {
        "filename": metadata["filename"],
        "size": metadata["size"],
        "shopify_file_id": created.get("id"),
        "file_status": created.get("fileStatus") or "PROCESSING",
    }
