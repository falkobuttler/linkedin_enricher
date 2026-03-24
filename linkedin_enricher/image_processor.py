"""Download and resize LinkedIn profile photos."""

import sys
from io import BytesIO
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image, ImageOps

from .config import PHOTO_SIZE_PX, PHOTOS_DIR

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def download_and_resize(photo_url: str, contact_id: str) -> Optional[Path]:
    """
    Download a LinkedIn profile photo, resize to a square JPEG, and save locally.
    Returns the local path or None on failure.
    """
    # Sanitize contact_id for use as filename (AppleScript GUIDs contain colons)
    safe_id = contact_id.replace(":", "_").replace("/", "_")
    local_path = PHOTOS_DIR / f"{safe_id}.jpg"

    if local_path.exists():
        return local_path

    try:
        response = httpx.get(
            photo_url,
            headers=_HEADERS,
            follow_redirects=True,
            timeout=15.0,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(
            f"[image] HTTP {exc.response.status_code} for {contact_id}",
            file=sys.stderr,
        )
        return None
    except Exception as exc:
        print(f"[image] Download failed for {contact_id}: {exc}", file=sys.stderr)
        return None

    try:
        img = Image.open(BytesIO(response.content))
        img = img.convert("RGB")
        img = ImageOps.exif_transpose(img)
        size = PHOTO_SIZE_PX
        img = ImageOps.fit(img, (size, size), Image.LANCZOS)
        img.save(str(local_path), "JPEG", quality=90, optimize=True)
    except Exception as exc:
        print(f"[image] Processing failed for {contact_id}: {exc}", file=sys.stderr)
        return None

    return local_path


def cleanup_photo(contact_id: str) -> None:
    """Remove the cached photo for a contact (e.g. after rejection)."""
    safe_id = contact_id.replace(":", "_").replace("/", "_")
    local_path = PHOTOS_DIR / f"{safe_id}.jpg"
    if local_path.exists():
        local_path.unlink()
