"""插件专属日志：独立滚动日志文件（plugin_data/astrbot_plugin_music_custom/logs/）+ 同步转发 AstrBot 主日志"""

import logging
import os
from logging.handlers import RotatingFileHandler

_PLUGIN_DATA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugin_data",
    "astrbot_plugin_music_custom",
)
_LOG_DIR = os.path.join(_PLUGIN_DATA, "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "music.log")

_LOG_NAME = "astrbot_plugin_music_custom"

_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    """获取插件专属 logger（首次调用时创建文件 handler + AstrBot 桥接 handler）"""
    global _logger
    if _logger is not None:
        return _logger

    os.makedirs(_LOG_DIR, exist_ok=True)

    _logger = logging.getLogger(_LOG_NAME)
    _logger.setLevel(logging.DEBUG)
    _logger.propagate = False

    if not _logger.handlers:
        # 专属文件 handler（2MB 滚动 × 3 份，UTF-8）
        try:
            fh = RotatingFileHandler(
                _LOG_FILE,
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            fh.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            _logger.addHandler(fh)
        except Exception:
            pass

        # 桥接：转发到 AstrBot 主日志，WebUI 中也能看到
        try:
            from astrbot.api import logger as astrbot_logger

            class _Bridge(logging.Handler):
                def emit(self, record):
                    try:
                        msg = record.getMessage()
                        level = record.levelname.lower()
                        handler = getattr(astrbot_logger, level, None)
                        (handler or astrbot_logger.info)(msg)
                    except Exception:
                        pass

            bh = _Bridge()
            bh.setFormatter(logging.Formatter("%(message)s"))
            _logger.addHandler(bh)
        except Exception:
            pass

    return _logger


def get_log_path() -> str:
    """插件专属日志文件路径"""
    return _LOG_FILE