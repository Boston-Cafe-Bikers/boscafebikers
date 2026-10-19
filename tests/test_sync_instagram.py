"""Offline tests for the Instagram-to-static-gallery cache."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import sync_instagram  # noqa: E402


IMAGE = {
    "id": "image-1",
    "caption": "  Sunday ride\nwith coffee  ",
    "media_type": "IMAGE",
    "media_product_type": "FEED",
    "media_url": "https://cdn.example/image-1.jpg",
    "permalink": "https://www.instagram.com/p/image-1/",
    "timestamp": "2026-09-20T14:00:00+0000",
}

REEL = {
    "id": "reel_2",
    "caption": "Rolling through Boston",
    "media_type": "VIDEO",
    "media_product_type": "REELS",
    "media_url": "https://cdn.example/reel-2.mp4",
    "thumbnail_url": "https://cdn.example/reel-2.jpg",
    "permalink": "https://www.instagram.com/reel/reel_2/",
    "timestamp": "2026-09-19T14:00:00+0000",
}

CAROUSEL = {
    "id": "carousel_3",
    "caption": "Nine photos from the ride",
    "media_type": "CAROUSEL_ALBUM",
    "media_product_type": "CAROUSEL_CONTAINER",
    "permalink": "https://www.instagram.com/p/carousel_3/",
    "timestamp": "2026-09-18T14:00:00+0000",
    "children": {
        "data": [
            {
                "id": "child-1",
                "media_type": "IMAGE",
                "media_url": "https://cdn.example/carousel-cover.jpg",
            },
            {
                "id": "child-2",
                "media_type": "IMAGE",
                "media_url": "https://cdn.example/carousel-second.jpg",
            },
        ]
    },
}


def profile_check(_token):
    return "bostoncafebikers"


def test_media_request_keeps_the_token_out_of_the_url():
    request = sync_instagram.media_request("secret-token")
    assert request.full_url.startswith("https://graph.instagram.com/v26.0/me/media?")
    assert "secret-token" not in request.full_url
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert "children%7B" in request.full_url


def test_selects_image_reel_and_carousel_cover_urls():
    assert sync_instagram.image_url(IMAGE) == "https://cdn.example/image-1.jpg"
    assert sync_instagram.image_url(REEL) == "https://cdn.example/reel-2.jpg"
    assert sync_instagram.image_url(CAROUSEL) == (
        "https://cdn.example/carousel-cover.jpg"
    )


def test_normalization_rejects_unsafe_ids_links_and_sources():
    assert sync_instagram.normalize_media({**IMAGE, "id": "../escape"}) is None
    assert sync_instagram.normalize_media({
        **IMAGE, "permalink": "https://example.com/not-instagram"
    }) is None
    assert sync_instagram.normalize_media({
        **IMAGE, "media_url": "http://cdn.example/insecure.jpg"
    }) is None


def test_sync_writes_public_metadata_and_local_images(tmp_path):
    downloads = []

    def image_fetch(url):
        downloads.append(url)
        return ("source:" + url).encode()

    result = sync_instagram.sync_gallery(
        "secret-token",
        tmp_path,
        profile_check=profile_check,
        media_fetch=lambda _token: [IMAGE, REEL, CAROUSEL],
        image_fetch=image_fetch,
        resizer=lambda data: b"jpeg:" + data,
        output=lambda _line: None,
    )

    assert result == {
        "posts": 3,
        "images_added": 3,
        "images_removed": 0,
        "changed": True,
    }
    payload = json.loads((tmp_path / "instagram-posts.json").read_text())
    assert payload["account"] == "bostoncafebikers"
    assert payload["count"] == 3
    assert [post["kind"] for post in payload["posts"]] == [
        "Post", "Reel", "Carousel"
    ]
    assert payload["posts"][0]["caption"] == "Sunday ride with coffee"
    assert payload["posts"][0]["image"] == "instagram/image-1.jpg"
    assert "source_image" not in json.dumps(payload)
    assert "cdn.example" not in json.dumps(payload)
    assert (tmp_path / "instagram" / "reel_2.jpg").read_bytes().startswith(b"jpeg:")
    assert downloads == [
        "https://cdn.example/image-1.jpg",
        "https://cdn.example/reel-2.jpg",
        "https://cdn.example/carousel-cover.jpg",
    ]


def test_second_identical_sync_reuses_images_and_writes_nothing(tmp_path):
    options = {
        "profile_check": profile_check,
        "media_fetch": lambda _token: [IMAGE],
        "image_fetch": lambda _url: b"source",
        "resizer": lambda data: b"jpeg:" + data,
        "output": lambda _line: None,
    }
    sync_instagram.sync_gallery("secret-token", tmp_path, **options)

    def no_download(_url):
        raise AssertionError("an immutable Instagram post was downloaded twice")

    result = sync_instagram.sync_gallery(
        "secret-token", tmp_path, **{**options, "image_fetch": no_download}
    )
    assert result["changed"] is False
    assert result["images_added"] == 0


def test_sync_prunes_images_that_are_no_longer_in_the_latest_nine(tmp_path):
    old = tmp_path / "instagram" / "old-post.jpg"
    old.parent.mkdir()
    old.write_bytes(b"old")

    result = sync_instagram.sync_gallery(
        "secret-token",
        tmp_path,
        profile_check=profile_check,
        media_fetch=lambda _token: [IMAGE],
        image_fetch=lambda _url: b"source",
        resizer=lambda _data: b"jpeg",
        output=lambda _line: None,
    )
    assert result["images_removed"] == 1
    assert not old.exists()


def test_dry_run_checks_media_but_does_not_download_or_write(tmp_path):
    calls = []

    result = sync_instagram.sync_gallery(
        "secret-token",
        tmp_path,
        profile_check=profile_check,
        media_fetch=lambda token: calls.append(token) or [IMAGE, REEL],
        image_fetch=lambda _url: pytest.fail("dry run downloaded an image"),
        dry_run=True,
        output=lambda _line: None,
    )
    assert result == {"posts": 2, "images_added": 0, "changed": False}
    assert calls == ["secret-token"]
    assert list(tmp_path.iterdir()) == []


def test_api_or_empty_media_failure_preserves_the_existing_cache(tmp_path):
    cache = tmp_path / "instagram-posts.json"
    cache.write_text('{"old": true}\n')

    with pytest.raises(sync_instagram.InstagramSyncError, match="no renderable"):
        sync_instagram.sync_gallery(
            "secret-token",
            tmp_path,
            profile_check=profile_check,
            media_fetch=lambda _token: [],
            output=lambda _line: None,
        )
    assert cache.read_text() == '{"old": true}\n'


def test_limit_takes_only_the_newest_renderable_posts(tmp_path):
    invalid = {**IMAGE, "id": "bad/id"}
    sync_instagram.sync_gallery(
        "secret-token",
        tmp_path,
        limit=2,
        profile_check=profile_check,
        media_fetch=lambda _token: [invalid, IMAGE, REEL, CAROUSEL],
        image_fetch=lambda _url: b"source",
        resizer=lambda _data: b"jpeg",
        output=lambda _line: None,
    )
    payload = json.loads((tmp_path / "instagram-posts.json").read_text())
    assert [post["id"] for post in payload["posts"]] == ["image-1", "reel_2"]
