# -*- coding: utf-8 -*-
"""music_custom 新功能测试（v1.7.0）：热评伴唱、个性化推荐。"""

import asyncio
import sys
import unittest
from unittest import mock

sys.path.insert(0, r"D:\astrbot\data\plugins")
sys.path.insert(0, r"D:\astrbot\data\plugins\astrbot_plugin_music_custom")

from astrbot_plugin_music_custom.main import MusicPlugin  # noqa: E402
from sources.base import MusicItem  # noqa: E402


def make_item(source="netease", title="晴天", artist="周杰伦", id="186016"):
    return MusicItem(
        source=source, id=id, title=title, artist=artist,
        url="", artwork="",
    )


class FakeEvent:
    def __init__(self, message_str="/点歌", sender_id="u1", sender_name="小明",
                 group_id="123", is_admin=False):
        self.message_str = message_str
        self._sid = sender_id
        self._name = sender_name
        self._gid = group_id
        self._admin = is_admin
        self.sent = []

    def get_sender_id(self):
        return self._sid

    def get_sender_name(self):
        return self._name

    def get_group_id(self):
        return self._gid

    def get_platform_id(self):
        return "onebot"

    def is_admin(self):
        return self._admin

    def stop_event(self):
        pass

    def chain_result(self, chain):
        return chain

    async def send(self, chain):
        self.sent.append(chain)


def run(coro):
    return asyncio.run(coro)


def chain_text(chain) -> str:
    if chain is None:
        return ""
    parts = []
    for comp in getattr(chain, "chain", chain):
        if hasattr(comp, "text"):
            parts.append(comp.text)
    return "".join(parts)


def make_plugin():
    """真实构造插件（data_dir 指向临时目录），再 mock 掉网络相关组件"""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="music_extra_")
    with mock.patch.object(MusicPlugin, "__init__", return_value=None):
        p = MusicPlugin(None, {})
    p.config = {}
    p.data_dir = tmp
    p.stats = None
    p.favs = mock.MagicMock()
    p.groups = mock.MagicMock()
    p.groups.get_key.return_value = None
    p.quota = mock.MagicMock()
    p.quota.used_today.return_value = 0
    p.quota.consume.return_value = 1
    p.blocked = mock.MagicMock()
    p.blocked.is_blocked.return_value = False
    p.sources = mock.MagicMock()
    p.push_state = mock.MagicMock()
    p._learned = mock.MagicMock()
    p._pending = {}
    p._last_order = {}
    p._queues = {}
    p._login_tasks = {}
    p._sms_sessions = {}
    return p


class TestHotComment(unittest.TestCase):
    def _setup_sources(self, p, comment="评论内容。"):
        netease_src = mock.MagicMock()
        netease_src.get_hot_comment = mock.AsyncMock(return_value=comment)
        p.sources.get.return_value = netease_src
        p.sources.search_all = mock.AsyncMock(
            return_value=[("netease", [make_item()])]
        )
        p._play_item = mock.AsyncMock(return_value=None)
        return netease_src

    def test_dispatch_and_play_with_comment(self):
        p = make_plugin()
        src = self._setup_sources(p, "故事的小黄花，从出生那年就飘着。")
        ev = FakeEvent("/点歌 热评 晴天")
        run(p.order_music(ev, "热评 晴天"))
        # 播放的是网易云版本，且带 tail_segs 热评段
        args, kwargs = p._play_item.call_args
        item, kw = args[1], args[2]
        self.assertEqual(item.source, "netease")
        self.assertEqual(item.title, "晴天")
        tail = kwargs.get("tail_segs") or []
        joined = "".join(getattr(s, "text", "") for s in tail)
        self.assertIn("网易云热评", joined)
        self.assertIn("故事的小黄花", joined)
        src.get_hot_comment.assert_awaited_once()

    def test_no_netease_version_hint(self):
        p = make_plugin()
        p.sources.search_all = mock.AsyncMock(
            return_value=[("kuwo", [make_item(source="kuwo", id="k1")])]
        )
        ev = FakeEvent("/点歌 热评 不存在的歌")
        text = chain_text(run(p.order_music(ev, "热评 不存在的歌")))
        self.assertIn("网易", text)

    def test_empty_keyword_usage(self):
        p = make_plugin()
        self._setup_sources(p)
        text = chain_text(run(p.order_music(FakeEvent("/点歌 热评"), "热评")))
        self.assertIn("用法", text)

    def test_no_comment_fallback_text(self):
        p = make_plugin()
        self._setup_sources(p, comment="")
        ev = FakeEvent("/点歌 热评 晴天")
        run(p.order_music(ev, "热评 晴天"))
        tail = p._play_item.call_args[1].get("tail_segs") or []
        joined = "".join(getattr(s, "text", "") for s in tail)
        self.assertIn("没有热评", joined)


class TestRecommend(unittest.TestCase):
    def _plugin_with_history(self, history):
        p = make_plugin()
        stats = mock.MagicMock()
        stats.top_songs_by_user.return_value = history
        p.stats = stats
        # 搜索返回同歌手的几首新歌 + 一首已听过的
        items = [
            make_item(title="七里香", artist="周杰伦"),
            make_item(title="晴天", artist="周杰伦"),       # 已点过
            make_item(title="稻香", artist="周杰伦"),
            make_item(source="kuwo", title="夜曲", artist="周杰伦"),
        ]
        p.sources.search_all = mock.AsyncMock(
            return_value=[("netease", items)]
        )
        return p

    def test_recommend_filters_played(self):
        history = [
            {"title": "晴天", "artist": "周杰伦", "score": 3},
            {"title": "告白气球", "artist": "周杰伦", "score": 1},
        ]
        p = self._plugin_with_history(history)
        ev = FakeEvent("/点歌 推荐")
        text = chain_text(run(p.order_music(ev, "推荐")))
        self.assertIn("常听「周杰伦」", text)
        # 已点过的晴天/告白气球不应出现在推荐列表
        self.assertNotIn("晴天 -", text)
        # 新歌出现
        self.assertIn("七里香", text)

    def test_recommend_no_history_hint(self):
        p = self._plugin_with_history([])
        ev = FakeEvent("/点歌 推荐")
        text = chain_text(run(p.order_music(ev, "推荐")))
        self.assertIn("还没有点过歌", text)
        self.assertIn("/点歌 <歌名>", text)

    def test_recommend_creates_pending_session(self):
        history = [{"title": "晴天", "artist": "周杰伦"}]
        p = self._plugin_with_history(history)
        ev = FakeEvent("/点歌 推荐")
        run(p.order_music(ev, "推荐"))
        sess = p._pending.get("123", {}).get("u1")
        self.assertIsNotNone(sess)
        self.assertEqual(sess["mode"], "search")

    def test_stats_disabled_hint(self):
        p = make_plugin()
        p.stats = None
        text = chain_text(run(p.order_music(FakeEvent("/点歌 推荐"), "推荐")))
        self.assertIn("未启用", text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
