"""音乐源注册表与轮询调度"""

import requests

try:
    from ..log import get_logger
    from .base import MusicSource
    from .bilibili import BilibiliSource
    from .kugou import KugouSource
    from .kuwo import KuwoSource
    from .netease import NeteaseSource
    from .qqmusic import QQMusicSource
except ImportError:  # unittest discover 顶层导入本包时无父包上下文
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from log import get_logger
    from base import MusicSource
    from bilibili import BilibiliSource
    from kugou import KugouSource
    from kuwo import KuwoSource
    from netease import NeteaseSource
    from qqmusic import QQMusicSource

logger = get_logger()

ALL_SOURCES: dict[str, type[MusicSource]] = {
    NeteaseSource.name: NeteaseSource,
    KuwoSource.name: KuwoSource,
    KugouSource.name: KugouSource,
    QQMusicSource.name: QQMusicSource,
    BilibiliSource.name: BilibiliSource,
}

# 默认启用顺序：稳定语音源优先，QQ 音乐用于卡片
DEFAULT_ORDER = ["netease", "kuwo", "kugou", "qqmusic", "bilibili"]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


class SourceManager:
    """管理多个音乐源，按配置顺序轮流尝试"""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        self._apply_proxy()
        self._sources: dict[str, MusicSource] = {}
        self._order: list[str] = []
        self.reload()

    def _apply_proxy(self) -> None:
        """应用配置中的 HTTP 代理（用于绕过音乐平台对服务器 IP 的风控）"""
        proxy = str(self.config.get("proxy", "") or "").strip()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
            logger.info(f"音乐插件已启用代理: {proxy}")
        else:
            self.session.proxies = {}
            logger.debug("音乐插件未配置代理")

    def reload(self) -> None:
        """按配置重建源实例（支持热重载）"""
        enabled = self.config.get("sources", "") or ""
        order = [x.strip() for x in str(enabled).split(",") if x.strip() in ALL_SOURCES]
        if not order:
            order = list(DEFAULT_ORDER)
        self._order = order
        self._apply_proxy()
        self._sources = {name: ALL_SOURCES[name](self.session, config=self.config) for name in order}
        logger.debug(f"音乐源已加载: {self._order}")

    @property
    def order(self) -> list[str]:
        return list(self._order)

    def get(self, name: str) -> MusicSource | None:
        return self._sources.get(name)

    async def search_all(self, keyword: str, limit: int = 5) -> list:
        """按顺序在启用源中搜索，收集所有结果（最多 limit 条/源）"""
        results = []
        for name in self._order:
            src = self._sources.get(name)
            if not src:
                continue
            try:
                items = await src.search(keyword, limit=limit)
                if items:
                    results.append((name, items))
                    logger.debug(f"[music] 源 {name} 搜索「{keyword}」成功，返回 {len(items)} 条")
                else:
                    logger.warning(f"[music] 源 {name} 搜索「{keyword}」无结果（可能被风控）")
            except Exception as e:
                logger.warning(f"[music] 源 {name} 搜索「{keyword}」异常: {e}")
                continue
        return results

    async def first_media_url(self, item, quality: str = "standard") -> str:
        """获取歌曲播放直链（仅使用其自身源，quality: standard/high/low）"""
        src = self._sources.get(item.source)
        if not src:
            logger.warning(f"[music] 源 {item.source} 未启用，无法获取播放地址")
            return ""
        try:
            url = await src.get_media_url(item, quality=quality)
            if url:
                logger.debug(
                    f"[music] 源 {item.source} 获取播放地址成功: {item.title} → {url[:80]}"
                )
            else:
                logger.warning(
                    f"[music] 源 {item.source} 未获取到播放地址（可能被风控）: {item.title} - {item.artist}"
                )
            return url
        except Exception as e:
            logger.warning(f"[music] 源 {item.source} 获取播放地址异常: {e}")
            return ""

    async def search_by_lyric(self, keyword: str, limit: int = 5) -> list:
        """按歌词片段搜索：仅网易云支持，其他源跳过"""
        src = self._sources.get("netease")
        if not src or not hasattr(src, "search_by_lyric"):
            return []
        try:
            items = await src.search_by_lyric(keyword, limit=limit)
            logger.debug(f"[music] 歌词搜索「{keyword}」返回 {len(items)} 条")
            return items
        except Exception as e:
            logger.warning(f"[music] 歌词搜索「{keyword}」异常: {e}")
            return []

    async def get_lyric(self, item, max_lines: int = 4) -> str:
        """获取歌词文本（仅网易云支持）"""
        src = self._sources.get("netease")
        if not src or not hasattr(src, "get_lyric"):
            return ""
        try:
            lyric = await src.get_lyric(item, max_lines=max_lines)
            if not lyric:
                logger.debug(f"[music] 歌词获取为空: {item.title}")
            return lyric
        except Exception as e:
            logger.warning(f"[music] 歌词获取异常: {e}")
            return ""

    async def parse_share(self, text: str):
        """解析分享链接，按源顺序返回第一个可解析的 MusicItem"""
        for name in self._order:
            src = self._sources.get(name)
            if not src or not hasattr(src, "parse_share"):
                continue
            try:
                item = await src.parse_share(text)
                if item is not None:
                    logger.info(f"[music] 链接解析成功，源 {name}: {item.title}")
                    return item
            except Exception as e:
                logger.warning(f"[music] 源 {name} 解析链接异常: {e}")
                continue
        logger.warning(f"[music] 链接解析失败: {text[:80]}")
        return None
