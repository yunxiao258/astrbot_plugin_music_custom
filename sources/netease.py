"""网易云音乐源（青听 music.json 中的 wy 子源）"""

import asyncio
import re

from .base import MusicItem, MusicSource

SEARCH_URL = "https://music.163.com/api/search/get/web"
DETAIL_URL = "https://music.163.com/api/song/detail"
LYRIC_URL = "https://music.163.com/api/song/lyric"
OUTER_URL = "http://music.163.com/song/media/outer/url?id={}.mp3"
HOT_URL = "https://music.163.com/api/playlist/detail?id=3778678"

# 分享链接：music.163.com/song?id=xxx / #/song?id=xxx / outer/url?id=xxx
SHARE_RE = re.compile(r"music\.163\.com/(?:#/)?song(?:\?.*?&?id=(\d+)|/media/outer/url\?.*?id=(\d+))", re.I)


def _parse_song(s: dict) -> MusicItem:
    """从网易云歌曲 JSON 构造 MusicItem"""
    artists = ", ".join(a.get("name", "") for a in s.get("artists") or [])
    return MusicItem(
        source="netease",
        id=str(s.get("id", "")),
        title=s.get("name", ""),
        artist=artists,
        album=(s.get("album") or {}).get("name", ""),
        duration=int(s.get("duration", 0)) // 1000,
        artwork=(s.get("album") or {}).get("picUrl", "") or "",
    )


class NeteaseSource(MusicSource):
    """网易云：搜索接口 + outer 直链播放（免登录）"""

    name = "netease"
    display_name = "网易云"

    async def search(self, keyword: str, limit: int = 5) -> list[MusicItem]:
        def _do():
            r = self.session.get(
                SEARCH_URL,
                params={"s": keyword, "type": 1, "limit": limit, "offset": 0},
                timeout=12,
            )
            return r.json()

        j = await asyncio.to_thread(_do)
        songs = (j.get("result") or {}).get("songs") or []
        return [_parse_song(s) for s in songs]

    async def search_by_lyric(self, keyword: str, limit: int = 5) -> list[MusicItem]:
        """按歌词片段搜索（网易云 type=1006）"""
        def _do():
            r = self.session.get(
                SEARCH_URL,
                params={"s": keyword, "type": 1006, "limit": limit, "offset": 0},
                timeout=12,
            )
            return r.json()

        j = await asyncio.to_thread(_do)
        songs = (j.get("result") or {}).get("songs") or []
        return [_parse_song(s) for s in songs]

    async def get_lyric(self, item: MusicItem, max_lines: int = 4) -> str:
        """获取逐字歌词，截取前若干有效行返回纯文本；失败返回空串"""
        def _do():
            r = self.session.get(
                LYRIC_URL,
                params={"id": item.id, "lv": 1, "kv": 1, "tv": -1},
                timeout=12,
            )
            return r.json()

        try:
            j = await asyncio.to_thread(_do)
        except Exception:
            return ""
        lrc = (j.get("lrc") or {}).get("lyric") or ""
        # 元数据前缀（作词/作曲/编曲 等）直接跳过
        meta_prefix = re.compile(
            r"^(作词|作曲|编曲|制作人|制作|混音|混音师|监制|和声|录音|录音室|母带|母带工程|原唱|OP|SP|词|曲|封面|企划|发行|出品)[：:：]?\s*"
        )
        lines = []
        for line in lrc.splitlines():
            # 去掉时间轴标签 [xx:xx.xx] 及纯元数据行 [ar:xx]
            m = re.search(r"\]\s*(.+)", line)
            text = m.group(1).strip() if m else line.strip()
            if not text or (text.startswith("[") and ":" in text):
                continue
            if meta_prefix.match(text):
                continue
            lines.append(text)
            if len(lines) >= max_lines:
                break
        return "\n".join(lines)

    async def get_media_url(self, item: MusicItem, quality: str = "standard") -> str:
        """网易云 outer 直链：无效 ID 会返回 404，调用方需校验"""
        return OUTER_URL.format(item.id)

    async def get_hot(self, limit: int = 5) -> list[MusicItem]:
        def _do():
            r = self.session.get(HOT_URL, params={"id": "3778678", "limit": 10}, timeout=12)
            return r.json()

        j = await asyncio.to_thread(_do)
        tracks = ((j.get("result") or {}).get("tracks")) or []
        items = []
        for s in tracks[:limit]:
            artists = ", ".join(a.get("name", "") for a in s.get("artists") or [])
            items.append(
                MusicItem(
                    source=self.name,
                    id=str(s.get("id", "")),
                    title=s.get("name", ""),
                    artist=artists,
                    album=(s.get("album") or {}).get("name", ""),
                    duration=int(s.get("duration", 0)) // 1000,
                    artwork=(s.get("album") or {}).get("picUrl", "") or "",
                )
            )
        return items

    async def parse_share(self, text: str) -> MusicItem | None:
        """解析网易云分享链接 → 歌曲"""
        m = SHARE_RE.search(text)
        if not m:
            return None
        song_id = m.group(1) or m.group(2)
        if not song_id:
            return None

        def _do():
            r = self.session.get(DETAIL_URL, params={"id": song_id, "ids": f"[{song_id}]"}, timeout=12)
            return r.json()

        try:
            j = await asyncio.to_thread(_do)
        except Exception:
            return None
        songs = j.get("songs") or []
        if not songs:
            return None
        s = songs[0]
        artists = ", ".join(a.get("name", "") for a in s.get("artists") or [])
        return MusicItem(
            source=self.name,
            id=str(s.get("id", song_id)),
            title=s.get("name", "网易云歌曲"),
            artist=artists,
            album=(s.get("album") or {}).get("name", ""),
            duration=int(s.get("duration", 0)) // 1000,
            artwork=(s.get("album") or {}).get("picUrl", "") or "",
        )
