from pathlib import Path

from .constants import ALLOWED_EXT, PHOTO_CATEGORIES
from .utils import safe_segment


def ensure_photo_dirs(photos_root: Path, sku: str):
    """确保三类图片目录存在"""
    if not safe_segment(sku):
        raise ValueError("invalid sku")
    base = photos_root / sku
    for folder in PHOTO_CATEGORIES.values():
        (base / folder).mkdir(parents=True, exist_ok=True)


def category_dir(photos_root: Path, sku: str, cat_key: str, *, ensure: bool = False) -> Path:
    if cat_key not in PHOTO_CATEGORIES:
        raise ValueError("invalid category")
    if not safe_segment(sku):
        raise ValueError("invalid sku")
    if ensure:
        ensure_photo_dirs(photos_root, sku)
    return (photos_root / sku / PHOTO_CATEGORIES[cat_key])


def safe_ext_ok(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in ALLOWED_EXT


def tmp_photo_dir(photos_root: Path, temp_id: str) -> Path:
    if not safe_segment(temp_id):
        raise ValueError("invalid temp_id")
    d = photos_root / "_tmp" / temp_id / PHOTO_CATEGORIES["seller"]
    d.mkdir(parents=True, exist_ok=True)
    return d


