"""音乐源适配器基类与数据结构"""

from dataclasses import dataclass, field


@dataclass
class MusicItem:
    """统一歌曲数据结构"""

    source: str          # 来源标识：netease/kuwo/qqmusic/kugou/bilibili
    id: str              # 源内歌曲唯一 ID
    title: str           # 歌名
    artist: str          # 歌手
    album: str = ""      # 专辑
    duration: int = 0    # 时长（秒）
    artwork: str = ""    # 封面图 URL
    url: str = ""        # 音频直链（可能为空，需 get_media_url 获取）
    extra: dict = field(default_factory=dict)  # 源特有数据（如 bilibili 的 bvid/cid、qq 的 songmid）

    @property
    def key(self) -> str:
        """统计用唯一键"""
        return f"{self.source}:{self.id}"

    @property
    def display(self) -> str:
        """列表展示文本"""
        dur = f"[{self.duration // 60}:{self.duration % 60:02d}]" if self.duration else ""
        return f"{self.title} - {self.artist} {dur}".strip()


class MusicSource:
    """音乐源抽象基类"""

    name = "base"
    display_name = "未知源"

    def __init__(self, session) -> None:
        """session 为共享 requests.Session"""
        self.session = session

    async def search(self, keyword: str, limit: int = 5) -> list[MusicItem]:
        """搜索歌曲，返回统一 MusicItem 列表"""
        raise NotImplementedError

    async def get_media_url(self, item: MusicItem, quality: str = "standard") -> str:
        """获取音频直链；不支持返回空字符串"""
        return item.url or ""

    async def get_hot(self, limit: int = 5) -> list[MusicItem]:
        """获取热门/榜单歌曲；不支持返回空列表"""
        return []

    @property
    def supports_card(self) -> bool:
        """是否支持音乐卡片（QQ 源）"""
        return False
