"""/thumbnail_from_url エンドポイントのテスト。"""

import io

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_check_ffmpeg", lambda: True)
    monkeypatch.setattr(main, "_check_ffprobe", lambda: True)
    monkeypatch.setattr(main, "ALLOW_PRIVATE_URL", True)
    with TestClient(main.app) as c:
        yield c


def _open_webp(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_from_url_basic(client, sample_mp4_bytes, httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/v.mp4",
        content=sample_mp4_bytes,
        headers={"content-length": str(len(sample_mp4_bytes))},
    )
    resp = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/v.mp4"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"
    img = _open_webp(resp.content)
    assert img.format == "WEBP"


def test_from_url_max_dimension_capped(
    client, sample_mp4_bytes, httpx_mock, monkeypatch
):
    monkeypatch.setattr(main, "MAX_DIMENSION", 120)
    httpx_mock.add_response(
        url="https://example.com/v.mp4",
        content=sample_mp4_bytes,
    )
    resp = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/v.mp4", "max_dimension": 9999},
    )
    assert resp.status_code == 200
    img = _open_webp(resp.content)
    assert max(img.size) == 120


def test_from_url_max_dimension_param(client, sample_mp4_bytes, httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/v.mp4",
        content=sample_mp4_bytes,
    )
    resp = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/v.mp4", "max_dimension": 80},
    )
    assert resp.status_code == 200
    img = _open_webp(resp.content)
    assert max(img.size) == 80


def test_from_url_rejects_invalid_scheme(client):
    for bad in [
        "ftp://example.com/v.mp4",
        "file:///etc/passwd",
        "gopher://example.com/v.mp4",
    ]:
        resp = client.post("/thumbnail_from_url", json={"url": bad})
        assert resp.status_code == 400, bad


def test_from_url_rejects_private_ip_by_default(monkeypatch):
    """デフォルトではプライベート IP 宛 URL を拒否する。"""
    monkeypatch.setattr(main, "_check_ffmpeg", lambda: True)
    monkeypatch.setattr(main, "_check_ffprobe", lambda: True)
    monkeypatch.setattr(main, "ALLOW_PRIVATE_URL", False)
    with TestClient(main.app) as c:
        for bad in [
            "http://127.0.0.1/v.mp4",
            "http://10.0.0.1/v.mp4",
            "http://192.168.1.1/v.mp4",
            "http://169.254.169.254/v.mp4",
        ]:
            resp = c.post("/thumbnail_from_url", json={"url": bad})
            assert resp.status_code == 400, bad


def test_from_url_content_length_too_large(client, httpx_mock, monkeypatch):
    monkeypatch.setattr(main, "MAX_FILE_SIZE", 1024)
    httpx_mock.add_response(
        url="https://example.com/big.mp4",
        content=b"x" * 10,
        headers={"content-length": "999999999"},
    )
    resp = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/big.mp4"},
    )
    assert resp.status_code == 400


def test_from_url_actual_size_too_large(client, httpx_mock, monkeypatch):
    """Content-Length が無くても実サイズで弾く。"""
    monkeypatch.setattr(main, "MAX_FILE_SIZE", 100)
    httpx_mock.add_response(
        url="https://example.com/big.mp4",
        content=b"x" * 5000,
    )
    resp = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/big.mp4"},
    )
    assert resp.status_code == 400


def test_from_url_http_error(client, httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/missing.mp4",
        status_code=404,
    )
    resp = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/missing.mp4"},
    )
    assert resp.status_code == 502


def test_from_url_timeout(client, httpx_mock):
    httpx_mock.add_exception(httpx.ReadTimeout("timeout"))
    resp = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/slow.mp4"},
    )
    assert resp.status_code == 504


def test_from_url_redirect_followed(client, sample_mp4_bytes, httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/redir.mp4",
        status_code=302,
        headers={"location": "https://example.com/v.mp4"},
    )
    httpx_mock.add_response(
        url="https://example.com/v.mp4",
        content=sample_mp4_bytes,
    )
    resp = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/redir.mp4"},
    )
    assert resp.status_code == 200


def test_from_url_redirect_to_private_blocked(monkeypatch, httpx_mock):
    """リダイレクト先のプライベート IP も拒否する。"""
    monkeypatch.setattr(main, "_check_ffmpeg", lambda: True)
    monkeypatch.setattr(main, "_check_ffprobe", lambda: True)
    monkeypatch.setattr(main, "ALLOW_PRIVATE_URL", False)
    httpx_mock.add_response(
        url="https://example.com/redir.mp4",
        status_code=302,
        headers={"location": "http://127.0.0.1/inner.mp4"},
    )
    with TestClient(main.app) as c:
        resp = c.post(
            "/thumbnail_from_url",
            json={"url": "https://example.com/redir.mp4"},
        )
        assert resp.status_code == 400
