#!/usr/bin/env python3
"""Cache the latest Instagram posts for the static gallery.

The browser must never call Meta itself: that would publish the access token in
page source or network traffic.  This script runs server-side in GitHub Actions,
reads ``INSTAGRAM_ACCESS_TOKEN`` from the environment, and writes only public
post metadata plus web-sized local thumbnails into the data branch:

    instagram-posts.json
    instagram/<media-id>.jpg

The gallery fetches that same-origin JSON.  If this script or Meta is down, the
last successful cache remains deployed; if no cache exists, gallery.html keeps
its hand-written photos as the fallback.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import check_meta_integration

GRAPH_API_VERSION = check_meta_integration.GRAPH_API_VERSION
MEDIA_API_URL = f"https://graph.instagram.com/{GRAPH_API_VERSION}/me/media"
MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,media_url,permalink,"
    "thumbnail_url,timestamp,children{id,media_type,media_url,thumbnail_url}"
)
DEFAULT_LIMIT = 9
API_LIMIT = 25
TIMEOUT = 30
MAX_IMAGE_BYTES = 20 * 1024 * 1024
USER_AGENT = "boscafebikers-instagram-sync/1.0"
IMAGE_DIR = "instagram"
JSON_NAME = "instagram-posts.json"
IMAGE_MAX_PX = 1000
IMAGE_QUALITY = 78
RESIZE_TIMEOUT = 60
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


class InstagramSyncError(RuntimeError):
    """The live Instagram cache could not be refreshed safely."""


def media_request(token: str) -> urllib.request.Request:
    """Build the media-list request with the token in a header, never its URL."""
    query = urllib.parse.urlencode({"fields": MEDIA_FIELDS, "limit": str(API_LIMIT)})
    return urllib.request.Request(
        f"{MEDIA_API_URL}?{query}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
        },
    )


def fetch_media(token: str, opener=None) -> list:
    """Fetch the newest media objects from Instagram API with Instagram Login."""
    token = str(token or "").strip()
    if not token:
        raise InstagramSyncError("INSTAGRAM_ACCESS_TOKEN is missing")
    opener = opener or urllib.request.urlopen
    try:
        with opener(media_request(token), timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        # Do not print Meta's body. OAuth error text can repeat credential data.
        raise InstagramSyncError(
            f"Instagram media request was rejected (HTTP {exc.code})"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise InstagramSyncError("could not reach the Instagram media API") from None

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InstagramSyncError("Instagram media API returned non-JSON data") from None
    media = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(media, list):
        raise InstagramSyncError("Instagram media API returned an unexpected response")
    return media


def _https_url(value) -> Optional[str]:
    """A usable HTTPS URL, or None for malformed API data."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return value


def _permalink(value) -> Optional[str]:
    """Accept only actual Instagram post links for the visitor-facing anchor."""
    url = _https_url(value)
    if not url:
        return None
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host not in {"instagram.com", "www.instagram.com"}:
        return None
    return url


def _child_data(media: dict) -> list:
    children = media.get("children")
    data = children.get("data") if isinstance(children, dict) else None
    return data if isinstance(data, list) else []


def image_url(media: dict) -> Optional[str]:
    """Select an image, carousel cover, or video/Reel thumbnail."""
    media_type = str(media.get("media_type") or "").upper()
    if media_type == "CAROUSEL_ALBUM":
        for child in _child_data(media):
            if not isinstance(child, dict):
                continue
            child_type = str(child.get("media_type") or "").upper()
            preferred = (
                child.get("thumbnail_url") if child_type == "VIDEO"
                else child.get("media_url")
            )
            url = _https_url(preferred) or _https_url(child.get("thumbnail_url"))
            if url:
                return url
        return None
    if media_type == "VIDEO":
        return _https_url(media.get("thumbnail_url")) or _https_url(media.get("media_url"))
    if media_type == "IMAGE":
        return _https_url(media.get("media_url"))
    return None


def normalize_media(media: dict) -> Optional[dict]:
    """Public metadata plus the private-to-the-sync source image URL."""
    if not isinstance(media, dict):
        return None
    media_id = str(media.get("id") or "").strip()
    permalink = _permalink(media.get("permalink"))
    source = image_url(media)
    if not SAFE_ID.fullmatch(media_id) or not permalink or not source:
        return None

    media_type = str(media.get("media_type") or "").upper()
    product_type = str(media.get("media_product_type") or "").upper()
    if media_type == "CAROUSEL_ALBUM":
        kind = "Carousel"
    elif product_type == "REELS":
        kind = "Reel"
    elif media_type == "VIDEO":
        kind = "Video"
    else:
        kind = "Post"

    caption = media.get("caption")
    caption = " ".join(caption.split()) if isinstance(caption, str) else ""
    timestamp = media.get("timestamp")
    timestamp = timestamp.strip() if isinstance(timestamp, str) else ""
    return {
        "id": media_id,
        "caption": caption,
        "kind": kind,
        "permalink": permalink,
        "timestamp": timestamp,
        "source_image": source,
    }


