"""動画サムネイル生成マイクロサービス。

FFmpeg でフレーム抽出 → Pillow で WebP リサイズ・エンコード。
nekonoverse の video_thumb_queue.py から POST /thumbnail で呼び出される。
URL からの動画取得は POST /thumbnail_from_url で行う。

環境変数:
  MAX_DIMENSION:      サムネイル最大辺 (default: 800)
  WEBP_QUALITY:       WebP品質 (default: 80)
  SEEK_PERCENT:       フレーム抽出位置 % (default: 10)
  MIN_SEEK_SEC:       最小シーク位置 秒 (default: 1.0)
  MAX_SEEK_SEC:       最大シーク位置 秒 (default: 10.0)
  MAX_FILE_SIZE:      最大ファイルサイズ bytes (default: 500MB)
  ALLOW_PRIVATE_URL:  "1" でプライベートIP宛の URL を許可 (default: "0")
  URL_FETCH_TIMEOUT:  URL ダウンロード/IO タイムアウト 秒 (default: 60)
"""

import asyncio
import io
import ipaddress
import json
import logging
import os
import shutil
import socket
import tempfile

from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlparse

import httpx

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel, Field

logger = logging.getLogger("video-thumb")
logging.basicConfig(level=logging.INFO)

MAX_DIMENSION = int(os.environ.get("MAX_DIMENSION", "800"))
WEBP_QUALITY = int(os.environ.get("WEBP_QUALITY", "80"))
SEEK_PERCENT = int(os.environ.get("SEEK_PERCENT", "10"))
MIN_SEEK_SEC = float(os.environ.get("MIN_SEEK_SEC", "1.0"))
MAX_SEEK_SEC = float(os.environ.get("MAX_SEEK_SEC", "10.0"))
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", str(500 * 1024 * 1024)))
ALLOW_PRIVATE_URL = os.environ.get("ALLOW_PRIVATE_URL", "0") == "1"
URL_FETCH_TIMEOUT = float(os.environ.get("URL_FETCH_TIMEOUT", "60"))
MAX_REDIRECTS = 5

_ffmpeg_ok: bool = False
_ffprobe_ok: bool = False


def _check_ffmpeg() -> bool:
    """ffmpeg の存在確認。"""
    return shutil.which("ffmpeg") is not None


def _check_ffprobe() -> bool:
    """ffprobe の存在確認。"""
    return shutil.which("ffprobe") is not None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ffmpeg_ok, _ffprobe_ok
    _ffmpeg_ok = _check_ffmpeg()
    _ffprobe_ok = _check_ffprobe()
    if _ffmpeg_ok and _ffprobe_ok:
        logger.info("FFmpeg/FFprobe 利用可能")
    else:
        logger.warning(
            "FFmpeg/FFprobe 不足: ffmpeg=%s, ffprobe=%s", _ffmpeg_ok, _ffprobe_ok
        )
    yield


app = FastAPI(title="video-thumb", lifespan=lifespan)


class ThumbnailFromUrlRequest(BaseModel):
    url: str = Field(..., description="動画 URL (http/https のみ)")
    max_dimension: Optional[int] = Field(
        default=None, description="サムネイル最大辺 (px)。環境変数 MAX_DIMENSION でキャップ"
    )


def _resolve_max_dim(requested: Optional[int]) -> int:
    """リクエストされた max_dimension を環境変数値で上限キャップして返す。

    Raises:
        HTTPException(400): 1 未満の値が渡された場合
    """
    if requested is None:
        return MAX_DIMENSION
    if requested < 1:
        raise HTTPException(status_code=400, detail="max_dimension は 1 以上の整数")
    return min(requested, MAX_DIMENSION)


