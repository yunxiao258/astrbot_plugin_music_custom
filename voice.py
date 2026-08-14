"""语音消息发送：URL 直发 → base64 → amr 转码兜底"""

import asyncio
import os
import shutil
import subprocess
import tempfile
import time

FFMPEG = None
# 探测失败冷却：避免高频转换时反复等待超时子进程
_LAST_FAIL_TS = 0.0
_RETRY_INTERVAL = 600.0


def _find_ffmpeg() -> str | None:
    """查找 ffmpeg：优先环境变量，其次 PATH/常见安装路径；失败后冷却 10 分钟再试"""
    global FFMPEG, _LAST_FAIL_TS
    if FFMPEG is not None:
        return FFMPEG
    if time.monotonic() - _LAST_FAIL_TS < _RETRY_INTERVAL:
        return None
    candidates = [
        os.environ.get("FFMPEG_PATH", ""),
        shutil.which("ffmpeg") or "",
        "ffmpeg",
    ]
    # 常见安装路径（不再写死用户目录）
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), r"C:\ffmpeg"):
        for sub in (r"ffmpeg\bin\ffmpeg.exe", r"bin\ffmpeg.exe"):
            candidates.append(os.path.join(base, sub))
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
    _LAST_FAIL_TS = time.monotonic()
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
