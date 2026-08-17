"""点歌统计持久化（plugin_data/astrbot_plugin_music_custom/stats.json）
支持总榜 / 周榜 / 月榜 / 个人榜 / 用户榜，按日聚合时间维度。
"""

import json
import os
import threading
import time
from datetime import date, timedelta


class MusicStats:
    """歌曲/用户点歌统计，带线程锁的 JSON 持久化"""

    def __init__(self, data_dir: str) -> None:
        self.path = os.path.join(data_dir, "astrbot_plugin_music_custom", "stats.json")
        self._lock = threading.Lock()
        self.data: dict = {"songs": {}, "users": {}}
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
        except Exception:
            self.data = {"songs": {}, "users": {}}
        # 兼容旧结构
        self.data.setdefault("songs", {})
        self.data.setdefault("users", {})

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    @staticmethod
    def _since_date(days: int) -> str:
        return (date.today() - timedelta(days=days)).isoformat()

    def record(self, item, user_id: str, user_name: str = "", group_id: str = "") -> None:
        with self._lock:
            today = self._today()
            songs = self.data["songs"]
            key = item.key
            entry = songs.get(key) or {
                "title": item.title, "artist": item.artist, "source": item.source,
                "count": 0, "last_at": 0, "daily": {}, "groups": {}, "users": {},
            }
            entry["count"] += 1
            entry["last_at"] = int(time.time())
            daily = entry.setdefault("daily", {})
            daily[today] = daily.get(today, 0) + 1
            if group_id:
                entry.setdefault("groups", {})[group_id] = entry.get("groups", {}).get(group_id, 0) + 1
            entry.setdefault("users", {})[user_id] = entry.get("users", {}).get(user_id, 0) + 1
            songs[key] = entry

            u = self.data["users"].get(user_id) or {
                "name": user_name, "count": 0, "daily": {},
            }
            u["name"] = user_name or u.get("name", "")
            u["count"] += 1
            u.setdefault("daily", {})[today] = u.get("daily", {}).get(today, 0) + 1
            self.data["users"][user_id] = u
            self.save()

    @staticmethod
    def _score(entry: dict, since: str) -> int:
        """窗口内点播次数：优先 daily 精确统计，旧数据回退 last_at 近似"""
        daily = entry.get("daily")
        if daily:
            return sum(n for d, n in daily.items() if d >= since)
        if since and int(entry.get("last_at", 0)) < time.time() - 7 * 86400:
            return 0
        return int(entry.get("count", 0))

    def top_songs(self, limit: int = 10, days: int = 0) -> list[dict]:
        """days=0 表示全部；否则按近 N 天统计"""
        since = "" if not days else self._since_date(days)
        with self._lock:
            ranked = sorted(
                self.data["songs"].values(),
                key=lambda x: self._score(x, since),
                reverse=True,
            )
            out = []
            for e in ranked:
                s = self._score(e, since)
                if s <= 0:
                    continue
                out.append({**e, "score": s})
                if len(out) >= limit:
                    break
            return out

    def top_songs_by_user(self, user_id: str, limit: int = 10, days: int = 0) -> list[dict]:
        since = "" if not days else self._since_date(days)
        with self._lock:
            rows = []
            for e in self.data["songs"].values():
                cnt = e.get("users", {}).get(user_id, 0)
                if days:
                    # 个人维度的天级明细未记录，用歌曲总 daily 中该用户的占比近似
                    total_daily = sum(e.get("daily", {}).get(d, 0) for d in e.get("daily", {}) if d >= since)
                    if total_daily and cnt:
                        cnt = max(1, round(cnt * total_daily / e.get("count", 1)))
                if cnt > 0:
                    rows.append({**e, "score": cnt, "count": cnt})
            rows.sort(key=lambda x: x["score"], reverse=True)
            return rows[:limit]

    def top_songs_by_group(self, group_id: str, limit: int = 10, days: int = 0) -> list[dict]:
        """群内排行：基于 songs 条目中的 groups 字段（精确群维度）"""
        since = "" if not days else self._since_date(days)
        with self._lock:
            rows = []
            for e in self.data["songs"].values():
                cnt = self._group_count(e, group_id, since)
                if cnt <= 0:
                    continue
                rows.append({**e, "score": cnt, "count": cnt})
            rows.sort(key=lambda x: x["score"], reverse=True)
            return rows[:limit]

    @staticmethod
    def _group_count(entry: dict, group_id: str, since: str) -> int:
        """群维度窗口内点播次数：优先 daily 精确统计 + 群占比近似"""
        cnt = int(entry.get("groups", {}).get(group_id, 0))
        if cnt <= 0:
            return 0
        if since:
            total_daily = sum(
                entry.get("daily", {}).get(d, 0) for d in entry.get("daily", {}) if d >= since
            )
            if not total_daily:
                return 0
            return max(1, round(cnt * total_daily / max(1, entry.get("count", 1))))
        return cnt

    def group_totals(self, group_id: str, days: int = 7) -> int:
        """群维度窗口内点播总次数"""
        since = "" if not days else self._since_date(days)
        with self._lock:
            return sum(self._group_count(e, group_id, since) for e in self.data["songs"].values())

    def active_groups(self) -> list[str]:
        """返回所有出现过点歌记录的群 id（用于周报自动选择目标群）"""
        with self._lock:
            gs = set()
            for e in self.data["songs"].values():
                gs.update(str(g) for g in e.get("groups", {}).keys())
            return sorted(g for g in gs if g.isdigit())

    def top_users(self, limit: int = 10, days: int = 0) -> list[dict]:
        since = "" if not days else self._since_date(days)
        with self._lock:
            ranked = []
            for uid, e in self.data["users"].items():
                daily = e.get("daily")
                score = sum(n for d, n in daily.items() if d >= since) if (since and daily) else int(e.get("count", 0))
                if score <= 0:
                    continue
                ranked.append({"user_id": uid, "name": e.get("name", ""), "score": score})
            ranked.sort(key=lambda x: x["score"], reverse=True)
            return ranked[:limit]

    def find_songs(self, keyword: str, limit: int = 5) -> list[dict]:
        """按标题/歌手模糊搜索点歌记录（用于「统计 歌 <关键词>」查询单曲详情）"""
        kw = (keyword or "").strip().lower()
        with self._lock:
            if not kw:
                return []
            hits = []
            for e in self.data["songs"].values():
                if kw in str(e.get("title", "")).lower() or kw in str(e.get("artist", "")).lower():
                    hits.append(e)
                if len(hits) >= limit:
                    break
            return hits
