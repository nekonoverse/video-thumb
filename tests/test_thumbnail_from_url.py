"""/thumbnail_from_url エンドポイントのユニットテスト。

実装は HEAD でリダイレクトを 200 まで追跡し、その後 ffprobe/ffmpeg に
URL を直接渡す方式。subprocess は mock 困難なため、_process_video を
スタブ化して HEAD 追跡 / SSRF / Content-Length / リダイレクト周辺を
集中的に検証する。実 URL → WebP の正常系は E2E でカバー。
"""

import httpx
import pytest
from fastapi.testclient import TestClient

import main


FAKE_WEBP = b"FAKE-WEBP-BYTES"
FAKE_META = {"duration": 2.0, "width": 320, "height": 240}


async def _stub_process_video(source, max_dim, is_url=False):
    return FAKE_WEBP, {**FAKE_META, "max_dim": max_dim, "is_url": is_url, "src": source}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_check_ffmpeg", lambda: True)
    monkeypatch.setattr(main, "_check_ffprobe", lambda: True)
    monkeypatch.setattr(main, "ALLOW_PRIVATE_URL", True)
    monkeypatch.setattr(main, "_process_video", _stub_process_video)
    with TestClient(main.app) as c:
        yield c


def test_from_url_basic(client, httpx_mock):
    httpx_mock.add_response(
        method="HEAD",
        url="https://example.com/v.mp4",
        headers={"content-length": "12345"},
    )
    resp = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/v.mp4"},
    )
    assert resp.status_code == 200
    assert resp.content == FAKE_WEBP
    assert resp.headers["content-type"] == "image/webp"


def test_from_url_passes_final_url_to_ffmpeg(client, httpx_mock, monkeypatch):
    """HEAD で確定した URL が _process_video に is_url=True で渡る。"""
    captured = {}

    async def capture(source, max_dim, is_url=False):
        captured["source"] = source
        captured["is_url"] = is_url
        captured["max_dim"] = max_dim
        return FAKE_WEBP, FAKE_META

    monkeypatch.setattr(main, "_process_video", capture)
    httpx_mock.add_response(
        method="HEAD",
        url="https://example.com/v.mp4",
        headers={"content-length": "12345"},
    )
    r = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/v.mp4", "max_dimension": 100},
    )
    assert r.status_code == 200
    assert captured["source"] == "https://example.com/v.mp4"
    assert captured["is_url"] is True
    assert captured["max_dim"] == 100


def test_from_url_max_dimension_capped(client, httpx_mock, monkeypatch):
    monkeypatch.setattr(main, "MAX_DIMENSION", 120)
    captured = {}

    async def capture(source, max_dim, is_url=False):
        captured["max_dim"] = max_dim
        return FAKE_WEBP, FAKE_META

    monkeypatch.setattr(main, "_process_video", capture)
    httpx_mock.add_response(
        method="HEAD",
        url="https://example.com/v.mp4",
        headers={"content-length": "12345"},
    )
    r = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/v.mp4", "max_dimension": 9999},
    )
    assert r.status_code == 200
    assert captured["max_dim"] == 120


def test_from_url_rejects_invalid_scheme(client):
    for bad in [
        "ftp://example.com/v.mp4",
        "file:///etc/passwd",
        "gopher://example.com/v.mp4",
    ]:
        r = client.post("/thumbnail_from_url", json={"url": bad})
        assert r.status_code == 400, bad


def test_from_url_rejects_private_ip_by_default(monkeypatch):
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
            r = c.post("/thumbnail_from_url", json={"url": bad})
            assert r.status_code == 400, bad


def test_from_url_content_length_too_large(client, httpx_mock, monkeypatch):
    monkeypatch.setattr(main, "MAX_FILE_SIZE", 1024)
    httpx_mock.add_response(
        method="HEAD",
        url="https://example.com/big.mp4",
        headers={"content-length": "999999999"},
    )
    r = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/big.mp4"},
    )
    assert r.status_code == 400


def test_from_url_missing_content_length(client, httpx_mock):
    """Content-Length を返さないサーバは 400 で拒否する。"""
    httpx_mock.add_response(
        method="HEAD",
        url="https://example.com/v.mp4",
        headers={},
    )
    r = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/v.mp4"},
    )
    assert r.status_code == 400


def test_from_url_zero_content_length(client, httpx_mock):
    httpx_mock.add_response(
        method="HEAD",
        url="https://example.com/v.mp4",
        headers={"content-length": "0"},
    )
    r = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/v.mp4"},
    )
    assert r.status_code == 400


def test_from_url_http_error(client, httpx_mock):
    httpx_mock.add_response(
        method="HEAD",
        url="https://example.com/missing.mp4",
        status_code=404,
    )
    r = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/missing.mp4"},
    )
    assert r.status_code == 502


def test_from_url_timeout(client, httpx_mock):
    httpx_mock.add_exception(httpx.ReadTimeout("timeout"))
    r = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/slow.mp4"},
    )
    assert r.status_code == 504


def test_from_url_redirect_followed(client, httpx_mock, monkeypatch):
    captured = {}

    async def capture(source, max_dim, is_url=False):
        captured["source"] = source
        return FAKE_WEBP, FAKE_META

    monkeypatch.setattr(main, "_process_video", capture)
    httpx_mock.add_response(
        method="HEAD",
        url="https://example.com/redir.mp4",
        status_code=302,
        headers={"location": "https://example.com/v.mp4"},
    )
    httpx_mock.add_response(
        method="HEAD",
        url="https://example.com/v.mp4",
        headers={"content-length": "12345"},
    )
    r = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/redir.mp4"},
    )
    assert r.status_code == 200
    # 確定 URL が ffmpeg に渡る
    assert captured["source"] == "https://example.com/v.mp4"


