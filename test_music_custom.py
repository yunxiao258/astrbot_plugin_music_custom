# -*- coding: utf-8 -*-
"""music_custom 插件单元测试：配置读取、管理员判定、限频、结果合并去重、缓存清理"""
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
    def __init__(self, sender_id="", is_admin=False):
        self._sender_id = sender_id
        self._is_admin = is_admin

    def get_sender_id(self):
        return self._sender_id

    def is_admin(self):
        return self._is_admin

    def chain_result(self, chain):
        return chain


def make_plugin(**overrides):
    cfg = {
        "frequency_seconds": 30,
        "daily_limit": 0,
        "admins": "",
        "search_limit": 5,
        "select_timeout": 30,
        "cache_max_mb": 200,
        "voice_mode": "off",
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
    )
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


if __name__ == "__main__":
    unittest.main()