def _is_public_host(host: str) -> bool:
    """ホスト名/IP がパブリック IP に解決されるか判定する。

    プライベート/ループバック/リンクローカル/予約済み IP は False を返す。
    解決された全てのアドレスがパブリックでなければ False。
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def _validate_url(url: str) -> None:
    """URL を検証する。スキーム / ホスト / SSRF をチェック。

    Raises:
        HTTPException(400): 検証失敗
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        raise HTTPException(status_code=400, detail="URL が不正です")
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400, detail="URL スキームは http / https のみ許可"
        )
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="URL にホスト名がありません")
    if not ALLOW_PRIVATE_URL and not _is_public_host(parsed.hostname):
        raise HTTPException(
            status_code=400,
            detail="プライベート/ループバック IP 宛の URL は許可されていません",
        )


async def _resolve_final_url(url: str) -> str:
    """HEAD でリダイレクトを追跡し、最終的な 200 OK の URL を返す。

    各ホップで _validate_url を通し SSRF を防ぐ。Content-Length が
    無い / MAX_FILE_SIZE を超えるサーバは 400 で拒否する。

    Returns:
        確定 URL (httpx.URL → str)
    Raises:
        HTTPException: 検証失敗 / HTTP エラー / タイムアウト
    """
    current = url
    timeout = httpx.Timeout(URL_FETCH_TIMEOUT)

    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _validate_url(current)
            try:
                r = await client.head(current)
            except httpx.TimeoutException:
                raise HTTPException(status_code=504, detail="URL 取得タイムアウト")
            except httpx.RequestError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"URL 取得に失敗: {exc.__class__.__name__}",
                )

            if r.is_redirect:
                loc = r.headers.get("location")
                if not loc:
                    raise HTTPException(status_code=502, detail="リダイレクト先が不明")
                current = str(r.url.join(loc))
                continue

            if r.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"URL 取得失敗 (HTTP {r.status_code})",
                )

            cl = r.headers.get("content-length")
            if cl is None:
                raise HTTPException(
                    status_code=400,
                    detail="サーバが Content-Length を返さないため処理できません",
                )
            try:
                total = int(cl)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Content-Length が不正"
                )
            if total <= 0:
                raise HTTPException(
                    status_code=400, detail="Content-Length が 0 以下"
                )
            if total > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "ファイルサイズが上限"
                        f" ({MAX_FILE_SIZE} bytes) を超えています"
                    ),
                )
            return str(r.url)

    raise HTTPException(status_code=502, detail="リダイレクト回数が上限を超えました")


def _url_input_args() -> list[str]:
    """URL を入力にするときの ffmpeg/ffprobe 共通引数。

    - protocol_whitelist: file:// 等の混入を遮断
    - rw_timeout: 読み取り/書き込みタイムアウト (μs)
    """
    return [
        "-protocol_whitelist", "http,https,tcp,tls",
        "-rw_timeout", str(int(URL_FETCH_TIMEOUT * 1_000_000)),
    ]


async def _probe_video(source: str, is_url: bool = False) -> dict:
    """ffprobe で動画のメタデータを取得する。

    Returns:
        {"duration": float, "width": int, "height": int}
    Raises:
        HTTPException: ffprobe の実行失敗時
    """
    args = ["ffprobe"]
    if is_url:
        args += _url_input_args()
    args += [
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        source,
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=400, detail="ffprobe タイムアウト")

    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail="動画として解析できません")

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="ffprobe 出力の解析に失敗")

    duration = None
    fmt = data.get("format", {})
    if "duration" in fmt:
        try:
            duration = float(fmt["duration"])
        except (ValueError, TypeError):
            pass

    width = None
    height = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            width = stream.get("width")
            height = stream.get("height")
            if duration is None and "duration" in stream:
                try:
                    duration = float(stream["duration"])
                except (ValueError, TypeError):
                    pass
            break

    return {
        "duration": duration or 0.0,
        "width": width or 0,
        "height": height or 0,
    }


