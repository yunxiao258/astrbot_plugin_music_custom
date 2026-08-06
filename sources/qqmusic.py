"""QQ音乐源（青听 music.json 中的 tx 子源）：搜索 + QQ 音乐卡片"""

import asyncio
import json
import re
import time

from ..log import get_logger
from .base import MusicItem, MusicSource

logger = get_logger()

SEARCH_URL = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
VKEY_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
DETAIL_URL = "https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg"

# 分享链接：y.qq.com/n/ryqq/songDetail/xxx / song.html?songmid=xxx / songmid=xxx
SHARE_RE = re.compile(r"songDetail/([A-Za-z0-9]+)|songmid=([A-Za-z0-9]+)", re.I)


class QQMusicSource(MusicSource):
    """QQ音乐：client_search_cp 搜索。语音直链需 vkey（易风控），主要用于 QQ 音乐卡片"""

    name = "qqmusic"
    display_name = "QQ音乐"

    @property
    def supports_card(self) -> bool:
        return True

    def _headers(self) -> dict:
        """QQ 音乐接口请求头（带 Referer + 基础 Cookie，降低风控概率）"""
        return {
            "Referer": "https://y.qq.com/",
            "Origin": "https://y.qq.com",
            "Cookie": "pgv_pvid=0000000000000000; pgv_info=ssid=s0000000000; ts_uid=0;",
            "Accept-Charset": "utf-8",
        }

    async def search(self, keyword: str, limit: int = 5) -> list[MusicItem]:
        def _do():
            r = self.session.get(
                SEARCH_URL,
                params={"p": 1, "n": limit, "w": keyword, "format": "json", "cr": 1},
                headers=self._headers(),
                timeout=12,
            )
            return r.json()

        try:
            j = await asyncio.to_thread(_do)
        except Exception as e:
            logger.warning(f"[qqmusic] 搜索「{keyword}」请求异常: {e}")
            return []
        songs = ((j.get("data") or {}).get("song") or {}).get("list") or []
        if not songs:
            logger.warning(
                f"[qqmusic] 搜索「{keyword}」无结果, code={j.get('code')}, msg={j.get('msg', '')}"
            )
        items = []
        for s in songs:
            mid = s.get("songmid", "")
            if not mid:
                continue
            singers = ", ".join(x.get("name", "") for x in s.get("singer") or [])
            items.append(
                MusicItem(
                    source=self.name,
                    id=mid,
                    title=s.get("songname", ""),
                    artist=singers,
                    album=s.get("albumname", ""),
                    duration=int(s.get("interval", 0) or 0),
                    artwork=(s.get("albummid") and f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{s.get('albummid')}.jpg") or "",
                    url=f"https://i.y.qq.com/n2/m/share/details/song.html?songid={s.get('songid', '')}",
                    extra={"songid": int(s.get("songid") or 0) or 0},
                )
            )
        return items[:limit]

    async def get_media_url(self, item: MusicItem, quality: str = "standard") -> str:
        """尝试获取 vkey 直链（可能被风控，失败返回空，调用方回退卡片/换源）"""
        def _do():
            guid = str(int(time.time() * 1000))[-10:]
            data = json.dumps({
                "req_0": {
                    "module": "vkey.GetVkeyServer", "method": "CgiGetVkey",
                    "param": {"guid": guid, "songmid": [item.id], "songtype": [0],
                             "uin": "0", "loginflag": 1, "platform": "20"},
                },
                "comm": {"uin": 0, "format": "json", "ct": 24, "cv": 0},
            })
            r = self.session.get(VKEY_URL, params={"format": "json", "data": data}, headers=self._headers(), timeout=12)
            return r.json()

        try:
            j = await asyncio.to_thread(_do)
            urls = ((j.get("req_0") or {}).get("data") or {}).get("midurlinfo") or []
            purl = urls[0].get("purl", "") if urls else ""
            if purl:
                return "https://dl.stream.qqmusic.qq.com/" + purl
            logger.warning(f"[qqmusic] 获取 vkey 直链失败（可能被风控）: {item.title}")
        except Exception as e:
            logger.warning(f"[qqmusic] 获取 vkey 直链异常: {e}")
        return ""

    def get_card(self, item: MusicItem) -> dict:
        """构造 Music 组件参数（QQ 音乐卡片，id 用数字 songid）"""
        return {
            "_type": "qq",
            "id": item.extra.get("songid") or 0,
            "title": item.title,
            "content": item.artist,
            "url": item.url or f"https://i.y.qq.com/n2/m/share/details/song.html?songmid={item.id}",
            "image": item.artwork,
        }

    async def parse_share(self, text: str) -> MusicItem | None:
        """解析 QQ 音乐分享链接 → 歌曲"""
        m = SHARE_RE.search(text)
        if not m:
            return None
        mid = m.group(1) or m.group(2)
        if not mid:
            return None

        def _do():
            r = self.session.get(
                DETAIL_URL,
                params={"songmid": mid, "format": "json", "inCharset": "utf8", "outCharset": "utf8"},
                headers=self._headers(),
                timeout=12,
            )
            return r.json()

        try:
            j = await asyncio.to_thread(_do)
        except Exception as e:
            logger.warning(f"[qqmusic] 解析链接失败 {mid}: {e}")
            return None
        song = ((j.get("data") or [None]))[0] if j.get("data") else None
        if not song:
            return None
        singers = ", ".join(x.get("name", "") for x in song.get("singer") or [])
        album = (song.get("album") or {})
        albummid = album.get("mid", "") or ""
        return MusicItem(
            source=self.name,
            id=mid,
            title=song.get("name") or song.get("title") or "QQ音乐歌曲",
            artist=singers,
            album=album.get("name", ""),
            duration=int(song.get("interval", 0) or 0),
            artwork=(albummid and f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{albummid}.jpg") or "",
            url=f"https://i.y.qq.com/n2/m/share/details/song.html?songid={song.get('id', '')}",
            extra={"songid": int(song.get("id") or 0) or 0},
        )