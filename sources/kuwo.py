"""酷我音乐源（青听 music.json 中的 kw 子源）"""

import ast
import asyncio
import json

from .base import MusicItem, MusicSource

SEARCH_URL = "http://search.kuwo.cn/r.s"
PLAY_URL = "http://antiserver.kuwo.cn/anti.s"
RANK_URL = "http://search.kuwo.cn/r.s"
RANK_IDS = {
    "飙升榜": "MUSIC_TOP_100",      # 飙升榜
    "热歌榜": "MUSIC_TOP_50",       # 热歌榜
    "新歌榜": "MUSIC_TOP_20",       # 新歌榜
}


class KuwoSource(MusicSource):
    """酷我：search.kuwo.cn 搜索（返回单引号 JSON，需 ast 解析）+ antiserver 直链"""

    name = "kuwo"
    display_name = "酷我"

    def _parse(self, text: str) -> dict:
        """酷我接口返回 Python 风格单引号 JSON，用 ast 安全解析"""
        return ast.literal_eval(text.strip())

    async def search(self, keyword: str, limit: int = 5) -> list[MusicItem]:
        def _do():
            r = self.session.get(
                SEARCH_URL,
                params={
                    "all": keyword, "ft": "music", "itemset": "web_2013",
                    "client": "kt", "pn": 0, "rn": limit, "rformat": "json", "encoding": "utf8",
                },
                timeout=12,
            )
            return r.text

        try:
            txt = await asyncio.to_thread(_do)
            j = self._parse(txt)
        except Exception:
            return []
        lists = j.get("abslist") or []
        items = []
        for s in lists:
            rid = s.get("MUSICRID", "").replace("MUSIC_", "")
            if not rid:
                continue
            items.append(
                MusicItem(
                    source=self.name,
                    id=rid,
                    title=(s.get("SONGNAME") or "").replace("&nbsp;", " "),
                    artist=(s.get("ARTIST") or "").replace("&nbsp;", " "),
                    album=(s.get("ALBUM") or "").replace("&nbsp;", " "),
                    duration=int(s.get("DURATION", 0) or 0),
                    artwork=s.get("ARTISTPIC") or "",
                )
            )
        return items[:limit]

    async def get_media_url(self, item: MusicItem, quality: str = "standard") -> str:
        """antiserver 转换接口：返回 JSON 或纯 URL"""
        def _do():
            r = self.session.get(
                PLAY_URL,
                params={"type": "convert_url3", "rid": f"MUSIC_{item.id}", "format": "mp3|aac", "response": "url"},
                timeout=15,
            )
            return r.text.strip()

        try:
            txt = await asyncio.to_thread(_do)
            if txt.startswith("{"):
                url = json.loads(txt).get("url", "")
            else:
                url = txt
            if url.startswith("http"):
                return url
        except Exception:
            pass
        return ""

    async def get_hot(self, limit: int = 5) -> list[MusicItem]:
        """热歌榜（ft=music 榜单模式）"""
        def _do():
            r = self.session.get(
                RANK_URL,
                params={
                    "all": "", "ft": "music", "itemset": "web_2013",
                    "client": "kt", "pn": 0, "rn": limit, "rformat": "json", "encoding": "utf8",
                    "rank": "热歌榜",
                },
                timeout=12,
            )
            return r.text

        try:
            txt = await asyncio.to_thread(_do)
            j = self._parse(txt)
        except Exception:
            return []
        lists = j.get("abslist") or []
        items = []
        for s in lists:
            rid = s.get("MUSICRID", "").replace("MUSIC_", "")
            if not rid:
                continue
            items.append(
                MusicItem(
                    source=self.name,
                    id=rid,
                    title=(s.get("SONGNAME") or "").replace("&nbsp;", " "),
                    artist=(s.get("ARTIST") or "").replace("&nbsp;", " "),
                    duration=int(s.get("DURATION", 0) or 0),
                    artwork=s.get("ARTISTPIC") or "",
                )
            )
        return items[:limit]
