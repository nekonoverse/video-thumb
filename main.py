"""動画サムネイル生成マイクロサービス。

FFmpeg でフレーム抽出 → Pillow で WebP リサイズ・エンコード。
nekonoverse の video_thumb_queue.py から POST /thumbnail で呼び出される。

環境変数:
  MAX_DIMENSION: サムネイル最大辺 (default: 800)
  WEBP_QUALITY:  WebP品質 (default: 80)
  SEEK_PERCENT:  フレーム抽出位置 % (default: 10)
  MIN_SEEK_SEC:  最小シーク位置 秒 (default: 1.0)
  MAX_SEEK_SEC:  最大シーク位置 秒 (default: 10.0)
  MAX_FILE_SIZE: 最大ファイルサイズ bytes (default: 500MB)
"""

import asyncio
import io
import json
import logging
import os
import shutil
import tempfile

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image

logger = logging.getLogger("video-thumb")
logging.basicConfig(level=logging.INFO)

MAX_DIMENSION = int(os.environ.get("MAX_DIMENSION", "800"))
WEBP_QUALITY = int(os.environ.get("WEBP_QUALITY", "80"))
SEEK_PERCENT = int(os.environ.get("SEEK_PERCENT", "10"))
MIN_SEEK_SEC = float(os.environ.get("MIN_SEEK_SEC", "1.0"))
MAX_SEEK_SEC = float(os.environ.get("MAX_SEEK_SEC", "10.0"))
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", str(500 * 1024 * 1024)))

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


async def _probe_video(path: str) -> dict:
    """ffprobe で動画のメタデータを取得する。

    Returns:
        {"duration": float, "width": int, "height": int}
    Raises:
        HTTPException: ffprobe の実行失敗時
    """
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=400, detail="ffprobe タイムアウト")

    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail="動画として解析できません")

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="ffprobe 出力の解析に失敗")

    # duration: format.duration または video stream.duration
    duration = None
    fmt = data.get("format", {})
    if "duration" in fmt:
        try:
            duration = float(fmt["duration"])
        except (ValueError, TypeError):
            pass

    # video stream から width, height, (fallback duration)
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


async def _extract_frame(path: str, seek_sec: float) -> bytes:
    """ffmpeg で指定位置のフレームを PNG として抽出する。

    Returns:
        PNG 画像バイナリ
    Raises:
        HTTPException: フレーム抽出失敗時
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-ss", str(seek_sec),
        "-i", path,
        "-frames:v", "1",
        "-f", "image2pipe",
        "-vcodec", "png",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
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


@app.post("/thumbnail")
async def create_thumbnail(file: UploadFile) -> Response:
    """動画からサムネイル WebP を生成して返す。

    レスポンスヘッダ:
        Content-Type: image/webp
        X-Video-Duration: 秒数 (float)
        X-Video-Width: 幅 (int)
        X-Video-Height: 高さ (int)
    """
    if not _ffmpeg_ok or not _ffprobe_ok:
        raise HTTPException(status_code=503, detail="FFmpeg が利用できません")

    # 一時ファイルに書き出し
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".video")
    try:
        size = 0
        while True:
            chunk = await file.read(1024 * 1024)  # 1MB ずつ
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

        # メタデータ取得
        meta = await _probe_video(tmp_path)
        duration = meta["duration"]
        width = meta["width"]
        height = meta["height"]

        # シーク位置計算
        if duration <= MIN_SEEK_SEC:
            seek = 0.0
        else:
            seek = min(max(duration * SEEK_PERCENT / 100, MIN_SEEK_SEC), MAX_SEEK_SEC)
            # duration より手前であることを保証
            seek = min(seek, duration - 0.1)

        # フレーム抽出
        png_data = await _extract_frame(tmp_path, seek)

        # WebP 変換
        webp_data = _to_webp(png_data, MAX_DIMENSION, WEBP_QUALITY)

        return Response(
            content=webp_data,
            media_type="image/webp",
            headers={
                "X-Video-Duration": str(round(duration, 2)),
                "X-Video-Width": str(width),
                "X-Video-Height": str(height),
            },
        )

    finally:
        # 一時ファイル削除
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@app.get("/health")
async def health():
    """ヘルスチェック。"""
    return {
        "status": "ok",
        "ffmpeg_available": _ffmpeg_ok,
        "ffprobe_available": _ffprobe_ok,
    }
