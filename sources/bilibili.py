"""bilibili 音乐源（MusicFree bilibili 插件的 Python 移植）"""

import asyncio
import re

try:
    from ..log import get_logger
    from .base import MusicItem, MusicSource
except ImportError:  # unittest discover 顶层导入场景
    from log import get_logger
    from base import MusicItem, MusicSource

logger = get_logger()

SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
PLAYURL_URL = "https://api.bilibili.com/x/player/playurl"
POPULAR_URL = "https://api.bilibili.com/x/web-interface/popular/v2"


class BilibiliSource(MusicSource):
    """bilibili：搜索 B 站视频（音频直链为 dash audio）。与 MusicFree 官方插件行为一致"""

    name = "bilibili"
    display_name = "哔哩哔哩"

    # buvid cookie 缓存（SPI 单次请求即可，短时复用避免每次搜索多一次 HTTP 往返）
    _cookie_cache: str = ""
    _cookie_ts: float = 0.0
    _COOKIE_TTL = 600.0  # 10 分钟

    async def _get_cookie(self) -> str:
        import time as _time

        if (
            self._cookie_cache
            and _time.monotonic() - self._cookie_ts < self._COOKIE_TTL
        ):
            return self._cookie_cache

        def _do():
            r = self.session.get(SPI_URL, timeout=10)
            d = r.json()["data"]
            return f"buvid3={d['b_3']};buvid4={d['b_4']}"

        try:
            cookie = await asyncio.to_thread(_do)
        except Exception:
            return self._cookie_cache or ""
        self._cookie_cache = cookie
        self._cookie_ts = _time.monotonic()
        return cookie

    @staticmethod
    def _clean_title(title: str) -> str:
        """去掉搜索结果中的 <em class="keyword"> 高亮标签"""
        return re.sub(r"</?em[^>]*>", "", title or "")

    def _to_item(self, v: dict) -> MusicItem:
        title = self._clean_title(v.get("title", ""))
        alias = re.search(r"《(.+?)》", title)
        duration = v.get("duration", 0) or 0
        if isinstance(duration, str) and ":" in duration:
            # B 站返回 "mm:ss" 格式时长
            parts = duration.split(":")
            duration = int(parts[0]) * 60 + int(parts[1])
        return MusicItem(
            source=self.name,
            id=str(v.get("cid") or v.get("bvid") or v.get("aid") or ""),
            title=title,
            artist=v.get("author") or ((v.get("owner") or {}).get("name", "")),
            album=str(v.get("bvid") or v.get("aid") or ""),
            duration=duration,
            artwork=("https:" + v["pic"]) if str(v.get("pic", "")).startswith("//") else (v.get("pic") or ""),
            extra={"aid": v.get("aid"), "bvid": v.get("bvid"), "cid": v.get("cid")},
        )

    async def search(self, keyword: str, limit: int = 5) -> list[MusicItem]:
        cookie = await self._get_cookie()

        def _do():
            r = self.session.get(
                SEARCH_URL,
                params={
                    "search_type": "video", "keyword": keyword, "page": 1,
                    "page_size": limit, "highlight": 1, "single_column": 0,
                    "platform": "pc", "from_source": "", "dynamic_offset": 0,
                },
                headers={
                    "cookie": cookie,
                    "referer": "https://search.bilibili.com/",
                    "origin": "https://search.bilibili.com",
                    "accept": "application/json, text/plain, */*",
                },
                timeout=12,
            )
            return r.json()

        try:
            j = await asyncio.to_thread(_do)
        except Exception as e:
            logger.warning(f"[bilibili] 搜索「{keyword}」请求异常: {e}")
            return []
        if j.get("code") != 0:
            logger.warning(f"[bilibili] 搜索「{keyword}」被拒绝, code={j.get('code')}, msg={j.get('message', '')}")
            return []
        results = (j.get("data") or {}).get("result") or []
        if not results:
            logger.warning(f"[bilibili] 搜索「{keyword}」无结果")
        return [self._to_item(v) for v in results[:limit]]

    async def get_media_url(self, item: MusicItem, quality: str = "standard") -> str:
        """view 获取 cid → playurl 获取 dash 音频（MusicFree 插件 quality 映射：low/standard/high/super）"""
        qmap = {"low": 0, "standard": 1, "high": 2, "super": -1}

        def _do():
            params = {"bvid": item.extra.get("bvid")} if item.extra.get("bvid") else {"aid": item.extra.get("aid")}
            cid = item.extra.get("cid")
            if not cid:
                r = self.session.get(VIEW_URL, params=params, headers={"referer": "https://www.bilibili.com/"}, timeout=12)
                cid = (r.json().get("data") or {}).get("cid")
            if not cid:
                return ""
            r2 = self.session.get(PLAYURL_URL, params={**params, "cid": cid, "fnval": 16}, headers={"referer": "https://www.bilibili.com/"}, timeout=12)
            data = (r2.json().get("data") or {})
            audios = (data.get("dash") or {}).get("audio") or []
            if not audios:
                durl = data.get("durl")
                return (durl or [{}])[0].get("url", "")
            audios.sort(key=lambda a: a["bandwidth"])
            idx = qmap.get(quality, 1)
            if idx < 0:
                idx = len(audios) - 1
            else:
                idx = min(idx, len(audios) - 1)
            return audios[idx]["baseUrl"]

        try:
            url = await asyncio.to_thread(_do)
            if not url:
                logger.warning(f"[bilibili] 播放地址获取失败（可能被风控）: {item.title}")
            return url
        except Exception as e:
            logger.warning(f"[bilibili] 播放地址获取异常: {e}")
            return ""

    async def get_hot(self, limit: int = 5) -> list[MusicItem]:
        """B 站综合热门视频"""
        cookie = await self._get_cookie()

        def _do():
            r = self.session.get(
                POPULAR_URL,
                params={"pn": 1, "ps": limit},
                headers={"cookie": cookie, "referer": "https://www.bilibili.com/"},
                timeout=12,
            )
            return r.json()

        try:
            j = await asyncio.to_thread(_do)
        except Exception as e:
            logger.warning(f"[bilibili] 热门榜单获取异常: {e}")
            return []
        if j.get("code") != 0:
            logger.warning(f"[bilibili] 热门榜单被拒绝, code={j.get('code')}, msg={j.get('message', '')}")
            return []
        items = []
        for v in (j.get("data") or {}).get("list") or []:
            aid = v.get("aid")
            bvid = v.get("bvid")
            duration = v.get("duration", 0) or 0
            if isinstance(duration, str) and ":" in duration:
                parts = duration.split(":")
                duration = int(parts[0]) * 60 + int(parts[1])
            items.append(
                MusicItem(
                    source=self.name,
                    id=str(v.get("cid") or bvid or aid or ""),
                    title=self._clean_title(v.get("title", "")),
                    artist=((v.get("owner") or {}).get("name", "")),
                    album=str(bvid or aid or ""),
                    duration=duration,
                    artwork=v.get("pic") or "",
                    extra={"aid": aid, "bvid": bvid, "cid": v.get("cid")},
                )
            )
        return items[:limit]

    async def parse_share(self, text: str) -> MusicItem | None:
        """解析 B 站分享链接（bilibili.com/video/BVxxx 或短链）→ 歌曲"""
        m = re.search(r"bilibili\.com/video/(BV[0-9A-Za-z]+)", text, re.I)
        if not m:
            m = re.search(r"(BV[0-9A-Za-z]{10})", text)
        if not m:
            return None
        bvid = m.group(1)

        def _do():
            r = self.session.get(VIEW_URL, params={"bvid": bvid}, timeout=12)
            return r.json()

        try:
            j = await asyncio.to_thread(_do)
        except Exception as e:
            logger.warning(f"[bilibili] 解析链接请求异常: {e}")
            return None
        v = j.get("data") or {}
        if not v:
            logger.warning(f"[bilibili] 解析链接无数据: {bvid}")
            return None
        return MusicItem(
            source=self.name,
            id=str(v.get("cid") or bvid),
            title=v.get("title", "bilibili视频"),
            artist=(v.get("owner") or {}).get("name", ""),
            album=bvid,
            duration=int(v.get("duration", 0) or 0),
            artwork=v.get("pic") or "",
            extra={"aid": v.get("aid"), "bvid": bvid, "cid": v.get("cid")},
        )
