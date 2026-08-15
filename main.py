"""AstrBot 群聊点歌插件：多音乐源聚合搜索、语音/卡片发送、选歌交互、统计、收藏、播放队列、链接解析、群独立配置"""

import asyncio
import math
import os
import random
import re
import time
import unicodedata
from datetime import datetime

from astrbot.api import AstrBotConfig
from astrbot.api.all import MessageChain, MessageEventResult
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Image, Music, Plain, Record
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .log import get_logger
from .netease_login import (
    make_qrcode_image,
    qrcode_check,
    qrcode_login_get,
    sms_login,
    sms_send,
)
from .sources import SourceManager
from .stats import MusicStats
from .store import BlockedStore, Favorites, GroupConfigs, PlatformLearner, PushState, QuotaStore
from .voice import convert_to_amr, download_audio

logger = get_logger()

# 点歌标准指令名。
# 注意：部署环境的 AstrBot wake_prefix 为 "/"，消息前缀会在唤醒阶段被剥离，
# 因此这里注册剥离后的形式 "点歌"（用户实际发送 "/点歌 xxx"）。
# 群聊中不带 "/" 的 "点歌 xxx" 无法唤醒机器人，CommandFilter 不会触发，天然保证必须用 "/"。
ORDER_COMMAND = "点歌"
DEFAULT_ORDER_ALIASES = set()
# 序号选择
SELECT_PATTERN = re.compile(r"^\s*(\d{1,2})\s*$")
# URL 链接
URL_PATTERN = re.compile(r"https?://[^\s，。！？!?]+", re.I)
# 取消选歌
CANCEL_PATTERN = re.compile(r"^(取消|退出|算了|不听了|不要了|不想听)$")

# 高音质前缀（后接关键词）
HIGH_QUALITY_RE = re.compile(r"^(高清|高音质|无损|音质)[\s:：]+(.+)$", re.S)
# 歌词搜索前缀（后接歌词片段）
LYRIC_SEARCH_RE = re.compile(r"^歌词[\s:：]+(.+)$", re.S)
# 翻页指令
PAGE_PATTERN = re.compile(r"^(下一页|下页|更多|上一页|上页|上一批|下一批)$")

ADMIN_KEYS = {
    "sources": "启用音乐源（逗号分隔：netease,kuwo,kugou,qqmusic,bilibili）",
    "search_limit": "每页/每源搜索结果条数",
    "select_timeout": "选歌等待秒数",
    "frequency_seconds": "每人点歌间隔秒数",
    "voice_mode": "语音模式：url/amr/off",
    "enable_card": "是否发送QQ音乐卡片",
    "queue_limit": "每个群队列上限",
    "queue_interval": "队列歌曲播放间隔秒数",
    "admins": "额外管理员QQ（逗号分隔）",
    "quality": "默认音质：standard/high/low",
    "daily_limit": "每人每日点歌次数上限（0=不限）",
    "aliases": "自定义点歌指令别名（必须以 / 开头，逗号分隔）",
    "enable_artwork": "是否显示封面图",
    "enable_lyric": "是否附带歌词",
    "hot_push_enable": "是否开启定时热门推送",
    "hot_push_time": "定时推送时间 HH:MM",
    "hot_push_groups": "推送目标群号（逗号分隔）",
    "hot_push_platform": "推送平台 ID（留空自动学习，点歌时按群自动记录；旧值 onebot 已弃用）",
    "weekly_report_enable": "是否开启每周点歌排行榜推送",
    "weekly_report_weekday": "周报推送星期（1=周一 … 7=周日）",
    "weekly_report_time": "周报推送时间 HH:MM",
    "weekly_report_groups": "周报目标群号（逗号分隔，留空自动推所有有点歌记录的群）",
    "cache_max_mb": "语音缓存上限 MB",
    "netease_cookie": "网易云登录 Cookie（浏览器登录 music.163.com 后复制，含 MUSIC_U；可解锁 VIP 歌曲直链）",
}

RANDOM_KEYWORDS = ["热门", "经典", "2024", "抖音", "古风", "翻唱", "纯音乐", "BGM", "粤语", "民谣"]

# 语音发送结果标记：URL 直发成功
_URL_SENT = object()

# 品质回退链：请求高音质失败时逐级降级
_QUALITY_CHAIN = {
    "super": ["super", "high", "standard"],
    "high": ["high", "standard"],
    "standard": ["standard"],
    "low": ["low", "standard"],
}


