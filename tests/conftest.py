"""pytest 共通 fixture。"""

import io
import os
import shutil
import subprocess
import tempfile

import pytest


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


HAS_FFMPEG = _have("ffmpeg") and _have("ffprobe")


@pytest.fixture(scope="session")
def sample_mp4() -> str:
    """ffmpeg testsrc で生成した小さな mp4 のパスを返す。"""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg/ffprobe が無いためスキップ")
    f = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    f.close()
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f", "lavfi",
            "-i", "testsrc=duration=2:size=320x240:rate=10",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            f.name,
        ],
        check=True,
        capture_output=True,
    )
    yield f.name
    try:
        os.unlink(f.name)
    except OSError:
        pass


@pytest.fixture
def sample_mp4_bytes(sample_mp4: str) -> bytes:
    with open(sample_mp4, "rb") as fh:
        return fh.read()
