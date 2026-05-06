"""稼働中コンテナに対する E2E テスト。"""

import io

import httpx
import pytest
from PIL import Image

pytestmark = pytest.mark.e2e


def _open_webp(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_health(e2e_base_url):
    r = httpx.get(f"{e2e_base_url}/health", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["ffmpeg_available"] is True
    assert body["ffprobe_available"] is True


def test_thumbnail_multipart(e2e_base_url, fixture_mp4):
    with open(fixture_mp4, "rb") as fh:
        r = httpx.post(
            f"{e2e_base_url}/thumbnail",
            files={"file": ("sample.mp4", fh, "video/mp4")},
            timeout=30,
        )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/webp"
    img = _open_webp(r.content)
    assert img.format == "WEBP"
    assert int(r.headers["X-Video-Width"]) == 320
    assert int(r.headers["X-Video-Height"]) == 240
    assert r.headers.get("X-Video-Mimetype") == "video/mp4"


def test_thumbnail_multipart_max_dimension(e2e_base_url, fixture_mp4):
    with open(fixture_mp4, "rb") as fh:
        r = httpx.post(
            f"{e2e_base_url}/thumbnail",
            files={"file": ("sample.mp4", fh, "video/mp4")},
            data={"max_dimension": "100"},
            timeout=30,
        )
    assert r.status_code == 200, r.text
    img = _open_webp(r.content)
    assert max(img.size) == 100


def test_thumbnail_from_url(e2e_base_url, e2e_video_url):
    r = httpx.post(
        f"{e2e_base_url}/thumbnail_from_url",
        json={"url": e2e_video_url},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/webp"
    assert r.headers.get("X-Video-Mimetype") == "video/mp4"
    img = _open_webp(r.content)
    assert img.format == "WEBP"


def test_thumbnail_from_url_max_dimension(e2e_base_url, e2e_video_url):
    r = httpx.post(
        f"{e2e_base_url}/thumbnail_from_url",
        json={"url": e2e_video_url, "max_dimension": 80},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    img = _open_webp(r.content)
    assert max(img.size) == 80


def test_thumbnail_from_url_invalid_scheme(e2e_base_url):
    r = httpx.post(
        f"{e2e_base_url}/thumbnail_from_url",
        json={"url": "ftp://example.com/v.mp4"},
        timeout=10,
    )
    assert r.status_code == 400