@register("astrbot_plugin_music_custom", "Administrator", "群聊点歌：多源聚合搜索，语音/卡片发送，收藏/队列/统计/链接解析", "1.6.1")
class MusicPlugin(Star):
    """点歌指令「/点歌 歌名」，支持随机/热门/统计/收藏/排队，选中后发语音或QQ音乐卡片"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        # 数据目录：AstrBot 标准 plugin_data（仅读写插件自身文件，绝不触碰全局配置）
        self.data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin_data", "astrbot_plugin_music_custom")
        os.makedirs(self.data_dir, exist_ok=True)
        self.sources = SourceManager(self.config)
        self.stats = MusicStats(self.data_dir) if self.config.get("enable_stats", True) else None
        self.favs = Favorites(self.data_dir)
        self.groups = GroupConfigs(self.data_dir)
        self.quota = QuotaStore(self.data_dir)
        self.blocked = BlockedStore(self.data_dir)
        self.push_state = PushState(self.data_dir)
        self._learned = PlatformLearner(self.data_dir)
        self._pending: dict[str, dict] = {}   # group_id -> {user_id: {mode, items, page, ts, kw, quality}}
        self._last_order: dict[str, float] = {}  # user_id -> 上次点歌时间戳
        # 播放队列：group_id -> [队列项]
        self._queues: dict[str, list] = {}
        self._queue_last: dict[str, float] = {}
        self._queue_task: asyncio.Task | None = None
        self._push_task: asyncio.Task | None = None
        self._report_task: asyncio.Task | None = None
        # 网易云登录：进行中的扫码任务与验证码会话（group_id -> dict）
        self._login_tasks: dict[str, asyncio.Task] = {}
        self._sms_sessions: dict[str, dict] = {}
        self._sync_order_aliases()
        # 恢复上次登录的网易云 Cookie（数据目录持久化，避免 WebUI 配置覆盖丢失）
        try:
            from .store import JsonStore

            cookie_store = JsonStore(
                os.path.join(self.data_dir, "netease_cookie.json"), {}
            )
            saved = cookie_store.data.get("netease_cookie") or ""
            if saved and not (self.config.get("netease_cookie") or "").strip():
                self.config["netease_cookie"] = saved
                self.sources.reload()
                logger.info("已恢复上次登录的网易云 Cookie")
        except Exception as e:
            logger.warning(f"恢复网易云 Cookie 失败: {e}")
        # 定期清理缓存目录
        self._cleanup_cache()
        # WebUI 直接配置 hot_push_enable 时也要启动热推任务
        if self.config.get("hot_push_enable", False):
            self._ensure_push_task()
        # 周报定时推送
        if self.stats is not None and self.config.get("weekly_report_enable", True):
            self._ensure_report_task()
        logger.info("【CUSTOM-MUSIC】插件初始化完成")

    # ---------- 工具 ----------

    def _purge_stale_pending(self, group_id: str = "") -> None:
        """清理过期选歌会话（超时后长时间无人交互的堆积项）"""
        try:
            timeout = max(1, self._safe_int("select_timeout", 30, group_id))
            now = time.time()
            groups = [group_id] if group_id else list(self._pending.keys())
            for gid in groups:
                pend = self._pending.get(gid)
                if not pend:
                    continue
                expired = [uid for uid, s in pend.items() if now - s.get("ts", 0) > timeout]
                for uid in expired:
                    pend.pop(uid, None)
                if not pend:
                    self._pending.pop(gid, None)
        except Exception:
            pass

    def _cfg(self, key, default, group_id: str = ""):
        """配置读取：群独立配置优先，其次全局"""
        if group_id:
            v = self.groups.get_key(group_id, key)
            if v is not None:
                return v
        return self.config.get(key, default)

    def _safe_int(self, key: str, default: int, group_id: str = "") -> int:
        """安全整数配置：非法/缺失值回退默认，避免 WebUI 脏值导致崩溃"""
        try:
            return int(self._cfg(key, default, group_id))
        except (TypeError, ValueError):
            return int(default)

    def _sync_order_aliases(self) -> None:
        """将 aliases 配置合并进标准指令的别名集合（装饰器静态注册的 CommandFilter）"""
        try:
            from astrbot.core.star.filter.command import CommandFilter
            from astrbot.core.star.star_handler import (
                EventType,
                star_handlers_registry,
            )

            aliases = str(self._cfg("aliases", "")).replace("，", ",").strip()
            # 与唤醒阶段一致：注册剥离 "/" 前缀后的别名（用户实际发送 "/别名"）
            extra = {a.strip().lstrip("/") for a in aliases.split(",") if a.strip()}
            for md in star_handlers_registry.get_handlers_by_event_type(
                EventType.AdapterMessageEvent,
                plugins_name=None,
            ):
                if (
                    md.handler_module_path != "astrbot_plugin_music_custom.main"
                    or md.handler_name != "order_music"
                ):
                    continue
                for f in md.event_filters:
                    if isinstance(f, CommandFilter) and f.command_name == ORDER_COMMAND:
                        f.alias.update(extra)
                        f._cmpl_cmd_names = None
                        break
        except Exception as e:
            logger.warning(f"同步点歌指令别名失败: {e}")

    def _is_admin(self, event) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        admins = str(self._cfg("admins", "")).replace("，", ",")
        return str(event.get_sender_id()) in [a.strip() for a in admins.split(",") if a.strip()]

    def _check_frequency(self, user_id: str, group_id: str = "") -> bool:
        """频率限制：返回 True 表示允许"""
        sec = self._safe_int("frequency_seconds", 30, group_id)
        if sec <= 0:
            return True
        now = time.time()
        last = self._last_order.get(user_id, 0)
        if now - last < sec:
            return False
        self._last_order[user_id] = now
        return True

    def _check_quota(self, event, group_id: str) -> tuple[bool, str]:
        """每日配额检查：返回 (允许, 提示消息)。允许时扣除一次"""
        limit = self._safe_int("daily_limit", 0, group_id)
        if limit <= 0:
            return True, ""
        user_id = str(event.get_sender_id())
        today = time.strftime("%Y-%m-%d")
        used = self.quota.used_today(user_id, today)
        if used >= limit:
            return False, f"⚠️ 今日点歌已达上限（{limit} 次），明天再来吧～"
        cnt = self.quota.consume(user_id, today)
        remain = limit - cnt
        hint = f"今日剩余 {remain} 次" if remain <= 3 else ""
        return True, hint

    def _send_text(self, event, text: str) -> MessageEventResult:
        """构造纯文本回复结果"""
        return event.chain_result([Plain(text)])

    @staticmethod
    def _norm(s: str) -> str:
        """文本归一化：全角→半角、去空格与标点、小写（用于跨源去重与宽松重搜）"""
        s = unicodedata.normalize("NFKC", s or "")
        return re.sub(r"[\s\W_]+", "", s).lower()

    @staticmethod
    def _merge_results(results, keyword: str) -> list:
        """合并多源结果：跨源去重 + 标题含关键词者优先（保持源顺序稳定）"""
        norm_kw = MusicPlugin._norm(keyword)
        seen: set = set()
        items = []
        for _, its in results:
            for it in its:
                key = (MusicPlugin._norm(it.title), MusicPlugin._norm(it.artist)[:8])
                if key in seen:
                    continue
                seen.add(key)
                # 命中分：标题包含完整关键词 2 分，歌手包含 1 分
                t = MusicPlugin._norm(it.title)
                a = MusicPlugin._norm(it.artist)
                hit = 0
                if norm_kw and norm_kw in t:
                    hit = 2
                elif norm_kw and norm_kw in a:
                    hit = 1
                items.append((hit, it))
        items.sort(key=lambda x: x[0], reverse=True)
        return [it for _, it in items]

    def _page_items(self, sess: dict, page: int):
        """按页切分列表，返回 (当前页列表, 页码, 总页数)"""
        per = max(1, int(self._cfg("search_limit", 5, sess.get("group_id", ""))))
        total = max(1, math.ceil(len(sess["items"]) / per))
        page = max(1, min(page, total))
        items = sess["items"]
        return items[(page - 1) * per: page * per], page, total

    def _fmt_list(self, items, page: int = 1, total_pages: int = 1, head: str = "") -> str:
        lines = [head or "为你找到这些歌曲，回复序号选择（{} 秒内）:".format(self._safe_int("select_timeout", 30))]
        for i, it in enumerate(items, 1):
            src = self.sources.get(it.source)
            tag = src.display_name if src else it.source
            lines.append(f"{i}. {it.display}（{tag}）")
        if total_pages > 1:
            lines.append(f"— 第 {page}/{total_pages} 页，发「/点歌 下一页」翻页 —")
        return "\n".join(lines)

    def _ensure_queue_task(self):
        if self._queue_task is None or self._queue_task.done():
            self._queue_task = asyncio.create_task(self._queue_loop())

    def _ensure_push_task(self):
        if self._push_task is None or self._push_task.done():
            self._push_task = asyncio.create_task(self._hot_push_loop())

    def _ensure_report_task(self):
        if self._report_task is None or self._report_task.done():
            self._report_task = asyncio.create_task(self._report_push_loop())

    # ---------- 发送 ----------

    async def _try_voice(self, event, url: str, item):
        """尝试发送语音。

        返回：
            _URL_SENT: URL 直发成功（已直接发送，无需再回结果）
            Record:   amr 转码成功，可加入结果链发送
            None:     发送失败
        """
        mode = str(self._cfg("voice_mode", "url", str(event.get_group_id() or ""))).strip().lower()
        if mode == "off":
            return None
        group_id = str(event.get_group_id() or "")
        bot = getattr(event, "bot", None)
        if mode == "url" and bot is not None and group_id.isdigit():
            try:
                seg = {"type": "record", "data": {"file": url}}
                if event.get_message_type() == MessageType.GROUP_MESSAGE:
                    await bot.send_group_msg(group_id=int(group_id), message=[seg])
                else:
                    await bot.send_private_msg(user_id=int(event.get_sender_id()), message=[seg])
                return _URL_SENT
            except Exception as e:
                logger.warning(f"点歌语音 URL 直发失败，转 amr 模式: {e}")
        # amr 兜底：下载 → 转码 → 本地文件发送（命中缓存则跳过）
        try:
            tmp_dir = self._cache_dir()
            os.makedirs(tmp_dir, exist_ok=True)
            mp3_path = os.path.join(tmp_dir, f"{item.key.replace(':', '_')}.mp3")
            amr_path = os.path.join(tmp_dir, f"{item.key.replace(':', '_')}.amr")
            if os.path.exists(amr_path) and os.path.getsize(amr_path) > 2048:
                # 缓存复用：已存在有效 amr，直接发送
                return Record.fromFileSystem(amr_path)
            if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) <= 2048:
                if not await download_audio(url, mp3_path):
                    return None
            if not await convert_to_amr(mp3_path, amr_path):
                return None
            self._cleanup_cache()
            return Record.fromFileSystem(amr_path)
        except Exception as e:
            logger.warning(f"点歌语音 amr 发送失败: {e}")
            return None

    def _cache_dir(self) -> str:
        """语音缓存目录：可用配置 cache_dir 覆盖，默认插件目录下 cache"""
        cfg_dir = str(self.config.get("cache_dir", "") or "").strip()
        if cfg_dir:
            return os.path.abspath(cfg_dir)
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")

    def _cleanup_cache(self):
        """清理缓存目录，超过 cache_max_mb 时删除最旧文件"""
        try:
            max_mb = float(self._cfg("cache_max_mb", 200))
            if max_mb <= 0:
                return
            cache_dir = self._cache_dir()
            if not os.path.isdir(cache_dir):
                return
            # 递归扫描（含封面缩略图等子目录），避免子目录占用漏清理
            files = [
                os.path.join(root, f)
                for root, _, names in os.walk(cache_dir)
                for f in names
            ]
            total = sum(os.path.getsize(f) for f in files) / 1024 / 1024
            if total <= max_mb:
                return
            files.sort(key=lambda f: os.path.getmtime(f))
            for f in files:
                if total <= max_mb:
                    break
                try:
                    total -= os.path.getsize(f) / 1024 / 1024
                    os.remove(f)
                except Exception:
                    pass
        except Exception:
            pass

    def _build_card(self, item) -> list:
        """QQ 音乐卡片（仅 qqmusic 源）+ 封面 + 链接文本（组件列表）"""
        segs = []
        qqsrc = self.sources.get("qqmusic")
        if item.source == "qqmusic" and self._cfg("enable_card", True) and qqsrc is not None:
            try:
                card = qqsrc.get_card(item)
                if card is not None:
                    segs.append(Music(**card))
            except Exception as e:
                logger.warning(f"QQ 音乐卡片构建失败，回退封面/链接: {e}")
        if not segs and self._cfg("enable_artwork", True) and item.artwork and item.artwork.startswith("http"):
            try:
                segs.append(Image.fromURL(item.artwork))
            except Exception:
                pass
        if item.url and item.url.startswith("http"):
            segs.append(Plain(f"🎵 {item.title} - {item.artist}\n链接: {item.url}"))
        elif item.source == "netease" and item.id.isdigit():
            # 网易云 VIP/无版权歌曲拿不到直链：补网页链接兜底，点开可试听
            segs.append(Plain(f"🎵 {item.title} - {item.artist}\n链接: https://music.163.com/song?id={item.id}"))
        return segs

    # ---------- 播放主流程 ----------

    async def _play_item(self, event, item, keyword: str = "", sess: dict | None = None) -> MessageEventResult | None:
        """对选中的歌曲执行：语音 → 卡片/链接 → 歌词 → 统计"""
        group_id = str(event.get_group_id() or "")
        # 屏蔽词拦截（防收藏/队列绕过）
        if self.blocked.is_blocked(item):
            return self._send_text(event, f"⛔ 歌曲「{item.title}」被屏蔽，无法播放～")
        # 点歌人标识
        name = event.get_sender_name() or f"用户 {event.get_sender_id()}"
        head = f"🎵 {name} 点了《{item.title}》"
        if keyword and keyword not in ("链接", "收藏", "热门"):
            head += f"（{keyword}）"

        quality = (sess or {}).get("quality") or str(self._cfg("quality", "standard", group_id))
        chain = None
        url = ""
        for q in _QUALITY_CHAIN.get(quality, ["standard"]):
            url = await self.sources.first_media_url(item, quality=q)
            if url:
                logger.info(f"[music] 播放地址获取成功（{q}）: {item.title} - {item.artist}")
                break
        if not url:
            logger.warning(
                f"[music] 所有音质均无法获取播放地址，回退卡片/链接: {item.title} - {item.artist}（源: {item.source}）"
            )
        if url:
            voice_res = await self._try_voice(event, url, item)
            if voice_res is _URL_SENT:
                # 已直接发送语音：先发点歌人标识，再补歌词
                logger.info(f"[music] 语音 URL 直发成功: {item.title}")
                if self._cfg("enable_lyric", True):
                    await event.send(MessageChain([Plain(head)]))
                    await self._send_lyric(event, item)
                else:
                    await event.send(MessageChain([Plain(head)]))
                self._record(event, item, group_id)
                return None
            if isinstance(voice_res, Record):
                # amr 语音已就绪
                logger.info(f"[music] amr 转码语音发送成功: {item.title}")
                segs = []
                if self._cfg("enable_lyric", True):
                    segs.append(Plain(head))
                    segs.extend(await self._lyric_segs(item))
                segs.append(voice_res)
                self._record(event, item, group_id)
                return event.chain_result(segs)
            # 语音失败走卡片回退（卡片本身可能带封面/链接）
            logger.warning(f"[music] 语音发送失败，回退卡片/链接: {item.title}")
            chain = self._build_card(item)
        if not chain:
            chain = [Plain(f"⚠️ 歌曲「{item.title}」暂无法获取音频，可换个关键词试试～")]
            return event.chain_result(chain)
        segs = [Plain(head)]
        if self._cfg("enable_lyric", True):
            segs.extend(await self._lyric_segs(item))
        segs.extend(chain)
        self._record(event, item, group_id)
        return event.chain_result(segs)

    async def _lyric_segs(self, item) -> list:
        """获取歌词文本（仅网易云），失败返回空列表"""
        if item.source != "netease":
            return []
        try:
            lyric = await self.sources.get_lyric(item, max_lines=3)
        except Exception:
            return []
        if lyric:
            return [Plain(f"🎤 {lyric.replace(chr(10), ' / ')}")]
        return []

    async def _send_lyric(self, event, item):
        """URL 直发模式下单独补发歌词"""
        segs = await self._lyric_segs(item)
        if segs:
            try:
                await event.send(MessageChain(segs))
            except Exception:
                pass

    def _record(self, event, item, group_id: str = ""):
        if self.stats is not None:
            try:
                self.stats.record(item, str(event.get_sender_id()), event.get_sender_name() or "", group_id)
            except Exception as e:
                logger.warning(f"记录点歌统计失败: {e}")

    # ---------- 收藏 ----------

    def _fav_items(self, user_id: str) -> list:
        return self.favs.items(user_id)

    # ---------- 各功能分支 ----------

    async def _do_search(self, event, keyword: str, mode: str = "search", quality: str = "") -> MessageEventResult | None:
        """搜索并进入选歌状态；mode: search/fav/queue/lyric；含去重排序、屏蔽过滤、宽松重搜、翻页"""
        user_id = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "private")
        self._purge_stale_pending()
        ok, hint = self._check_quota(event, group_id)
        if not ok:
            return self._send_text(event, hint)
        limit = self._safe_int("search_limit", 5, group_id)
        kw = keyword
        results = await self.sources.search_all(kw, limit=limit)
        logger.info(
            f"[music] 搜索「{kw}」完成，共 {len(results)} 个源返回结果（源: {[n for n, _ in results]}）"
        )
        # 宽松重搜：无结果且关键词含全角/标点时，归一化后重试
        if not results:
            nk = self._norm(kw)
            if nk != self._norm_all(kw):
                results = await self.sources.search_all(self._norm(kw), limit=limit)
        if mode == "lyric":
            lyric_items = await self.sources.search_by_lyric(kw, limit=limit * 2)
            items = [it for it in lyric_items if not self.blocked.is_blocked(it)][:limit]
            if not items:
                return self._send_text(event, "😢 没找到包含这段歌词的歌，换个词试试？")
            head = f"🎶 按歌词找到这些歌{(f'（{hint}）') if hint else ''}，回复序号播放："
            self._pending.setdefault(group_id, {})[user_id] = {
                "mode": "search", "items": items, "page": 1, "ts": time.time(), "kw": kw, "quality": quality, "group_id": group_id,
            }
            return self._send_text(event, self._fmt_list(items, 1, 1, head))
        items = self._merge_results(results, kw)
        # 屏蔽词过滤
        blocked_n = 0
        filtered = []
        for it in items:
            if self.blocked.is_blocked(it):
                blocked_n += 1
                continue
            filtered.append(it)
        items = filtered
        if not items:
            tip = f"（已过滤 {blocked_n} 条被屏蔽的结果）" if blocked_n else ""
            logger.warning(f"[music] 「{kw}」搜索无任何可播放结果")
            return self._send_text(event, f"😢 没有找到相关歌曲{tip}，换个关键词试试？")
        heads = {
            "search": f"找到这些歌曲，回复序号播放{(f'（{hint}）') if hint else ''}：",
            "fav": "搜索到这些歌曲，回复序号即可收藏：",
            "queue": "搜索到这些歌曲，回复序号加入播放队列：",
        }
        head = heads[mode]
        if blocked_n:
            head += f"（已过滤 {blocked_n} 条屏蔽结果）"
        self._pending.setdefault(group_id, {})[user_id] = {
            "mode": mode, "items": items, "page": 1, "ts": time.time(), "kw": kw, "quality": quality, "group_id": group_id,
        }
        page_items, page, total = self._page_items(self._pending[group_id][user_id], 1)
        return self._send_text(event, self._fmt_list(page_items, page, total, head))

    @staticmethod
    def _norm_all(kw: str) -> str:
        """去掉空白与标点（区别于 _norm 的全角转半角），用于判断是否需要宽松重搜"""
        return re.sub(r"[\s\W_]+", "", kw or "").lower()

    async def _do_random(self, event) -> MessageEventResult | None:
        """随机点歌：随机关键词搜索后直接播第一首"""
        group_id = str(event.get_group_id() or "")
        if not self._check_frequency(str(event.get_sender_id()), group_id):
            return self._send_text(event, "⏳ 点歌太频繁啦，休息一下再点～")
        ok, hint = self._check_quota(event, group_id)
        if not ok:
            return self._send_text(event, hint)
        kw = random.choice(RANDOM_KEYWORDS)
        results = await self.sources.search_all(kw, limit=5)
        items = [it for _, its in results for it in its]
        items = [it for it in items if not self.blocked.is_blocked(it)]
        if not items:
            return self._send_text(event, "😢 随机点歌失败了，再试试？")
        item = random.choice(items)
        return await self._play_item(event, item, kw)

    async def _do_hot(self, event) -> MessageEventResult | None:
        """热门/榜单：取第一个启用源的热门列表，进入选歌状态"""
        user_id = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "private")
        ok, hint = self._check_quota(event, group_id)
        if not ok:
            return self._send_text(event, hint)
        items = []
        for name in self.sources.order:
            src = self.sources.get(name)
            if not src:
                continue
            try:
                items = await src.get_hot(limit=self._safe_int("search_limit", 5, group_id))
                if items:
                    break
            except Exception:
                continue
        items = [it for it in items if not self.blocked.is_blocked(it)]
        if not items:
            return self._send_text(event, "😢 热门榜单获取失败，稍后再试～")
        self._pending.setdefault(group_id, {})[user_id] = {
            "mode": "search", "items": items, "page": 1, "ts": time.time(), "kw": "热门", "quality": "", "group_id": group_id,
        }
        page_items, page, total = self._page_items(self._pending[group_id][user_id], 1)
        return self._send_text(event, self._fmt_list(page_items, page, total))

    async def _do_parse_url(self, event, text: str) -> MessageEventResult | None:
        """解析分享链接直接播放"""
        group_id = str(event.get_group_id() or "")
        if not self._check_frequency(str(event.get_sender_id()), group_id):
            return self._send_text(event, "⏳ 点歌太频繁啦，休息一下再点～")
        ok, _ = self._check_quota(event, group_id)
        if not ok:
            return self._send_text(event, "⚠️ 今日点歌已达上限，明天再来吧～")
        m = URL_PATTERN.search(text)
        url = m.group(0) if m else text
        item = await self.sources.parse_share(url)
        if item is None:
            return self._send_text(event, "😢 这个链接还解析不了，试试直接发歌名吧～")
        return await self._play_item(event, item, "链接")

    async def _do_stats(self, event, extra: str = "") -> MessageEventResult:
        """点歌统计：总榜 / 周 / 月 / 我 / 人 / 群"""
        if self.stats is None:
            return self._send_text(event, "统计功能未启用（enable_stats=False）")
        user_id = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")
        days = {"周": 7, "月": 30, "星期": 7}
        if extra in days:
            top = self.stats.top_songs(10, days[extra])
            label = "近7天" if days[extra] == 7 else "近30天"
        elif extra in ("我", "个人", "我的"):
            top = self.stats.top_songs_by_user(user_id, 5)
            if not top:
                return self._send_text(event, "你还没有点过歌，快来点一首吧～")
            lines = ["🎶 我的最爱 Top 5:"]
            for i, s in enumerate(top, 1):
                lines.append(f"{i}. {s['title']} - {s['artist']}（点了 {s['score']} 次）")
            return self._send_text(event, "\n".join(lines))
        elif extra in ("人", "达人", "用户"):
            top = self.stats.top_users(10)
            if not top:
                return self._send_text(event, "还没有人点过歌，快来点第一首吧～")
            lines = ["🏆 点歌达人 Top 10:"]
            for i, u in enumerate(top, 1):
                lines.append(f"{i}. {u['name'] or u['user_id']}（点了 {u['score']} 次）")
            return self._send_text(event, "\n".join(lines))
        elif extra in ("群", "本群", "群里"):
            if not group_id:
                return self._send_text(event, "私聊无法查看群内排行～")
            top = self.stats.top_songs_by_group(group_id, 10)
            if not top:
                return self._send_text(event, "本群还没有人点过歌，快来点第一首吧～")
            lines = ["🏆 本群最受欢迎 Top 10:"]
            for i, s in enumerate(top, 1):
                lines.append(f"{i}. {s['title']} - {s['artist']}（点了 {s['score']} 次）")
            return self._send_text(event, "\n".join(lines))
        elif extra.startswith(("歌 ", "歌曲 ")):
            # 单曲详情：/点歌 统计 歌 <关键词>（谁点过、多少次、最近一次）
            kw = re.sub(r"^(歌|歌曲)\s*", "", extra).strip()
            hits = self.stats.find_songs(kw, 5)
            if not hits:
                return self._send_text(event, f"没有「{kw}」的点歌记录～")
            lines = [f"🎶 「{kw}」相关点歌记录:"]
            for s in hits:
                lines.append(f"📍 {s['title']} - {s['artist']}")
                lines.append(f"   共点 {s['count']} 次，最近 {time.strftime('%m-%d %H:%M', time.localtime(s.get('last_at', 0)))}")
                users = s.get("users", {})
                if users:
                    who = "、".join(list(users)[:5])
                    lines.append(f"   点过的人: {who}" + (" 等" if len(users) > 5 else ""))
            return self._send_text(event, "\n".join(lines))
        else:
            top = self.stats.top_songs(10)
            label = "总榜"
        if not top:
            return self._send_text(event, "还没有人点过歌，快来点第一首吧～")
        lines = [f"🎶 最受欢迎点歌 Top 10（{label}）:"]
        for i, s in enumerate(top, 1):
            lines.append(f"{i}. {s['title']} - {s['artist']}（点了 {s['score']} 次）")
        return self._send_text(event, "\n".join(lines))

    # ---------- 播放队列 ----------

    async def _queue_loop(self):
        """队列播放循环：每 2 秒轮询，按群间隔播放队首歌曲"""
        while True:
            await asyncio.sleep(2)
            for gid in list(self._queues.keys()):
                q = self._queues.get(gid)
                if not q:
                    continue
                interval = self._safe_int("queue_interval", 5, gid)
                now = time.time()
                if now - self._queue_last.get(gid, 0) < interval:
                    continue
                entry = q.pop(0)
                if not q:
                    self._queues.pop(gid, None)
                self._queue_last[gid] = now
                try:
                    await self._play_queue_entry(gid, entry)
                except Exception as e:
                    logger.warning(f"队列播放失败: {e}")

    async def _play_queue_entry(self, group_id: str, entry: dict):
        """队列项播放（使用 event.send 主动发送）"""
        event = entry["event"]
        item = entry["item"]
        if self.blocked.is_blocked(item):
            await event.send(MessageChain([Plain(f"⛔ 队列中「{item.title}」被屏蔽，已跳过～")]))
            return
        url = await self.sources.first_media_url(item, quality=str(self._cfg("quality", "standard", group_id)))
        if url:
            voice_res = await self._try_voice(event, url, item)
            if voice_res is _URL_SENT:
                self._record_queue(group_id, entry)
                return
            if isinstance(voice_res, Record):
                self._record_queue(group_id, entry)
                await event.send(MessageChain([voice_res]))
                return
        segs = self._build_card(item)
        if segs:
            self._record_queue(group_id, entry)
            await event.send(MessageChain(segs))
        else:
            await event.send(MessageChain([Plain(f"⚠️ 队列中「{item.title}」暂无法获取音频，已跳过～")]))

    def _record_queue(self, group_id: str, entry: dict):
        if self.stats is not None:
            try:
                self.stats.record(entry["item"], entry["user_id"], entry["user_name"], group_id)
            except Exception as e:
                logger.warning(f"记录队列播放统计失败: {e}")

    def _enqueue(self, group_id: str, item, event, user_id: str, user_name: str) -> str:
        q = self._queues.setdefault(group_id, [])
        limit = self._safe_int("queue_limit", 10, group_id)
        if len(q) >= limit:
            return f"队列已满（上限 {limit} 首），请稍后再试～"
        q.append({"item": item, "event": event, "user_id": user_id, "user_name": user_name})
        self._ensure_queue_task()
        pos = len(q)
        return f"🎧 已加入队列第 {pos} 位：{item.title} - {item.artist}（/song queue 查看队列）"

    # ---------- 定时热门推送 ----------

    def _learn_platform(self, group_id: str, platform_id: str) -> None:
        """从群聊事件学习"群 → 平台实例 ID"映射（定时推送定位平台用）"""
        if not group_id or not platform_id:
            return
        try:
            self._learned.learn(group_id, platform_id)
        except Exception as e:
            logger.warning(f"学习平台映射失败: {e}")

    def _platform_for_group(self, group_id: str) -> str | None:
        """推送平台解析：群内学习记录优先；其次显式配置（跳过已弃用的 onebot/default 占位值）；未知返回 None"""
        learned = self._learned.get(group_id)
        if learned:
            return learned
        cfg = str(self._cfg("hot_push_platform", "")).strip()
        if cfg and cfg not in ("onebot", "default"):
            return cfg
        return None

    async def _hot_push_loop(self):
        """定时热门推送：每 30 秒检查一次，到达配置时间后向目标群推送"""
        while True:
            await asyncio.sleep(30)
            try:
                await self._hot_push_once()
            except Exception as e:
                logger.warning(f"定时热门推送失败: {e}")

    async def _hot_push_once(self):
        if not self._cfg("hot_push_enable", False):
            return
        target = str(self._cfg("hot_push_time", "21:00")).strip()
        now = time.strftime("%H:%M")
        # 30 秒轮询可能错过精确分钟：到达或超过目标时间即推送（同日仅一次，由 push_state 保证）
        if now < target:
            return
        groups = [g.strip() for g in str(self._cfg("hot_push_groups", "")).replace("，", ",").split(",") if g.strip().isdigit()]
        if not groups:
            return
        today = time.strftime("%Y-%m-%d")
        # 取热门榜单
        items = []
        for name in self.sources.order:
            src = self.sources.get(name)
            if not src:
                continue
            try:
                items = await src.get_hot(limit=10)
                if items:
                    break
            except Exception:
                continue
        if not items:
            return
        lines = ["🎶 每日热门点歌推荐 Top 5:"]
        for i, it in enumerate(items[:5], 1):
            lines.append(f"{i}. {it.title} - {it.artist}")
        lines.append("回复「/点歌 序号」即可点歌～")
        chain = MessageChain([Plain("\n".join(lines))])
        for gid in groups:
            key = f"{today}:{gid}"
            if self.push_state.already_pushed(key):
                continue
            platform = self._platform_for_group(gid)
            if not platform:
                logger.warning(
                    f"定时热门推送跳过 group={gid}: 未配置推送平台（请在群内点歌自动学习，"
                    "或配置 hot_push_platform 为平台实例 ID）"
                )
                continue
            session = f"{platform}:GroupMessage:{gid}"
            try:
                ok = await self.context.send_message(session, chain)
                if ok:
                    self.push_state.mark_pushed(key)
                    logger.info(f"定时热门推送成功: group={gid}")
            except Exception as e:
                logger.warning(f"定时热门推送失败 group={gid}: {e}")

    # ---------- 点歌周报 ----------

    async def _report_push_loop(self):
        """周报推送：每 30 秒检查一次，到达配置星期/时间后向目标群推送"""
        while True:
            await asyncio.sleep(30)
            try:
                await self._report_push_once()
            except Exception as e:
                logger.warning(f"定时周报推送失败: {e}")

    def _report_target_groups(self) -> list[str]:
        """周报目标群：优先配置指定；为空时自动使用所有有点歌记录的群"""
        groups = [
            g.strip()
            for g in str(self._cfg("weekly_report_groups", "")).replace("，", ",").split(",")
            if g.strip().isdigit()
        ]
        if groups:
            return groups
        if self.stats is not None:
            return self.stats.active_groups()
        return []

    def _weekly_report_text(self, group_id: str = "") -> str:
        """构建周报文本（group_id 为空时为全局榜单，否则为群内榜单）"""
        top = (
            self.stats.top_songs_by_group(group_id, limit=10, days=7)
            if group_id
            else self.stats.top_songs(limit=10, days=7)
        )
        total = (
            self.stats.group_totals(group_id, days=7)
            if group_id
            else sum(s["score"] for s in self.stats.top_songs(limit=100, days=7))
        )
        lines = [
            "📊 点歌周报（近 7 天）",
            "━━━━━━━━━━━━━━━━━━",
            f"🎵 本周共点歌 {total} 次",
            "",
            "🏆 最热歌曲 Top 10:",
        ]
        for i, s in enumerate(top, 1):
            lines.append(f"{i}. {s['title']} - {s['artist']}（点了 {s['score']} 次）")
        if not top:
            lines.append("（本周还没有人点歌～快去点一首吧）")
        lines.append("")
        lines.append("💡 查看其他维度: /点歌 统计 周/月/我/人/群")
        return "\n".join(lines)

    async def _report_push_once(self):
        """到达配置的星期与时间后推送周报（同周同群仅一次，由 push_state 保证）"""
        if self.stats is None or not self._cfg("weekly_report_enable", True):
            return
        target_time = str(self._cfg("weekly_report_time", "20:00")).strip()
        now = time.strftime("%H:%M")
        if now < target_time:
            return
        # 星期语义：1=周一 … 7=周日（与 Python weekday() 0-6 对齐）
        weekday_cfg = self._safe_int("weekly_report_weekday", 7, "")
        weekday = datetime.now().weekday() + 1
        if weekday != weekday_cfg:
            return
        groups = self._report_target_groups()
        if not groups:
            return
        iso = datetime.now().isocalendar()
        for gid in groups:
            key = f"week:{iso[0]}-W{iso[1]}:{gid}"
            if self.push_state.already_pushed(key):
                continue
            platform = self._platform_for_group(gid)
            if not platform:
                logger.warning(
                    f"周报推送跳过 group={gid}: 未配置推送平台（请在群内点歌自动学习，"
                    "或配置 hot_push_platform 为平台实例 ID）"
                )
                continue
            try:
                msg = self._weekly_report_text(gid)
                ok = await self.context.send_message(
                    f"{platform}:GroupMessage:{gid}", MessageChain([Plain(msg)])
                )
                if ok:
                    self.push_state.mark_pushed(key)
                    logger.info(f"周报推送成功: group={gid}")
            except Exception as e:
                logger.warning(f"周报推送失败 group={gid}: {e}")

    # ---------- 指令 ----------

    @filter.command(ORDER_COMMAND, alias=set(DEFAULT_ORDER_ALIASES), priority=1000)
    async def order_music(
        self,
        event: AstrMessageEvent,
        song_name: GreedyStr,
    ) -> MessageEventResult | None:
        """点歌主入口：点歌 [歌名|随机|热门|统计|收藏|排队|链接|歌词|高清|下一页]（标准指令，需 @机器人或唤醒词）"""
        kw = song_name.strip().strip("，。！？!?")
        group_id = str(event.get_group_id() or "")
        if group_id:
            self._learn_platform(group_id, str(event.get_platform_id() or ""))
        try:
            if not kw or kw in ("随机", "随便", "来一首"):
                return await self._do_random(event)
            if kw in ("热门", "榜单", "排行榜", "热榜"):
                return await self._do_hot(event)
            if kw == "统计" or kw.startswith("统计 "):
                extra = kw[2:].strip() if kw.startswith("统计") else ""
                return await self._do_stats(event, extra)
            # 周报：手动查看近 7 天点歌排行（群内=群榜，私聊=总榜）
            if kw in ("周报", "周榜"):
                if self.stats is None:
                    return self._send_text(event, "统计功能未启用（enable_stats=False）")
                return self._send_text(event, self._weekly_report_text(group_id))
            # 批量点歌：/点歌 批量 歌1，歌2，歌3
            if kw in ("批量", "连播", "多首") or kw.startswith(("批量 ", "连播 ", "多首 ")):
                names = kw.split(maxsplit=1)[1].strip() if " " in kw else ""
                if not names:
                    return self._send_text(event, "批量点歌用法: /点歌 批量 歌1，歌2，歌3（最多 5 首，用逗号/顿号分隔）")
                if not self._check_frequency(str(event.get_sender_id()), group_id):
                    return self._send_text(event, "⏳ 点歌太频繁啦，休息一下再点～")
                return await self._do_batch(event, names)
            # 完整歌词：/点歌 歌词本 <歌名>（区别于「歌词 xxx」的按歌词搜歌）
            if kw.startswith(("歌词本", "看歌词", "完整歌词")):
                sub = re.sub(r"^(歌词本|看歌词|完整歌词)[\s:：]*", "", kw).strip()
                if not sub:
                    return self._send_text(event, "歌词本用法: /点歌 歌词本 <歌名>（仅网易云歌曲有歌词）")
                if not self._check_frequency(str(event.get_sender_id()), group_id):
                    return self._send_text(event, "⏳ 点歌太频繁啦，休息一下再点～")
                return await self._do_lyric_full(event, sub)
            pm = PAGE_PATTERN.search(kw)
            if pm:
                return await self._do_page(event, pm.group(1))
            lm = LYRIC_SEARCH_RE.search(kw)
            if lm:
                if not self._check_frequency(str(event.get_sender_id()), group_id):
                    return self._send_text(event, "⏳ 点歌太频繁啦，休息一下再点～")
                return await self._do_search(event, lm.group(1), mode="lyric")
            hm = HIGH_QUALITY_RE.search(kw)
            if hm:
                if not self._check_frequency(str(event.get_sender_id()), group_id):
                    return self._send_text(event, "⏳ 点歌太频繁啦，休息一下再点～")
                return await self._do_search(event, hm.group(2), quality="high")
            if kw.startswith("收藏") and len(kw) > 2:
                sub = kw[2:].strip()
                if not self._check_frequency(str(event.get_sender_id()), group_id):
                    return self._send_text(event, "⏳ 点歌太频繁啦，休息一下再点～")
                return await self._do_search(event, sub, mode="fav")
            if kw.startswith("排队") and len(kw) > 2:
                sub = kw[2:].strip()
                if not self._check_frequency(str(event.get_sender_id()), group_id):
                    return self._send_text(event, "⏳ 点歌太频繁啦，休息一下再点～")
                return await self._do_search(event, sub, mode="queue")
            if URL_PATTERN.search(kw):
                return await self._do_parse_url(event, kw)
            # 普通搜索
            if not self._check_frequency(str(event.get_sender_id()), group_id):
                return self._send_text(event, "⏳ 点歌太频繁啦，休息一下再点～")
            return await self._do_search(event, kw)
        finally:
            event.stop_event()

    # ---------- 批量点歌 / 完整歌词 ----------

    async def _do_batch(self, event, names: str) -> MessageEventResult | None:
        """批量点歌：/点歌 批量 歌1，歌2，歌3（最多 5 首，逐首搜索取最佳结果直接播放）"""
        parts = [p.strip() for p in re.split(r"[,，、;；|｜]+", names) if p.strip()]
        if not parts:
            return self._send_text(event, "批量点歌用法: /点歌 批量 歌1，歌2，歌3")
        parts = parts[:5]
        user_id = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "private")
        limit = self._safe_int("search_limit", 5, group_id)
        played = []
        failed = []
        for kw in parts:
            ok, hint = self._check_quota(event, group_id)
            if not ok:
                failed.append(f"「{kw}」{hint}")
                continue
            try:
                results = await self.sources.search_all(kw, limit=limit)
                items = [it for _, its in results for it in its]
                items = [it for it in items if not self.blocked.is_blocked(it)]
                if not items:
                    failed.append(f"「{kw}」未找到")
                    continue
                item = items[0]
                await self._play_item(event, item, kw)
                played.append(f"「{item.title} - {item.artist}」")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[music] 批量点歌「{kw}」失败: {e}")
                failed.append(f"「{kw}」播放失败")
        lines = []
        if played:
            lines.append(f"🎵 批量点歌完成（{len(played)} 首）:")
            lines.extend(f"  ✅ {p}" for p in played)
        if failed:
            lines.append(f"⚠️ 未播放（{len(failed)} 首）:")
            lines.extend(f"  ❌ {p}" for p in failed)
        return self._send_text(event, "\n".join(lines) or "😢 批量点歌失败，稍后再试～")

    async def _do_lyric_full(self, event, keyword: str) -> MessageEventResult | None:
        """完整歌词：/点歌 歌词本 <歌名>（取最佳结果，仅网易云有歌词）"""
        limit = int(self._cfg("search_limit", 5, str(event.get_group_id() or "private")))
        results = await self.sources.search_all(keyword, limit=limit)
        items = [it for _, its in results for it in its]
        items = [it for it in items if not self.blocked.is_blocked(it)]
        if not items:
            return self._send_text(event, f"😢 没有找到「{keyword}」这首歌～")
        item = items[0]
        if item.source != "netease":
            return self._send_text(event, f"「{item.title}」来自 {item.source}，暂不支持歌词显示（仅网易云）～")
        try:
            lyric = await self.sources.get_lyric(item, max_lines=60)
        except Exception:
            lyric = ""
        if not lyric:
            return self._send_text(event, f"「{item.title}」暂无歌词或获取失败～")
        lines = [f"🎤 {item.title} - {item.artist}"]
        lines.append("━━━━━━━━━━━━━━━━━")
        body = lyric.splitlines()
        # 超长保护：最多展示 40 行
        if len(body) > 40:
            body = body[:40]
            body.append("…（歌词较长，已截断）")
        lines.extend(body)
        return self._send_text(event, "\n".join(lines))

    async def _do_page(self, event, cmd: str) -> MessageEventResult | None:
        """翻页：下一页 / 上一页"""
        group_id = str(event.get_group_id() or "private")
        pending = self._pending.get(group_id)
        if not pending:
            return self._send_text(event, "当前没有进行中的选歌列表，发「/点歌 歌名」开始吧～")
        user_id = str(event.get_sender_id())
        sess = pending.get(user_id)
        if not sess:
            return self._send_text(event, "你当前没有选歌列表，发「/点歌 歌名」开始吧～")
        try:
            if time.time() - sess["ts"] > self._safe_int("select_timeout", 30, group_id):
                pending.pop(user_id, None)
                if not pending:
                    self._pending.pop(group_id, None)
                return self._send_text(event, "⏳ 选歌已超时，请重新搜索～")
            if cmd in ("下一页", "下页", "更多", "下一批"):
                sess["page"] += 1
            else:
                sess["page"] -= 1
            page_items, page, total = self._page_items(sess, sess["page"])
            sess["page"] = page
            return self._send_text(event, self._fmt_list(page_items, page, total))
        finally:
            event.stop_event()

    @filter.regex(CANCEL_PATTERN, priority=225)
    async def cancel_select(self, event: AstrMessageEvent) -> MessageEventResult | None:
        """取消当前选歌"""
        group_id = str(event.get_group_id() or "private")
        pending = self._pending.get(group_id)
        if not pending or str(event.get_sender_id()) not in pending:
            return None
        try:
            pending.pop(str(event.get_sender_id()), None)
            if not pending:
                self._pending.pop(group_id, None)
            return self._send_text(event, "👌 已取消，不想听就不听～")
        finally:
            event.stop_event()

    @filter.regex(SELECT_PATTERN, priority=230)
    async def select_song(self, event: AstrMessageEvent) -> MessageEventResult | None:
        """选歌：回复序号（搜索/收藏/排队 按模式分发，支持翻页后的全局序号）"""
        group_id = str(event.get_group_id() or "private")
        pending = self._pending.get(group_id)
        if not pending:
            return None
        user_id = str(event.get_sender_id())
        sess = pending.get(user_id)
        if not sess:
            return None
        m = SELECT_PATTERN.search(event.message_str)
        if not m:
            return None
        try:
            if time.time() - sess["ts"] > self._safe_int("select_timeout", 30, group_id):
                pending.pop(user_id, None)
                if not pending:
                    self._pending.pop(group_id, None)
                return None
            idx = int(m.group(1))
            items = sess["items"]
            if not 1 <= idx <= len(items):
                return self._send_text(event, f"序号要在 1-{len(items)} 之间哦～")
            pending.pop(user_id, None)
            if not pending:
                self._pending.pop(group_id, None)
            # 翻页后的全局序号
            per = max(1, self._safe_int("search_limit", 5, group_id))
            global_idx = (int(sess.get("page", 1)) - 1) * per + idx
            if not 1 <= global_idx <= len(items):
                return self._send_text(event, "序号超出范围，请重新翻页查看～")
            item = items[global_idx - 1]
            mode = sess.get("mode", "search")
            if mode == "fav":
                # 收藏选择
                ok, msg = self.favs.add(user_id, item)
                return self._send_text(event, ("✅ " if ok else "") + msg)
            if mode == "queue":
                # 排队选择
                return self._send_text(event, self._enqueue(group_id, item, event, user_id, event.get_sender_name() or ""))
            return await self._play_item(event, item, sess.get("kw", ""), sess)
        finally:
            event.stop_event()

    @filter.command("song", priority=210)
    async def song_admin(self, event: AstrMessageEvent) -> MessageEventResult:
        """/song 子命令：set / gset / greset / fav / queue / block / help（用户发送 /song，前缀 / 由唤醒阶段剥离）"""
        try:
            text = event.message_str.strip()
            group_id = str(event.get_group_id() or "private")
            user_id = str(event.get_sender_id())
            m = re.match(r"^/?song\s+set\s+([\w_]+)\s+(.+)$", text, re.S)
            if m:
                if not self._is_admin(event):
                    return self._send_text(event, "⚠️ 只有管理员可以使用该指令")
                return await self._do_set(event, m.group(1), m.group(2), group_id, per_group=False)
            m = re.match(r"^/?song\s+gset\s+([\w_]+)\s+(.+)$", text, re.S)
            if m:
                if not self._is_admin(event):
                    return self._send_text(event, "⚠️ 只有管理员可以使用该指令")
                return await self._do_set(event, m.group(1), m.group(2), group_id, per_group=True)
            m = re.match(r"^/?song\s+greset(?:\s+([\w_]+))?$", text)
            if m:
                if not self._is_admin(event):
                    return self._send_text(event, "⚠️ 只有管理员可以使用该指令")
                key = m.group(1)
                ok = self.groups.reset_key(group_id, key)
                return self._send_text(event, f"✅ 已清除本群配置{'「' + key + '」' if key else ''}" if ok else f"本群没有{'该' if key else '任何'}群配置")
            m = re.match(r"^/?song\s+block\s+(.+)$", text, re.S)
            if m:
                if not self._is_admin(event):
                    return self._send_text(event, "⚠️ 只有管理员可以使用该指令")
                term = m.group(1).strip()
                if self.blocked.block(term):
                    return self._send_text(event, f"⛔ 已屏蔽词「{term}」（匹配歌曲标题/歌手）")
                return self._send_text(event, f"「{term}」已在屏蔽列表中")
            m = re.match(r"^/?song\s+unblock\s+(.+)$", text, re.S)
            if m:
                if not self._is_admin(event):
                    return self._send_text(event, "⚠️ 只有管理员可以使用该指令")
                term = m.group(1).strip()
                if self.blocked.unblock(term):
                    return self._send_text(event, f"✅ 已解除屏蔽「{term}」")
                return self._send_text(event, f"「{term}」不在屏蔽列表中")
            m = re.match(r"^/?song\s+blocks?$", text)
            if m:
                terms = self.blocked.list()
                if not terms:
                    return self._send_text(event, "当前没有屏蔽词。/song block 词 添加")
                return self._send_text(event, "⛔ 屏蔽词列表:\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(terms, 1)))
            m = re.match(r"^/?song\s+fav(?:\s+del\s+(\d{1,2}))?$", text)
            if m:
                if m.group(1):
                    ok = self.favs.remove(user_id, int(m.group(1)))
                    return self._send_text(event, "✅ 已删除该收藏" if ok else "❌ 序号不对哦，/song fav 查看你的收藏")
                items = self._fav_items(user_id)
                if not items:
                    return self._send_text(event, "你还没有收藏。发「/点歌 收藏 歌名」试试～")
                self._pending.setdefault(group_id, {})[user_id] = {
                    "mode": "favlist", "items": items, "page": 1, "ts": time.time(), "kw": "收藏", "quality": "", "group_id": group_id,
                }
                page_items, page, total = self._page_items(self._pending[group_id][user_id], 1)
                return self._send_text(event, self._fmt_list(page_items, page, total, f"🎵 我的收藏（共 {len(items)} 首），回复序号播放：" + "，/song fav del 序号 删除"))
            m = re.match(r"^/?song\s+queue(?:\s+(\w+))?$", text)
            if m:
                sub = m.group(1)
                if sub == "clear":
                    self._queues.pop(group_id, None)
                    return self._send_text(event, "✅ 已清空本群播放队列")
                if sub == "next":
                    if not self._queues.get(group_id):
                        return self._send_text(event, "队列是空的～")
                    self._queue_last[group_id] = 0
                    self._ensure_queue_task()
                    return self._send_text(event, "⏭️ 即将播放下一首～")
                return self._show_queue(event, group_id)
            m = re.match(r"^/?song\s+login(?:\s+(.+))?$", text, re.S)
            if m:
                if not self._is_admin(event):
                    return self._send_text(event, "⚠️ 只有管理员可以使用该指令")
                arg = (m.group(1) or "").strip()
                return await self._do_netease_login(event, arg, user_id, group_id)
            # 帮助
            return self._send_text(event, self._help_text())
        finally:
            event.stop_event()

    async def _do_netease_login(self, event, arg: str, user_id: str, group_id: str) -> MessageEventResult:
        """网易云登录：/song login（扫码）、/song login sms 手机号（发验证码）、/song login sms 手机号 验证码（登录）"""
        if not arg:
            # 扫码登录：生成二维码并后台轮询
            if group_id in self._login_tasks:
                return self._send_text(event, "⏳ 已有扫码登录进行中，请先在手机端确认或等待超时～")
            return await self._start_qrcode_login(event, group_id)
        m = re.match(r"^sms\s+(\d{5,15})(?:\s+(\d{4,6}))?$", arg, re.S)
        if not m:
            return self._send_text(
                event,
                "用法：\n/song login — 扫码登录\n/song login sms 手机号 — 发送验证码\n/song login sms 手机号 验证码 — 验证码登录",
            )
        phone = m.group(1)
        captcha = m.group(2)
        if not captcha:
            # 发送验证码
            res = await sms_send(self.sources.session, phone)
            if res["ok"]:
                self._sms_sessions[user_id] = {"phone": phone, "ts": time.time()}
            return self._send_text(event, ("✅ " if res["ok"] else "❌ ") + res["message"])
        # 验证码登录
        res = await sms_login(self.sources.session, phone, captcha)
        if not res["ok"]:
            return self._send_text(event, "❌ " + res["message"])
        cookie = res.get("cookie", "")
        if not cookie:
            return self._send_text(event, "⚠️ 登录成功但未获取到 Cookie，请重试或改用扫码登录")
        self._save_netease_cookie(cookie)
        self._sms_sessions.pop(user_id, None)
        logger.info(f"[netease-login] 手机号验证码登录成功: {phone[:3]}****{phone[-4:]}")
        return self._send_text(event, f"✅ 网易云登录成功（{phone}）！VIP 歌曲直链已解锁～")

    async def _start_qrcode_login(self, event, group_id: str) -> MessageEventResult:
        """扫码登录：获取 unikey → 生成二维码 → 后台轮询（60 秒 × 5s）"""
        try:
            unikey = await qrcode_login_get(self.sources.session)
        except Exception as e:
            logger.warning(f"[netease-login] 获取 unikey 失败: {e}")
            return self._send_text(event, f"❌ 获取二维码失败：{e}")
        qr_url = f"https://music.163.com/login?codekey={unikey}"
        try:
            png = make_qrcode_image(qr_url)
            img = Image.fromBytes(png)
        except Exception as e:
            logger.warning(f"[netease-login] 生成二维码失败: {e}")
            return self._send_text(event, f"❌ 生成二维码失败：{e}")

        async def _poll():
            await asyncio.sleep(1)
            deadline = time.time() + 60
            last_msg = ""
            while time.time() < deadline:
                try:
                    res = await qrcode_check(self.sources.session, unikey)
                except Exception as e:
                    logger.warning(f"[netease-login] 轮询异常: {e}")
                    await asyncio.sleep(5)
                    continue
                code = res.get("code")
                if code == 800:
                    await event.send(MessageChain([Plain("⏳ 二维码已过期，请重新发送 /song login 获取新二维码～")]))
                    return
                if code == 802:
                    msg = "📱 已扫码！请在手机上确认登录～"
                elif code == 801:
                    msg = ""
                elif code == 803:
                    cookies = res.get("cookies") or []
                    if not cookies:
                        await event.send(MessageChain([Plain("⚠️ 扫码成功但未获取到 Cookie，请重试")]))
                        return
                    self._save_netease_cookie("; ".join(cookies))
                    await event.send(MessageChain([Plain("✅ 网易云扫码登录成功！VIP 歌曲直链已解锁～")]))
                    logger.info("[netease-login] 扫码登录成功")
                    return
                else:
                    await asyncio.sleep(5)
                    continue
                if msg and msg != last_msg:
                    await event.send(MessageChain([Plain(msg)]))
                    last_msg = msg
                await asyncio.sleep(5)
            await event.send(MessageChain([Plain("⏳ 扫码登录超时，请重新发送 /song login 获取新二维码～")]))

        self._login_tasks[group_id] = asyncio.create_task(_poll())
        self._login_tasks[group_id].add_done_callback(
            lambda _t: self._login_tasks.pop(group_id, None)
        )
        chain = MessageChain(
            [Plain("📱 请使用手机网易云 App 扫码登录（60 秒内有效，登录后 VIP 歌曲直链自动解锁）：\n"), img]
        )
        return event.chain_result(chain)

    def _save_netease_cookie(self, cookie: str) -> None:
        """保存登录 Cookie：内存 + 插件数据目录原子持久化（不触碰 AstrBot 全局配置）"""
        # 仅保留关键字段，避免冗余
        keep = {"MUSIC_U", "__csrf", "NMTID", "MUSIC_A"}
        parts = [c.strip() for c in cookie.split(";") if "=" in c]
        kept = []
        for p in parts:
            k = p.split("=", 1)[0].strip()
            if not k:
                continue
            if k in keep or not kept:
                kept.append(p)
        cookie_str = "; ".join(kept)
        self.config["netease_cookie"] = cookie_str
        try:
            self.sources.reload()
        except Exception as e:
            logger.warning(f"[netease-login] reload 源失败: {e}")
        # 持久化到插件自身数据目录（原子写，WebUI 保存配置不会覆盖）
        try:
            from .store import JsonStore

            store = JsonStore(
                os.path.join(self.data_dir, "netease_cookie.json"), {}
            )
            store.data["netease_cookie"] = cookie_str
            store.data["updated_at"] = (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
            store.save()
            logger.info("[netease-login] Cookie 已持久化到插件数据目录")
        except Exception as e:
            logger.warning(f"[netease-login] Cookie 持久化失败（重启后可能丢失）: {e}")

    async def _do_set(self, event, key: str, value, group_id: str, per_group: bool) -> MessageEventResult:
        """设置配置（全局 / 群级）"""
        if key not in ADMIN_KEYS:
            return self._send_text(event, f"未知配置项: {key}。可用: {', '.join(ADMIN_KEYS)}")
        # 值类型转换
        if key in ("search_limit", "select_timeout", "frequency_seconds", "queue_limit", "queue_interval", "daily_limit", "cache_max_mb"):
            try:
                value = int(value)
            except ValueError:
                return self._send_text(event, "该项需要数字")
        if key in ("enable_card", "enable_artwork", "enable_lyric", "hot_push_enable"):
            value = str(value).lower() in ("1", "true", "on", "yes")
        if per_group:
            self.groups.set_key(group_id, key, value)
            return self._send_text(event, f"✅ 已设置本群 {key} = {value}（/song greset {key} 还原）")
        self.config[key] = value
        if key in ("sources", "netease_cookie"):
            self.sources.reload()
        if key == "aliases":
            self._sync_order_aliases()
        if key in ("hot_push_enable", "hot_push_time", "hot_push_groups"):
            self._ensure_push_task()
        if key in ("weekly_report_enable", "weekly_report_time", "weekly_report_weekday", "weekly_report_groups"):
            if self.stats is not None:
                self._ensure_report_task()
        return self._send_text(event, f"✅ 已更新全局 {key} = {value}（重启后若想保留请同步修改 WebUI 配置）")

    def _show_queue(self, event, group_id: str) -> MessageEventResult:
        q = self._queues.get(group_id) or []
        if not q:
            return self._send_text(event, "队列是空的。发「/点歌 排队 歌名」加入队列～")
        lines = [f"🎧 本群播放队列（{len(q)} 首）:"]
        for i, e in enumerate(q, 1):
            it = e["item"]
            lines.append(f"{i}. {it.title} - {it.artist}（由 {e['user_name'] or e['user_id']} 添加）")
        lines.append("提示: /song queue clear 清空 · /song queue next 跳下一首")
        return self._send_text(event, "\n".join(lines))

    def _help_text(self) -> str:
        return (
            "🎵 点歌插件使用说明\n"
            "使用方式：发送「/点歌 歌名」（需 @机器人 或唤醒词）\n"
            "/点歌 歌名 — 搜索点歌（回复序号播放，发「/点歌 下一页」翻页）\n"
            "/点歌 随机 / 热门 / 统计 — 随机、榜单、统计\n"
            "/点歌 统计 周/月/我/人/群 — 周榜/月榜/我的最爱/点歌达人/群内排行\n"
            "/点歌 歌词 片段 — 按歌词搜索\n"
            "/点歌 高清 歌名 — 高音质搜索\n"
            "/点歌 收藏 歌名 / 点歌 排队 歌名 — 搜索后收藏/加入队列\n"
            "粘贴音乐链接 — 网易云/QQ音乐/B站 直解析播放\n"
            "回复「取消」— 退出当前选歌\n"
            "/song fav — 查看收藏（回复序号播放，/song fav del 序号 删除）\n"
            "/song queue — 查看队列（clear 清空 / next 下一首）\n"
            "/song block 词 / unblock 词 / blocks — 屏蔽管理（管理员）\n"
            "/song set 键 值 — 全局配置（管理员）\n"
            "/song gset 键 值 — 本群配置（管理员，/song greset 还原）\n"
            "/song login — 网易云扫码登录（解锁 VIP 直链，管理员）\n"
            "/song login sms 手机号 — 发送短信验证码；/song login sms 手机号 验证码 — 验证码登录\n"
            "可配置键: " + ", ".join(ADMIN_KEYS)
        )

    async def terminate(self):
        """插件卸载时清理"""
        try:
            if self.stats is not None:
                self.stats.save()
            for task in list(self._login_tasks.values()):
                if not task.done():
                    task.cancel()
            for task in (self._queue_task, self._push_task, self._report_task):
                if task is not None and not task.done():
                    task.cancel()
        except Exception:
            pass
