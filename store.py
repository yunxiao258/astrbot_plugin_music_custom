"""收藏 / 群独立配置 的 JSON 持久化存储"""

import dataclasses
import json
import os
import threading
import time

from .sources.base import MusicItem

# 每人收藏上限
FAV_LIMIT = 50


class JsonStore:
    """线程安全 + 原子写入的 JSON 文件存储基类"""

    def __init__(self, path: str, default: dict) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data: dict = default
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data = loaded
        except Exception:
            pass

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass


class Favorites(JsonStore):
    """favorites.json：user_id -> [MusicItem 字典列表]"""

    def __init__(self, data_dir: str) -> None:
        super().__init__(os.path.join(data_dir, "favorites.json"), {})

    @staticmethod
    def _to_dict(item: MusicItem) -> dict:
        return dataclasses.asdict(item)

    @staticmethod
    def _to_item(d: dict) -> MusicItem:
        try:
            return MusicItem(**d)
        except (TypeError, ValueError):
            return None

    def get_list(self, user_id: str) -> list[dict]:
        """返回收藏字典列表（用于展示）；损坏条目自动过滤"""
        with self._lock:
            raw = self.data.get(user_id) or []
            good = [d for d in raw if isinstance(d, dict) and d.get("title")]
            if len(good) != len(raw):
                self.data[user_id] = good
                self.save()
            return list(good)

    def items(self, user_id: str) -> list[MusicItem]:
        items = []
        for d in self.get_list(user_id):
            it = self._to_item(d)
            if it:
                items.append(it)
        return items

    def add(self, user_id: str, item: MusicItem) -> tuple[bool, str]:
        """收藏；返回 (是否成功, 说明)"""
        with self._lock:
            lst = self.data.setdefault(user_id, [])
            if any(d.get("source") == item.source and str(d.get("id")) == str(item.id) for d in lst):
                return False, "这首歌已经在你的收藏里啦～"
            if len(lst) >= FAV_LIMIT:
                return False, f"收藏已满（最多 {FAV_LIMIT} 首），请先删除一些～"
            lst.append(self._to_dict(item))
            self.save()
            return True, f"已收藏《{item.title}》- {item.artist}（共 {len(lst)} 首）"

    def remove(self, user_id: str, idx: int) -> bool:
        with self._lock:
            lst = self.data.get(user_id) or []
            if not 1 <= idx <= len(lst):
                return False
            del lst[idx - 1]
            self.save()
            return True


class GroupConfigs(JsonStore):
    """per_group.json：group_id -> {key: value} 群独立配置"""

    def __init__(self, data_dir: str) -> None:
        super().__init__(os.path.join(data_dir, "per_group.json"), {})

    def get_key(self, group_id: str, key: str):
        with self._lock:
            return (self.data.get(group_id) or {}).get(key)

    def set_key(self, group_id: str, key: str, value) -> None:
        with self._lock:
            self.data.setdefault(group_id, {})[key] = value
            self.save()

    def reset_key(self, group_id: str, key: str | None = None) -> bool:
        with self._lock:
            conf = self.data.get(group_id)
            if not conf:
                return False
            if key is None:
                self.data.pop(group_id, None)
            else:
                if key not in conf:
                    return False
                conf.pop(key, None)
                if not conf:
                    self.data.pop(group_id, None)
            self.save()
            return True


class QuotaStore(JsonStore):
    """daily_quota.json：user_id -> {date: 当日点歌次数}"""

    def __init__(self, data_dir: str) -> None:
        super().__init__(os.path.join(data_dir, "daily_quota.json"), {})

    def consume(self, user_id: str, today: str) -> int:
        """记录一次点歌，返回当日累计次数"""
        with self._lock:
            cnt = self.data.setdefault(user_id, {}).get(today, 0) + 1
            self.data[user_id][today] = cnt
            self.save()
            return cnt

    def used_today(self, user_id: str, today: str) -> int:
        with self._lock:
            return int(self.data.get(user_id, {}).get(today, 0))


class BlockedStore(JsonStore):
    """blocked.json：{terms: [屏蔽词]}，匹配歌曲标题/歌手"""

    def __init__(self, data_dir: str) -> None:
        super().__init__(os.path.join(data_dir, "blocked.json"), {"terms": []})

    def block(self, term: str) -> bool:
        with self._lock:
            terms = self.data.setdefault("terms", [])
            t = term.strip()
            if t and t not in terms:
                terms.append(t)
                self.save()
                return True
            return False

    def unblock(self, term: str) -> bool:
        with self._lock:
            terms = self.data.get("terms") or []
            t = term.strip()
            if t in terms:
                terms.remove(t)
                self.save()
                return True
            return False

    def list(self) -> list[str]:
        with self._lock:
            return list(self.data.get("terms") or [])

    def is_blocked(self, item) -> bool:
        """歌曲标题/歌手 是否命中屏蔽词"""
        terms = self.data.get("terms") or []
        if not terms:
            return False
        hay = f"{item.title} {item.artist}".lower()
        for t in terms:
            if t.lower() in hay:
                return True
        return False


class PushState(JsonStore):
    """push_state.json：{date_group: True} 记录每日定时推送状态，防重启重复推送"""

    def __init__(self, data_dir: str) -> None:
        super().__init__(os.path.join(data_dir, "push_state.json"), {})

    def already_pushed(self, key: str) -> bool:
        with self._lock:
            return bool(self.data.get(key))

    def mark_pushed(self, key: str) -> None:
        with self._lock:
            self.data[key] = True
            # 只保留最近 60 条，防止无限膨胀
            if len(self.data) > 60:
                for k in sorted(self.data)[: len(self.data) - 60]:
                    self.data.pop(k, None)
            self.save()
