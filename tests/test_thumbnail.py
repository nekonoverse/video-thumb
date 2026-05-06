"""multipart の /thumbnail エンドポイントのテスト。"""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_check_ffmpeg", lambda: True)
    monkeypatch.setattr(main, "_check_ffprobe", lambda: True)
    with TestClient(main.app) as c:
        yield c


def _open_webp(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_thumbnail_basic(client, sample_mp4_bytes):
    resp = client.post(
        "/thumbnail",
        files={"file": ("v.mp4", sample_mp4_bytes, "video/mp4")},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"
    img = _open_webp(resp.content)
    assert img.format == "WEBP"
    # 元 320x240 → MAX_DIMENSION=800 でキャップなので元サイズに収まる
    assert max(img.size) <= main.MAX_DIMENSION


def test_thumbnail_includes_video_mimetype_header(client, sample_mp4_bytes):
    """ffprobe で判別された MIME が X-Video-Mimetype に入る。"""
    resp = client.post(
        "/thumbnail",
        files={"file": ("v.mp4", sample_mp4_bytes, "video/mp4")},
    )
    assert resp.status_code == 200
    # libx264 で生成した mp4 → format_name は "mov,mp4,m4a,3gp,3g2,mj2"
    assert resp.headers.get("X-Video-Mimetype") == "video/mp4"


def test_thumbnail_max_dimension_param(client, sample_mp4_bytes):
    """Form max_dimension が反映されること。"""
    resp = client.post(
        "/thumbnail",
        files={"file": ("v.mp4", sample_mp4_bytes, "video/mp4")},
        data={"max_dimension": "100"},
    )
    assert resp.status_code == 200
    img = _open_webp(resp.content)
    assert max(img.size) == 100


def test_thumbnail_max_dimension_capped_by_env(
    client, sample_mp4_bytes, monkeypatch
):
    """環境変数より大きい値はキャップされる。"""
    monkeypatch.setattr(main, "MAX_DIMENSION", 150)
    resp = client.post(
        "/thumbnail",
        files={"file": ("v.mp4", sample_mp4_bytes, "video/mp4")},
        data={"max_dimension": "9999"},
    )
    assert resp.status_code == 200
    img = _open_webp(resp.content)
    assert max(img.size) == 150


def test_thumbnail_invalid_max_dimension(client, sample_mp4_bytes):
    resp = client.post(
        "/thumbnail",
        files={"file": ("v.mp4", sample_mp4_bytes, "video/mp4")},
        data={"max_dimension": "0"},
    )
    assert resp.status_code == 400


def test_thumbnail_empty_file(client):
    resp = client.post(
        "/thumbnail",
        files={"file": ("v.mp4", b"", "video/mp4")},
    )
    assert resp.status_code == 400
