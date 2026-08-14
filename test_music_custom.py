# -*- coding: utf-8 -*-
"""music_custom 插件单元测试：配置读取、管理员判定、限频、结果合并去重、缓存清理"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, r"D:\astrbot\data\plugins")
sys.path.insert(0, r"D:\astrbot\data\plugins\astrbot_plugin_music_custom")

from astrbot_plugin_music_custom.main import MusicPlugin  # noqa: E402

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


class FakeEvent:
    def __init__(self, sender_id="", is_admin=False, message_str="", group_id=""):
        self._sender_id = sender_id
        self._is_admin = is_admin
        self.message_str = message_str
        self._group_id = group_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return "测试用户"

    def get_group_id(self):
        return self._group_id

    def is_admin(self):
        return self._is_admin

    def stop_event(self):
        pass

    def chain_result(self, chain):
        return chain


class FakeSources:
    """可编程搜索源替身：by_kw 映射关键词到歌曲列表"""

    def __init__(self, by_kw, lyric="第一行歌词\n第二行歌词\n第三行歌词"):
        self.by_kw = by_kw
        self.lyric = lyric
        self.order = ["netease"]
        self.display_name = "netease"

    def get(self, name):
        return self

    async def search_all(self, kw, limit=5):
        items = list(self.by_kw.get(kw, []))[:limit]
        src = items[0].source if items else "netease"
        return [(src, items)]

    async def first_media_url(self, item, quality="standard"):
        return ""

    async def get_lyric(self, item, max_lines=4):
        if item.source != "netease":
            return ""
        return "\n".join(self.lyric.splitlines()[:max_lines])


def mk_item(source="netease", title="测试歌曲", artist="歌手甲", mid="1"):
    from astrbot_plugin_music_custom.sources.base import MusicItem

    return MusicItem(source=source, id=mid, title=title, artist=artist)


def make_plugin(**overrides):
    cfg = {
        "frequency_seconds": 30,
        "daily_limit": 0,
        "admins": "",
        "search_limit": 5,
        "select_timeout": 30,
        "cache_max_mb": 200,
        "voice_mode": "off",
        "weekly_report_enable": False,  # 测试环境无事件循环，避免 __init__ 创建后台任务
    }
    cfg.update(overrides)
    p = MusicPlugin(context=None, config=cfg)
    # 隔离有副作用的状态
    p._last_order = {}
    p._pending = {}
    # 用内存存储替换真实持久化，避免测试写入 plugin_data
    p.groups = SimpleNamespace(
        data={},
        get_key=lambda g, k: p.groups.data.get(g, {}).get(k),
        set_key=lambda g, k, v: p.groups.data.setdefault(g, {}).__setitem__(k, v),
        reset_key=lambda g, k=None: (
            p.groups.data.get(g, {}).pop(k, None) is not None
            if k
            else (p.groups.data.pop(g, None) is not None)
        ),
    )
    # 屏蔽词/收藏内存替身
    terms = []

    def _block(t):
        if t in terms:
            return False
        terms.append(t)
        return True

    def _unblock(t):
        if t in terms:
            terms.remove(t)
            return True
        return False

    p.blocked = SimpleNamespace(
        _terms=terms,
        block=_block,
        unblock=_unblock,
        list=lambda: list(terms),
        is_blocked=lambda t: t in terms,
    )
    p.favs = SimpleNamespace(remove=lambda uid, idx: True)
    return p


class TestConfig(unittest.TestCase):
    def test_cfg_global_default(self):
        p = make_plugin()
        self.assertEqual(p._cfg("frequency_seconds", 30), 30)

    def test_cfg_group_override_wins(self):
        p = make_plugin(frequency_seconds=30)
        p.groups.set_key("g1", "frequency_seconds", 5)
        self.assertEqual(p._cfg("frequency_seconds", 30, "g1"), 5)
        self.assertEqual(p._cfg("frequency_seconds", 30, "g2"), 30)

    def test_is_admin_ev_or_config(self):
        p = make_plugin(admins="10001, 10002")
        self.assertTrue(p._is_admin(FakeEvent(sender_id="1", is_admin=True)))
        self.assertTrue(p._is_admin(FakeEvent(sender_id="10001")))
        self.assertTrue(p._is_admin(FakeEvent(sender_id="10002")))
        self.assertFalse(p._is_admin(FakeEvent(sender_id="999")))

    def test_check_frequency(self):
        p = make_plugin(frequency_seconds=30)
        self.assertTrue(p._check_frequency("u1"))
        self.assertFalse(p._check_frequency("u1"))
        # 不同用户不受影响
        self.assertTrue(p._check_frequency("u2"))

    def test_frequency_disabled(self):
        p = make_plugin(frequency_seconds=0)
        self.assertTrue(p._check_frequency("u1"))
        self.assertTrue(p._check_frequency("u1"))


class TestMerge(unittest.TestCase):
    @staticmethod
    def _item(title, artist, source="netease"):
        return SimpleNamespace(title=title, artist=artist, source=source, display=f"{title}-{artist}")

    def test_norm(self):
        # 全角→半角、去空格/标点、小写
        self.assertEqual(MusicPlugin._norm("周杰伦《晴天》"), "周杰伦晴天")
        self.assertEqual(MusicPlugin._norm("  晴天。"), "晴天")
        self.assertEqual(MusicPlugin._norm("A-B_c 12"), "abc12")
        self.assertEqual(MusicPlugin._norm(""), "")

    def test_dedup_across_sources(self):
        results = [
            ("netease", [self._item("晴天", "周杰伦", "netease")]),
            ("kuwo", [self._item("晴 天", "周杰伦", "kuwo")]),  # 归一化后重复
        ]
        merged = MusicPlugin._merge_results(results, "晴天")
        self.assertEqual(len(merged), 1)

    def test_keyword_priority(self):
        results = [
            ("netease", [self._item("海阔天空", "Beyond", "netease")]),
            ("kuwo", [self._item("Beyond 海阔天空演唱会", "Beyond", "kuwo")]),
        ]
        merged = MusicPlugin._merge_results(results, "海阔天空")
        # 标题完整含关键词的排前面
        self.assertEqual(merged[0].title, "海阔天空")

    def test_artist_match_priority(self):
        results = [
            ("netease", [self._item("夜曲", "周杰伦", "netease")]),
            ("kuwo", [self._item("夜曲", "张学友", "kuwo")]),
        ]
        merged = MusicPlugin._merge_results(results, "周杰伦")
        self.assertEqual(merged[0].artist, "周杰伦")

    def test_page_items(self):
        p = make_plugin(search_limit=2)
        sess = {"items": [self._item(f"歌{i}", "a") for i in range(5)], "group_id": ""}
        page1, cur, total = p._page_items(sess, 1)
        self.assertEqual(len(page1), 2)
        self.assertEqual((cur, total), (1, 3))
        page3, cur, total = p._page_items(sess, 3)
        self.assertEqual(len(page3), 1)
        # 越界页码钳制
        page9, cur, total = p._page_items(sess, 9)
        self.assertEqual((cur, total), (3, 3))


class TestCleanupCache(unittest.TestCase):
    def setUp(self):
        # 使用临时目录作为缓存目录，避免污染/误删插件真实 cache 目录
        self._tmp = tempfile.mkdtemp(prefix="music_cache_test_")
        self.cache_dir = os.path.join(self._tmp, "cache")
        os.makedirs(self.cache_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _plugin(self):
        p = make_plugin(cache_max_mb=0.001, cache_dir=self._tmp)
        return p

    def test_cleanup_removes_oldest(self):
        p = self._plugin()  # 约 1KB 上限
        big = os.path.join(self.cache_dir, "big.mp3")
        small = os.path.join(self.cache_dir, "small.mp3")
        with open(big, "wb") as f:
            f.write(b"x" * 2048)
        # 注意：utime 须在文件句柄关闭后调用（Windows 上对打开文件设置 mtime 会静默失效）
        os.utime(big, (time.time() - 100, time.time() - 100))  # 最旧
        with open(small, "wb") as f:
            f.write(b"y" * 1024)
        p._cleanup_cache()
        self.assertFalse(os.path.exists(big), "最旧文件应被删除")
        self.assertTrue(os.path.exists(small))

    def test_cleanup_scans_subdirs(self):
        """递归扫描：子目录中的旧文件也应纳入清理"""
        p = self._plugin()
        sub = os.path.join(self.cache_dir, "test_sub")
        os.makedirs(sub, exist_ok=True)
        old_in_sub = os.path.join(sub, "old.bin")
        with open(old_in_sub, "wb") as f:
            f.write(b"z" * 2048)
        # utime 须在句柄关闭后调用，否则 Windows 上静默失效导致 mtime 未真正回拨
        os.utime(old_in_sub, (time.time() - 100, time.time() - 100))
        fresh = os.path.join(self.cache_dir, "fresh.bin")
        with open(fresh, "wb") as f:
            f.write(b"f" * 1024)
        p._cleanup_cache()
        self.assertFalse(os.path.exists(old_in_sub), "子目录旧文件应被删除")
        self.assertTrue(os.path.exists(fresh))

    def test_cleanup_under_limit_noop(self):
        p = self._plugin()
        f = os.path.join(self.cache_dir, "tiny.mp3")
        with open(f, "wb") as fh:
            fh.write(b"t")
        p._cleanup_cache()
        self.assertTrue(os.path.exists(f))


class TestNewFeatures(unittest.TestCase):
    def _plugin(self):
        p = make_plugin(enable_lyric=False, voice_mode="off")
        p.sources = FakeSources({})
        return p

    def _text(self, result):
        if not result:
            return ""
        chain = result if isinstance(result, list) else getattr(result, "chain", [])
        return "".join(c.text for c in chain if hasattr(c, "text"))

    def test_batch_order_splits_separators(self):
        p = self._plugin()
        p.sources.by_kw = {
            "歌一": [mk_item(title="歌一", artist="A", mid="1")],
            "歌二": [mk_item(title="歌二", artist="B", mid="2")],
            "歌三": [mk_item(title="歌三", artist="C", mid="3")],
        }
        result = asyncio.run(p._do_batch(FakeEvent("u1"), "歌一，歌二、歌三"))
        text = self._text(result)
        self.assertIn("批量点歌完成（3 首）", text)
        self.assertIn("歌一 - A", text)
        self.assertIn("歌二 - B", text)
        self.assertIn("歌三 - C", text)

    def test_batch_order_reports_missing(self):
        p = self._plugin()
        p.sources.by_kw = {"歌一": [mk_item(title="歌一", artist="A", mid="1")]}
        result = asyncio.run(p._do_batch(FakeEvent("u1"), "歌一，不存在的歌"))
        text = self._text(result)
        self.assertIn("批量点歌完成（1 首）", text)
        self.assertIn("未播放（1 首）", text)
        self.assertIn("不存在的歌」未找到", text)

    def test_batch_order_caps_at_5(self):
        p = self._plugin()
        p.sources.by_kw = {f"歌{i}": [mk_item(title=f"歌{i}", artist="A", mid=str(i))] for i in range(8)}
        result = asyncio.run(p._do_batch(FakeEvent("u1"), "，".join(f"歌{i}" for i in range(8))))
        text = self._text(result)
        self.assertIn("批量点歌完成（5 首）", text)

    def test_batch_order_empty_input(self):
        p = self._plugin()
        result = asyncio.run(p._do_batch(FakeEvent("u1"), "，，，"))
        self.assertIn("批量点歌用法", self._text(result))

    def test_batch_order_play_failure_isolated(self):
        p = self._plugin()
        p.sources.by_kw = {"歌一": [mk_item(title="歌一", artist="A", mid="1")]}

        async def boom(event, item, keyword="", sess=None):
            raise RuntimeError("播放失败")

        p._play_item = boom
        result = asyncio.run(p._do_batch(FakeEvent("u1"), "歌一，歌二"))
        text = self._text(result)
        self.assertIn("未播放（2 首）", text)
        self.assertIn("播放失败", text)

    def test_batch_respects_quota(self):
        p = self._plugin()
        p.config["daily_limit"] = 1
        state = {"used": 0}
        p.quota = SimpleNamespace(
            used_today=lambda uid, day: state["used"],
            consume=lambda uid, day: (state.__setitem__("used", state["used"] + 1) or state["used"]),
        )
        p.sources.by_kw = {"歌一": [mk_item(title="歌一", mid="1")], "歌二": [mk_item(title="歌二", mid="2")]}
        result = asyncio.run(p._do_batch(FakeEvent("u1"), "歌一，歌二"))
        text = self._text(result)
        self.assertIn("未播放", text)

    def test_lyric_full_netease(self):
        p = self._plugin()
        p.sources.by_kw = {"某歌": [mk_item(source="netease", title="某歌", artist="N", mid="9")]}
        result = asyncio.run(p._do_lyric_full(FakeEvent("u1"), "某歌"))
        text = self._text(result)
        self.assertIn("某歌 - N", text)
        self.assertIn("第一行歌词", text)
        self.assertIn("第三行歌词", text)

    def test_lyric_full_non_netease_hint(self):
        p = self._plugin()
        p.sources.by_kw = {"某歌": [mk_item(source="kuwo", title="某歌", artist="K", mid="9")]}
        result = asyncio.run(p._do_lyric_full(FakeEvent("u1"), "某歌"))
        self.assertIn("暂不支持歌词显示", self._text(result))

    def test_lyric_full_no_result(self):
        p = self._plugin()
        result = asyncio.run(p._do_lyric_full(FakeEvent("u1"), "虚空歌曲"))
        self.assertIn("没有找到", self._text(result))

    def test_lyric_full_empty_lyric(self):
        p = self._plugin()
        p.sources.by_kw = {"某歌": [mk_item(source="netease", title="某歌", mid="9")]}
        p.sources.lyric = ""
        result = asyncio.run(p._do_lyric_full(FakeEvent("u1"), "某歌"))
        self.assertIn("暂无歌词", self._text(result))

    def test_stats_song_detail(self):
        from astrbot_plugin_music_custom.stats import MusicStats

        p = self._plugin()
        with tempfile.TemporaryDirectory() as td:
            stats = MusicStats(td)
            stats.data["songs"] = {
                "netease:1": {
                    "title": "夜航星", "artist": "不才", "source": "netease",
                    "count": 3, "last_at": int(time.time()),
                    "daily": {}, "groups": {}, "users": {"u1": 2, "u2": 1},
                }
            }
            p.stats = stats
            result = asyncio.run(p._do_stats(FakeEvent("u1"), "歌 夜航"))
            text = self._text(result)
            self.assertIn("夜航星", text)
            self.assertIn("共点 3 次", text)
            self.assertIn("u1", text)
            result2 = asyncio.run(p._do_stats(FakeEvent("u1"), "歌 不存在"))
            self.assertIn("没有", self._text(result2))

    # ---------- 周报 ----------

    def _seed_stats(self, p):
        from datetime import date

        today = date.today().isoformat()
        p.stats.data["songs"] = {
            "netease:1": {
                "title": "晴天", "artist": "周杰伦", "source": "netease",
                "count": 5, "last_at": int(time.time()),
                "daily": {today: 5}, "groups": {"10001": 3, "20002": 2},
                "users": {"u1": 5},
            },
            "netease:2": {
                "title": "夜曲", "artist": "周杰伦", "source": "netease",
                "count": 2, "last_at": int(time.time()),
                "daily": {today: 2}, "groups": {"10001": 2},
                "users": {"u2": 2},
            },
        }
        p.stats.data["users"] = {
            "u1": {"name": "小明", "count": 5, "daily": {today: 5}},
            "u2": {"name": "小红", "count": 2, "daily": {today: 2}},
        }

    def test_weekly_report_group_text(self):
        p = make_plugin()
        self._seed_stats(p)
        text = p._weekly_report_text("10001")
        self.assertIn("点歌周报", text)
        self.assertIn("本周共点歌 5 次", text)  # 3 + 2（群占比）
        self.assertIn("晴天 - 周杰伦", text)
        self.assertIn("点了 3 次", text)

    def test_weekly_report_global_text(self):
        p = make_plugin()
        self._seed_stats(p)
        text = p._weekly_report_text("")
        self.assertIn("本周共点歌 7 次", text)
        self.assertIn("1. 晴天 - 周杰伦（点了 5 次）", text)

    def test_report_target_groups_from_config(self):
        p = make_plugin(weekly_report_groups="111, 222，333")
        self.assertEqual(p._report_target_groups(), ["111", "222", "333"])

    def test_report_target_groups_auto(self):
        p = make_plugin()
        self._seed_stats(p)
        groups = p._report_target_groups()
        self.assertEqual(sorted(groups), ["10001", "20002"])

    def test_report_push_sends_once_per_week(self):
        from datetime import datetime

        async def scenario():
            p = make_plugin(weekly_report_enable=True, weekly_report_time="00:00")
            p.config["weekly_report_weekday"] = datetime.now().weekday() + 1
            self._seed_stats(p)
            sent = []
            pushed = []

            async def fake_send(session, chain):
                sent.append((session, "".join(c.text for c in chain.chain)))
                return True

            p.context = SimpleNamespace(send_message=fake_send)
            p.push_state = SimpleNamespace(
                already_pushed=lambda k: k in pushed,
                mark_pushed=pushed.append,
            )
            await p._report_push_once()
            await p._report_push_once()
            return sent, pushed

        sent, pushed = asyncio.run(scenario())
        # 同周同群只推一次
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(pushed), 2)
        self.assertTrue(sent[0][0].endswith(":10001"))
        self.assertIn("晴天", sent[0][1])

    def test_report_push_skipped_wrong_weekday(self):
        from datetime import datetime

        async def scenario():
            p = make_plugin(weekly_report_enable=True, weekly_report_time="00:00")
            p.config["weekly_report_weekday"] = (datetime.now().weekday() + 1) % 7 + 1
            sent = []

            async def fake_send(session, chain):
                sent.append(session)
                return True

            p.context = SimpleNamespace(send_message=fake_send)
            p.push_state = SimpleNamespace(
                already_pushed=lambda k: False, mark_pushed=lambda k: None
            )
            await p._report_push_once()
            return sent

        self.assertEqual(asyncio.run(scenario()), [])

    def test_report_push_skipped_before_time(self):
        from datetime import datetime

        async def scenario():
            p = make_plugin(weekly_report_enable=True, weekly_report_time="99:99")
            p.config["weekly_report_weekday"] = datetime.now().weekday() + 1
            sent = []

            async def fake_send(session, chain):
                sent.append(session)
                return True

            p.context = SimpleNamespace(send_message=fake_send)
            p.push_state = SimpleNamespace(
                already_pushed=lambda k: False, mark_pushed=lambda k: None
            )
            await p._report_push_once()
            return sent

        self.assertEqual(asyncio.run(scenario()), [])

    # ---------- 修复回归测试 ----------

    def test_safe_int_falls_back_on_dirty_config(self):
        p = make_plugin(frequency_seconds="abc", daily_limit=None, search_limit="")
        # WebUI 脏值不会崩溃，回退默认
        self.assertEqual(p._safe_int("frequency_seconds", 30, ""), 30)
        self.assertEqual(p._safe_int("daily_limit", 0, ""), 0)
        self.assertEqual(p._safe_int("search_limit", 5, "g1"), 5)
        # 正常值仍然生效
        self.assertTrue(p._check_frequency("u1"))

    def test_hot_push_task_started_on_init(self):
        async def scenario():
            p = make_plugin(hot_push_enable=True)
            started = p._push_task is not None
            if p._push_task:
                p._push_task.cancel()
            return started
        self.assertTrue(asyncio.run(scenario()))

    def test_hot_push_task_not_started_when_disabled(self):
        p = make_plugin(hot_push_enable=False)
        self.assertIsNone(p._push_task)

    def test_hot_respects_quota(self):
        p = self._plugin()
        p.config["daily_limit"] = 0
        state = {"used": 0}
        p.quota = SimpleNamespace(
            used_today=lambda uid, day: state["used"],
            consume=lambda uid, day: (state.__setitem__("used", state["used"] + 1) or state["used"]),
        )
        # daily_limit=1 时超限拦截
        p.config["daily_limit"] = 1
        p.quota = SimpleNamespace(
            used_today=lambda uid, day: 1,
            consume=lambda uid, day: 2,
        )
        result = asyncio.run(p._do_hot(FakeEvent("u1")))
        self.assertIn("已达上限", self._text(result))

    def test_hot_allowed_with_quota(self):
        p = self._plugin()
        p.config["daily_limit"] = 5
        state = {"used": 0}
        p.quota = SimpleNamespace(
            used_today=lambda uid, day: state["used"],
            consume=lambda uid, day: (state.__setitem__("used", state["used"] + 1) or state["used"]),
        )
        async def fake_hot(limit=5):
            return [mk_item(title="热歌", mid="1")]
        p.sources.get_hot = fake_hot
        result = asyncio.run(p._do_hot(FakeEvent("u1")))
        self.assertIn("热歌", self._text(result))
        self.assertEqual(state["used"], 1)


class TestAdminCommands(unittest.TestCase):
    """/song 管理命令：set / gset / greset 的权限与行为"""

    def _text(self, result):
        if not result:
            return ""
        chain = result if isinstance(result, list) else getattr(result, "chain", [])
        return "".join(c.text for c in chain if hasattr(c, "text"))

    def test_song_admin_denied_for_non_admin(self):
        p = make_plugin()
        ev = FakeEvent(sender_id="u1", is_admin=False, message_str="song set search_limit 3")
        result = asyncio.run(p.song_admin(ev))
        self.assertIn("只有管理员", self._text(result))

    def test_set_global_updates_config(self):
        p = make_plugin()
        ev = FakeEvent(sender_id="u1", is_admin=True, message_str="song set search_limit 3")
        result = asyncio.run(p.song_admin(ev))
        self.assertIn("已更新全局 search_limit = 3", self._text(result))
        self.assertEqual(p.config["search_limit"], 3)

    def test_set_unknown_key_rejected(self):
        p = make_plugin()
        ev = FakeEvent(sender_id="u1", is_admin=True, message_str="song set not_a_key 1")
        result = asyncio.run(p.song_admin(ev))
        self.assertIn("未知配置项", self._text(result))

    def test_set_numeric_key_non_number_rejected(self):
        p = make_plugin()
        ev = FakeEvent(sender_id="u1", is_admin=True, message_str="song set search_limit abc")
        result = asyncio.run(p.song_admin(ev))
        self.assertIn("需要数字", self._text(result))

    def test_gset_per_group(self):
        p = make_plugin(search_limit=5)
        ev = FakeEvent(sender_id="u1", is_admin=True, message_str="song gset search_limit 2", group_id="g1")
        result = asyncio.run(p.song_admin(ev))
        self.assertIn("已设置本群 search_limit = 2", self._text(result))
        self.assertEqual(p.groups.get_key("g1", "search_limit"), 2)
        # 全局不受影响
        self.assertEqual(p.config["search_limit"], 5)

    def test_greset_single_key(self):
        p = make_plugin(search_limit=5)
        p.groups.set_key("g1", "search_limit", 2)
        ev = FakeEvent(sender_id="u1", is_admin=True, message_str="song greset search_limit", group_id="g1")
        result = asyncio.run(p.song_admin(ev))
        self.assertIn("已清除", self._text(result))
        self.assertIsNone(p.groups.get_key("g1", "search_limit"))

    def test_greset_all(self):
        p = make_plugin()
        p.groups.set_key("g1", "search_limit", 2)
        p.groups.set_key("g1", "daily_limit", 9)
        ev = FakeEvent(sender_id="u1", is_admin=True, message_str="song greset", group_id="g1")
        result = asyncio.run(p.song_admin(ev))
        self.assertIn("已清除", self._text(result))
        self.assertEqual(p.groups.data.get("g1"), None)

    def test_block_unblock_flow(self):
        p = make_plugin()
        ev = FakeEvent(sender_id="u1", is_admin=True, message_str="song block 违禁词")
        result = asyncio.run(p.song_admin(ev))
        self.assertIn("已屏蔽词", self._text(result))
        # 重复屏蔽返回已在列表
        result2 = asyncio.run(p.song_admin(ev))
        self.assertIn("已在屏蔽列表", self._text(result2))
        ev3 = FakeEvent(sender_id="u1", is_admin=True, message_str="song unblock 违禁词")
        result3 = asyncio.run(p.song_admin(ev3))
        self.assertIn("已解除屏蔽", self._text(result3))


if __name__ == "__main__":
    unittest.main()