def download_image(url: str, opener=None) -> bytes:
    """Download one public CDN image without sending the Meta credential."""
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener(request, timeout=TIMEOUT) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        raise InstagramSyncError("could not download an Instagram thumbnail") from None
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise InstagramSyncError("Instagram thumbnail was empty or too large")
    return data


def imagemagick(which=None) -> Optional[str]:
    """Find ImageMagick 7 (magick) or 6 (convert)."""
    which = which or shutil.which
    for name in ("magick", "convert"):
        binary = which(name)
        if binary:
            return binary
    return None


def resize_image(data: bytes, binary: str) -> Optional[bytes]:
    """Strip metadata and turn an Instagram image into a bounded JPEG."""
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "instagram.in"
        target = Path(tmp) / "instagram.jpg"
        source.write_bytes(data)
        command = [
            binary,
            f"{source}[0]",
            "-auto-orient",
            "-strip",
            "-resize",
            f"{IMAGE_MAX_PX}x{IMAGE_MAX_PX}>",
            "-quality",
            str(IMAGE_QUALITY),
            f"jpg:{target}",
        ]
        try:
            done = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=RESIZE_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if done.returncode != 0 or not target.is_file():
            return None
        return target.read_bytes()


def _write_if_changed(path: Path, data: bytes) -> bool:
    if path.is_file() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return True


def sync_gallery(
    token: str,
    data_dir: Path,
    limit: int = DEFAULT_LIMIT,
    *,
    profile_check: Optional[Callable] = None,
    media_fetch: Optional[Callable] = None,
    image_fetch: Optional[Callable] = None,
    resizer: Optional[Callable] = None,
    dry_run: bool = False,
    output: Optional[Callable] = None,
) -> dict:
    """Refresh the gallery cache and return a small run summary.

    All network/image seams are injectable so the normal test suite remains
    offline. Files are replaced only after useful media was found, preserving
    the previous cache during an API outage or malformed response.
    """
    output = output or print
    profile_check = profile_check or check_meta_integration.check_integration
    media_fetch = media_fetch or fetch_media
    image_fetch = image_fetch or download_image

    try:
        username = profile_check(token)
    except check_meta_integration.MetaIntegrationError as exc:
        raise InstagramSyncError(str(exc)) from None
    if str(username).lower().lstrip("@") != check_meta_integration.EXPECTED_USERNAME:
        raise InstagramSyncError("Instagram token belongs to the wrong account")

    raw_media = media_fetch(token)
    candidates = []
    for item in raw_media:
        normalized = normalize_media(item)
        if normalized:
            candidates.append(normalized)
        if len(candidates) >= limit:
            break
    if not candidates:
        raise InstagramSyncError("Instagram returned no renderable posts")

    if dry_run:
        output(f"instagram: would cache {len(candidates)} latest post(s)")
        return {"posts": len(candidates), "images_added": 0, "changed": False}

    if resizer is None:
        binary = imagemagick()
        if not binary:
            raise InstagramSyncError("ImageMagick is required to cache Instagram images")
        resizer = lambda data: resize_image(data, binary)

    data_dir = Path(data_dir)
    image_dir = data_dir / IMAGE_DIR
    image_dir.mkdir(parents=True, exist_ok=True)
    posts = []
    images_added = 0
    for candidate in candidates:
        target = image_dir / f"{candidate['id']}.jpg"
        if not target.is_file():
            try:
                original = image_fetch(candidate["source_image"])
                jpeg = resizer(original)
            except (InstagramSyncError, OSError, ValueError, TypeError):
                jpeg = None
            if not jpeg:
                output(f"warning: could not cache Instagram {candidate['kind'].lower()} {candidate['id']}")
                continue
            _write_if_changed(target, jpeg)
            images_added += 1

        posts.append({
            "id": candidate["id"],
            "caption": candidate["caption"],
            "kind": candidate["kind"],
            "permalink": candidate["permalink"],
            "timestamp": candidate["timestamp"],
            "image": f"{IMAGE_DIR}/{target.name}",
        })

    if not posts:
        raise InstagramSyncError("none of the latest Instagram images could be cached")

    payload = {
        "account": check_meta_integration.EXPECTED_USERNAME,
        "count": len(posts),
        "posts": posts,
    }
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    changed = _write_if_changed(data_dir / JSON_NAME, encoded)

    keep = {Path(post["image"]).name for post in posts}
    removed = 0
    for old in image_dir.glob("*.jpg"):
        if old.name not in keep:
            old.unlink()
            removed += 1

    output(
        f"instagram: {len(posts)} post(s), {images_added} image(s) cached, "
        f"{removed} old image(s) removed"
    )
    return {
        "posts": len(posts),
        "images_added": images_added,
        "images_removed": removed,
        "changed": changed or bool(images_added or removed),
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "site",
        help="directory that receives instagram-posts.json and instagram/",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"number of latest posts to cache (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="verify profile/media access but write and download nothing",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= API_LIMIT:
        parser.error(f"--limit must be between 1 and {API_LIMIT}")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
    try:
        sync_gallery(token, args.data_dir, args.limit, dry_run=args.dry_run)
    except InstagramSyncError as exc:
        print(f"Instagram gallery sync failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
