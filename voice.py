"""语音消息发送：URL 直发 → base64 → amr 转码兜底"""

import asyncio
import os
import subprocess
import tempfile

FFMPEG = None


def _find_ffmpeg() -> str | None:
    """查找 ffmpeg：优先环境变量，其次常见路径"""
    global FFMPEG
    if FFMPEG is not None:
        return FFMPEG
    candidates = [
        os.environ.get("FFMPEG_PATH", ""),
        "ffmpeg",
        r"C:\Users\Administrator\ffmpeg\ffmpeg.exe",
    ]
    for c in candidates:
        if not c:
            continue
        try:
            r = subprocess.run([c, "-version"], capture_output=True, timeout=8)
            if r.returncode == 0:
                FFMPEG = c
                return c
        except Exception:
            continue
    FFMPEG = ""
    return None


async def convert_to_amr(src: str, out_path: str) -> bool:
    """用 ffmpeg 将音频转换为 QQ 可播放的 AMR-NB 12.2kbps"""
    ff = _find_ffmpeg()
    if not ff:
        return False

    def _do():
        try:
            r = subprocess.run(
                [ff, "-y", "-i", src, "-ar", "8000", "-ac", "1", "-b:a", "12.2k", out_path],
                capture_output=True, timeout=120,
            )
            return r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 100
        except Exception:
            return False

    return await asyncio.to_thread(_do)


async def download_audio(url: str, out_path: str, timeout: int = 60) -> bool:
    """下载音频到本地文件"""
    import requests

    def _do():
        try:
            r = requests.get(url, stream=True, timeout=timeout, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.bilibili.com/",
            })
            if r.status_code != 200:
                return False
            total = 0
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    f.write(chunk)
                    total += len(chunk)
                    if total > 50 * 1024 * 1024:
                        r.close()
                        return False
            return total > 1000
        except Exception:
            return False

    return await asyncio.to_thread(_do)