def test_from_url_redirect_to_private_blocked(monkeypatch, httpx_mock):
    """リダイレクト先のプライベート IP も拒否する。"""
    monkeypatch.setattr(main, "_check_ffmpeg", lambda: True)
    monkeypatch.setattr(main, "_check_ffprobe", lambda: True)
    monkeypatch.setattr(main, "ALLOW_PRIVATE_URL", False)
    httpx_mock.add_response(
        method="HEAD",
        url="https://example.com/redir.mp4",
        status_code=302,
        headers={"location": "http://127.0.0.1/inner.mp4"},
    )
    with TestClient(main.app) as c:
        r = c.post(
            "/thumbnail_from_url",
            json={"url": "https://example.com/redir.mp4"},
        )
        assert r.status_code == 400


def test_from_url_redirect_loop_limit(client, httpx_mock):
    """リダイレクト回数の上限を超えると 502。"""
    # MAX_REDIRECTS + 1 回 HEAD して全て 302 → ループ抜けで 502
    for i in range(main.MAX_REDIRECTS + 1):
        httpx_mock.add_response(
            method="HEAD",
            url=f"https://example.com/r{i}.mp4",
            status_code=302,
            headers={"location": f"https://example.com/r{i + 1}.mp4"},
        )
    r = client.post(
        "/thumbnail_from_url",
        json={"url": "https://example.com/r0.mp4"},
    )
    assert r.status_code == 502


def test_url_input_args_disables_ffmpeg_redirects():
    """ffmpeg 自身のリダイレクト追従が無効化されている。

    Python 側 HEAD で SSRF 検証した終端 URL から、ffmpeg GET 時に Location で
    別ホストへリダイレクトされる経路を塞ぐため -max_redirects 0 必須。
    """
    args = main._url_input_args()
    # -max_redirects 0 が連続して含まれる
    pairs = list(zip(args, args[1:]))
    assert ("-max_redirects", "0") in pairs
    # SSRF 系の他のガードも維持されている
    assert "-protocol_whitelist" in args
    assert "-rw_timeout" in args


def test_from_url_head_method_not_allowed_falls_back_to_get_range(client, httpx_mock):
    """HEAD 405 (S3 presigned URL 等で起こりがち) のとき GET Range フォールバック。

    Content-Range の `bytes 0-0/<total>` から全体サイズを抽出する。
    """
    # HEAD は 405
    httpx_mock.add_response(
        method="HEAD",
        url="https://s3.example.com/v.mp4?sig=abc",
        status_code=405,
    )
    # GET Range bytes=0-0 → 206 Partial Content
    httpx_mock.add_response(
        method="GET",
        url="https://s3.example.com/v.mp4?sig=abc",
        status_code=206,
        headers={
            "content-range": "bytes 0-0/12345678",
            "content-length": "1",
        },
    )
    resp = client.post(
        "/thumbnail_from_url",
        json={"url": "https://s3.example.com/v.mp4?sig=abc"},
    )
    assert resp.status_code == 200
    assert resp.content == FAKE_WEBP


def test_from_url_head_403_falls_back_to_get_range(client, httpx_mock):
    """HEAD 403 でも GET Range にフォールバックする。"""
    httpx_mock.add_response(
        method="HEAD",
        url="https://s3.example.com/v.mp4?sig=xyz",
        status_code=403,
    )
    httpx_mock.add_response(
        method="GET",
        url="https://s3.example.com/v.mp4?sig=xyz",
        status_code=206,
        headers={"content-range": "bytes 0-0/9999"},
    )
    resp = client.post(
        "/thumbnail_from_url",
        json={"url": "https://s3.example.com/v.mp4?sig=xyz"},
    )
    assert resp.status_code == 200


def test_from_url_head_405_get_too_large(client, httpx_mock):
    """HEAD 405 → GET Range で得た Content-Range が上限超過 → 400。"""
    httpx_mock.add_response(
        method="HEAD",
        url="https://s3.example.com/big.mp4",
        status_code=405,
    )
    httpx_mock.add_response(
        method="GET",
        url="https://s3.example.com/big.mp4",
        status_code=206,
        headers={"content-range": f"bytes 0-0/{main.MAX_FILE_SIZE + 1}"},
    )
    r = client.post(
        "/thumbnail_from_url",
        json={"url": "https://s3.example.com/big.mp4"},
    )
    assert r.status_code == 400


def test_extract_total_size_prefers_content_range():
    """Content-Range と Content-Length が両方あるとき Content-Range を優先。"""
    resp = httpx.Response(
        status_code=206,
        headers={
            "content-range": "bytes 0-0/55555",
            "content-length": "1",
        },
    )
    assert main._extract_total_size(resp) == 55555


def test_extract_total_size_falls_back_to_content_length():
    """Content-Range が無いとき Content-Length を使う。"""
    resp = httpx.Response(status_code=200, headers={"content-length": "777"})
    assert main._extract_total_size(resp) == 777


def test_extract_total_size_unknown_when_total_is_star():
    """Content-Range の total が `*` のときは不明扱いで Content-Length にフォールバック。"""
    resp = httpx.Response(
        status_code=206,
        headers={"content-range": "bytes 0-0/*", "content-length": "1"},
    )
    # */1 → unknown total → Content-Length 1 にフォールバック
    assert main._extract_total_size(resp) == 1


def test_extract_total_size_returns_none_when_neither():
    """どちらのヘッダも無いとき None。"""
    resp = httpx.Response(status_code=200)
    assert main._extract_total_size(resp) is None
