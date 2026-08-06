"""网易云音乐源（青听 music.json 中的 wy 子源）"""

import asyncio
import re

from ..log import get_logger
from .base import MusicItem, MusicSource

logger = get_logger()

SEARCH_URL = "https://music.163.com/api/search/get/web"
DETAIL_URL = "https://music.163.com/api/song/detail"
LYRIC_URL = "https://music.163.com/api/song/lyric"
PLAY_URL = "https://music.163.com/api/song/enhance/player/url"
HOT_URL = "https://music.163.com/api/playlist/detail"

# 热门歌单 ID（云音乐飙升榜/热歌榜等），用于 get_hot
HOT_PLAYLIST = "3778678"

# 分享链接：music.163.com/song?id=xxx / #/song?id=xxx / outer/url?id=xxx
SHARE_RE = re.compile(r"music\.163\.com/(?:#/)?song(?:\?.*?&?id=(\d+)|/media/outer/url\?.*?id=(\d+))", re.I)


def _parse_song(s: dict) -> MusicItem:
    """从网易云歌曲 JSON 构造 MusicItem"""
    if not isinstance(s, dict):
        return MusicItem(source="netease", id="", title="", artist="")
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
    """网易云：明文搜索接口 + enhance/player/url 播放直链（配置登录 Cookie 可解锁 VIP）"""

    name = "netease"
    display_name = "网易云"

    def _headers(self) -> dict:
        """网易云接口请求头：合并配置中的登录 Cookie（MUSIC_U）以解锁 VIP 直链"""
        cookie = str(self.config.get("netease_cookie", "") or "").strip()
        base = "os=pc; appver=2.2.16; NMTID=00000000000000000000000000000000"
        if cookie:
            base += "; " + cookie.strip().rstrip(";")
        return {
            "Referer": "https://music.163.com/",
            "Cookie": base,
            "X-Real-IP": "",
        }

    def _sync_search(self, url: str, params: dict, extra_headers: dict | None = None) -> dict:
        headers = {**self._headers(), **(extra_headers or {})}
        r = self.session.get(url, params=params, headers=headers, timeout=12)
        # 请求失败/被风控时打日志
        if r.status_code != 200:
            logger.warning(f"[netease] 搜索 HTTP {r.status_code}: {url}")
            return {}
        try:
            data = r.json()
            # 网易云风控时可能返回 JSON 字符串（如 "请求过于频繁"），需防御
            if not isinstance(data, dict):
                logger.warning(f"[netease] 搜索响应非 JSON 对象: {type(data).__name__}, {str(data)[:80]}")
                return {}
            return data
        except Exception as e:
            logger.warning(f"[netease] 搜索响应解析失败: {e}\n{r.text[:200]}")
            return {}

    async def search(self, keyword: str, limit: int = 5) -> list[MusicItem]:
        def _do():
            return self._sync_search(
                SEARCH_URL,
                {"s": keyword, "type": 1, "limit": limit, "offset": 0},
            )

        j = await asyncio.to_thread(_do)
        if not isinstance(j, dict):
            return []
        songs = (j.get("result") or {}).get("songs") or []
        if not songs:
            # 记录 code / 空结果原因，方便排查（-460 表示被风控 / 需要验证）
            logger.warning(
                f"[netease] 搜索「{keyword}」无结果或为空, code={j.get('code')}, 响应原因={j.get('message', '')}"
            )
        return [_parse_song(s) for s in songs]

    async def search_by_lyric(self, keyword: str, limit: int = 5) -> list[MusicItem]:
        """按歌词片段搜索（网易云 type=1006）"""
        def _do():
            return self._sync_search(
                SEARCH_URL,
                {"s": keyword, "type": 1006, "limit": limit, "offset": 0},
            )

        j = await asyncio.to_thread(_do)
        if not isinstance(j, dict):
            return []
        songs = (j.get("result") or {}).get("songs") or []
        if not songs:
            logger.warning(
                f"[netease] 歌词搜索「{keyword}」无结果, code={j.get('code')}, message={j.get('message', '')}"
            )
        return [_parse_song(s) for s in songs]

    async def get_lyric(self, item: MusicItem, max_lines: int = 4) -> str:
        """获取逐字歌词，截取前若干有效行返回纯文本；失败返回空串"""
        def _do():
            r = self.session.get(
                LYRIC_URL,
                params={"id": item.id, "lv": 1, "kv": 1, "tv": -1},
                headers=self._headers(),
                timeout=12,
            )
            return r.json()

        try:
            j = await asyncio.to_thread(_do)
        except Exception as e:
            logger.warning(f"[netease] 歌词获取异常 {item.id}: {e}")
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
        """网易云 enhance/player/url 明文接口：免费歌曲直链；配置登录 Cookie 后 VIP 也可获取"""
        br_map = {"super": 320000, "high": 192000, "low": 128000}
        br = br_map.get(quality, 128000)

        def _do():
            r = self.session.get(
                PLAY_URL,
                params={"ids": f"[{item.id}]", "br": br},
                headers=self._headers(),
                timeout=12,
            )
            return r.json()

        try:
            j = await asyncio.to_thread(_do)
        except Exception as e:
            logger.warning(f"[netease] 获取播放地址异常 {item.id}: {e}")
            return ""
        d = j.get("data") if isinstance(j, dict) else None
        if not isinstance(d, list) or not d:
            logger.warning(f"[netease] 播放地址接口无数据: {item.id}, code={j.get('code') if isinstance(j, dict) else '?'}")
            return ""
        first = d[0] if isinstance(d[0], dict) else {}
        url = first.get("url") or ""
        if url.startswith("http"):
            return url
        # 无版权/未登录拿不到直链，返回空由调用方回退卡片/换源
        logger.warning(
            f"[netease] 无可用播放地址: {item.title} - {item.artist}, code={first.get('code')}"
            f"{'' if self.config.get('netease_cookie') else '（未配置登录 Cookie，VIP 歌曲可能无法获取）'}"
        )
        return ""

    async def get_hot(self, limit: int = 5) -> list[MusicItem]:
        def _do():
            r = self.session.get(
                HOT_URL,
                params={"id": HOT_PLAYLIST, "limit": 10},
                headers=self._headers(),
                timeout=12,
            )
            return r.json()

        j = await asyncio.to_thread(_do)
        tracks = ((j.get("result") or {}).get("tracks")) or []
        if not tracks:
            logger.warning(
                f"[netease] 热门榜单获取为空, code={j.get('code')}, message={j.get('message', '')}"
            )
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
            r = self.session.get(
                DETAIL_URL,
                params={"id": song_id, "ids": f"[{song_id}]"},
                headers=self._headers(),
                timeout=12,
            )
            return r.json()

        try:
            j = await asyncio.to_thread(_do)
        except Exception as e:
            logger.warning(f"[netease] 解析链接失败 {song_id}: {e}")
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