async def _extract_frame(source: str, seek_sec: float, is_url: bool = False) -> bytes:
    """ffmpeg で指定位置のフレームを PNG として抽出する。

    -ss を -i の前に置くことで HTTP Range シークが効き、URL 入力でも
    必要箇所のみ取得される。

    Returns:
        PNG 画像バイナリ
    Raises:
        HTTPException: フレーム抽出失敗時
    """
    args = ["ffmpeg"]
    if is_url:
        args += _url_input_args()
    args += [
        "-ss", str(seek_sec),
        "-i", source,
        "-frames:v", "1",
        "-f", "image2pipe",
        "-vcodec", "png",
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=502, detail="ffmpeg タイムアウト")

    if proc.returncode != 0 or not stdout:
        raise HTTPException(status_code=502, detail="フレーム抽出に失敗")

    return stdout


def _to_webp(png_data: bytes, max_dim: int, quality: int) -> bytes:
    """PNG → WebP に変換しリサイズする。

    Returns:
        WebP 画像バイナリ
    """
    img = Image.open(io.BytesIO(png_data))
    img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=quality)
    return buf.getvalue()


async def _process_video(
    source: str, max_dim: int, is_url: bool = False
) -> tuple[bytes, dict]:
    """動画ファイル/URL をサムネイル WebP に変換する。

    probe → frame extract → WebP の中核処理を共通化。

    Returns:
        (webp_bytes, meta dict)
    """
    meta = await _probe_video(source, is_url=is_url)
    duration = meta["duration"]

    if duration <= MIN_SEEK_SEC:
        seek = 0.0
    else:
        seek = min(max(duration * SEEK_PERCENT / 100, MIN_SEEK_SEC), MAX_SEEK_SEC)
        seek = min(seek, duration - 0.1)

    png_data = await _extract_frame(source, seek, is_url=is_url)
    webp_data = _to_webp(png_data, max_dim, WEBP_QUALITY)
    return webp_data, meta


def _make_response(webp: bytes, meta: dict) -> Response:
    return Response(
        content=webp,
        media_type="image/webp",
        headers={
            "X-Video-Duration": str(round(meta["duration"], 2)),
            "X-Video-Width": str(meta["width"]),
            "X-Video-Height": str(meta["height"]),
        },
    )


@app.post("/thumbnail")
async def create_thumbnail(
    file: UploadFile,
    max_dimension: Optional[int] = Form(default=None),
) -> Response:
    """動画からサムネイル WebP を生成して返す。

    レスポンスヘッダ:
        Content-Type: image/webp
        X-Video-Duration: 秒数 (float)
        X-Video-Width: 幅 (int)
        X-Video-Height: 高さ (int)
    """
    if not _ffmpeg_ok or not _ffprobe_ok:
        raise HTTPException(status_code=503, detail="FFmpeg が利用できません")

    max_dim = _resolve_max_dim(max_dimension)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".video")
    try:
        size = 0
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=400,
                    detail=f"ファイルサイズが上限 ({MAX_FILE_SIZE} bytes) を超えています",
                )
            tmp.write(chunk)
        tmp.flush()
        tmp_path = tmp.name
        tmp.close()

        if size == 0:
            raise HTTPException(status_code=400, detail="空のファイル")

        webp, meta = await _process_video(tmp_path, max_dim, is_url=False)
        return _make_response(webp, meta)

    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@app.post("/thumbnail_from_url")
async def create_thumbnail_from_url(req: ThumbnailFromUrlRequest) -> Response:
    """URL から動画を取得してサムネイル WebP を生成して返す。

    HEAD でリダイレクトを 200 OK まで追跡し、各ホップで SSRF 検証と
    Content-Length チェックを行う。確定した URL を ffprobe / ffmpeg に
    直接渡し、HTTP Range シークで必要箇所のみ取得する。
    """
    if not _ffmpeg_ok or not _ffprobe_ok:
        raise HTTPException(status_code=503, detail="FFmpeg が利用できません")

    max_dim = _resolve_max_dim(req.max_dimension)
    final_url = await _resolve_final_url(req.url)

    webp, meta = await _process_video(final_url, max_dim, is_url=True)
    return _make_response(webp, meta)


@app.get("/health")
async def health():
    """ヘルスチェック。"""
    return {
        "status": "ok",
        "ffmpeg_available": _ffmpeg_ok,
        "ffprobe_available": _ffprobe_ok,
    }
