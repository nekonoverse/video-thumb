"""E2E テスト用 fixture。

実コンテナ (Docker) に対して HTTP で叩く。接続先は環境変数で切替:
  E2E_BASE_URL  : サービスのベース URL (default: http://localhost:8005)
  E2E_VIDEO_URL : /thumbnail_from_url で投げる動画 URL (未設定時は URL 系テストを skip)
"""

import os
import time
from pathlib import Path

import httpx
import pytest


@pytest.fixture(scope="session")
def e2e_base_url() -> str:
    return os.environ.get("E2E_BASE_URL", "http://localhost:8005")


@pytest.fixture(scope="session")
def e2e_video_url() -> str:
    url = os.environ.get("E2E_VIDEO_URL")
    if not url:
        pytest.skip("E2E_VIDEO_URL 未設定のためスキップ")
    return url


@pytest.fixture(scope="session")
def fixture_mp4() -> Path:
    p = Path(__file__).resolve().parent.parent / "fixtures" / "sample.mp4"
    if not p.exists():
        pytest.skip(f"fixture mp4 が無いためスキップ: {p}")
    return p


@pytest.fixture(scope="session", autouse=True)
def wait_for_health(e2e_base_url):
    """サービスが /health で ffmpeg_available=true を返すまで最大 30 秒待つ。"""
    deadline = time.time() + 30
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{e2e_base_url}/health", timeout=2)
            if r.status_code == 200 and r.json().get("ffmpeg_available"):
                return
        except Exception as e:
            last_err = e
        time.sleep(1)
    pytest.fail(f"サービス起動待ちタイムアウト ({e2e_base_url}): {last_err}")
