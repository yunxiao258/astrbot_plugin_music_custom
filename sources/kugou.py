"""酷狗音乐源（青听 music.json 中的 kg 子源）：搜索可用，播放接口易风控"""

import asyncio

from ..log import get_logger
from .base import MusicItem, MusicSource

logger = get_logger()

SEARCH_URL = "https://songsearch.kugou.com/song_search_v2"
PLAY_URL = "https://wwwapi.kugou.com/yy/index.php"


class KugouSource(MusicSource):
    """酷狗：song_search_v2 搜索。播放接口受风控，失败时由调用方换源"""

    name = "kugou"
    display_name = "酷狗"

    def _headers(self) -> dict:
        """酷狗接口请求头（Referer 必需）"""
        return {
            "Referer": "https://www.kugou.com/",
            "Accept": "application/json, text/plain, */*",
        }

    async def search(self, keyword: str, limit: int = 5) -> list[MusicItem]:
        def _do():
            r = self.session.get(
                SEARCH_URL,
                params={"keyword": keyword, "page": 1, "pagesize": limit, "platform": "WebFilter"},
                headers=self._headers(),
                timeout=12,
            )
            return r.json()

        try:
            j = await asyncio.to_thread(_do)
        except Exception as e:
            logger.warning(f"[kugou] 搜索「{keyword}」请求异常: {e}")
            return []
        lists = (j.get("data") or {}).get("lists") or []
        if not lists:
            logger.warning(f"[kugou] 搜索「{keyword}」无结果, status={j.get('status')}")
        items = []
        for s in lists:
            fhash = s.get("FileHash", "")
            if not fhash:
                continue
            items.append(
                MusicItem(
                    source=self.name,
                    id=fhash,
                    title=s.get("SongName", ""),
                    artist=s.get("SingerName", ""),
                    album=s.get("AlbumName", ""),
                    duration=int(s.get("Duration", 0) or 0),
                    artwork=s.get("ImgUrl", "") or "",
                    extra={"album_id": s.get("AlbumID", "")},
                )
            )
        return items[:limit]

    async def get_media_url(self, item: MusicItem, quality: str = "standard") -> str:
        """v2 play/getdata 接口；dfid 缺失时先访问主页获取"""
        def _do():
            try:
                self.session.get("https://www.kugou.com/", timeout=8)
            except Exception:
                pass
            dfid = self.session.cookies.get("kg_dfid") or self.session.cookies.get("dfid") or "1Q2W3E4R5T6"
            r = self.session.get(
                PLAY_URL,
                params={
                    "r": "play/getdata", "hash": item.id,
                    "album_id": item.extra.get("album_id", ""),
                    "dfid": dfid, "mid": "1234567890", "platid": 4, "appid": 1014,
                },
                headers=self._headers(),
                timeout=12,
            )
            return r.json()

        try:
            j = await asyncio.to_thread(_do)
            data = j.get("data") or {}
            url = data.get("play_url") or data.get("url") or ""
            if url.startswith("http"):
                return url
            logger.warning(f"[kugou] 播放地址获取失败（可能被风控）: {item.title}, status={j.get('status', '')}")
        except Exception as e:
            logger.warning(f"[kugou] 播放地址获取异常: {e}")
        return ""