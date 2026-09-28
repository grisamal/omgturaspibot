import aiosqlite
import os
import json
import time
import logging
from config import DB_PATH

log = logging.getLogger(__name__)


class Database:
    def __init__(self, db: aiosqlite.Connection):
        self._db = db
        # L1 in-memory cache for ultra-fast schedule retrieval: (group_id, date) -> (timestamp, lessons)
        self._l1_cache: dict[tuple[int, str], tuple[float, list[dict]]] = {}

    @classmethod
    async def create(cls) -> "Database":
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        # High-performance concurrent SQLite settings
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA temp_store=MEMORY")
        await db.execute("PRAGMA cache_size=-10000")
        inst = cls(db)
        await inst._init_tables()
        return inst

    async def _init_tables(self):
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                group_id INTEGER,
                group_name TEXT,
                subgroup INTEGER DEFAULT 1,
                notify_changes INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS schedule_cache (
                group_id INTEGER,
                date TEXT,
                lessons_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, date)
            );

            CREATE INDEX IF NOT EXISTS idx_users_group ON users(group_id);
            CREATE INDEX IF NOT EXISTS idx_cache_date ON schedule_cache(date);
        """)
        await self._db.commit()

    # ---------- User management ----------
    async def get_or_create_user(self, user_id: int, username: str = "", first_name: str = "") -> dict:
        cur = await self._db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if not row:
            await self._db.execute(
                """INSERT INTO users(user_id, username, first_name)
                   VALUES(?, ?, ?)""",
                (user_id, username, first_name)
            )
            await self._db.commit()
            cur = await self._db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
        else:
            await self._db.execute(
                "UPDATE users SET username = ?, first_name = ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?",
                (username, first_name, user_id)
            )
            await self._db.commit()
        return dict(row)

    async def get_user(self, user_id: int) -> dict | None:
        cur = await self._db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_last_active(self, user_id: int):
        await self._db.execute(
            "UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE user_id = ?",
            (user_id,)
        )
        await self._db.commit()

    async def set_user_group(self, user_id: int, group_id: int, group_name: str):
        await self._db.execute(
            "UPDATE users SET group_id = ?, group_name = ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?",
            (group_id, group_name, user_id)
        )
        await self._db.commit()

    async def set_user_subgroup(self, user_id: int, subgroup: int):
        await self._db.execute(
            "UPDATE users SET subgroup = ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?",
            (subgroup, user_id)
        )
        await self._db.commit()

    async def set_user_notify(self, user_id: int, notify: int):
        await self._db.execute(
            "UPDATE users SET notify_changes = ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?",
            (notify, user_id)
        )
        await self._db.commit()

    async def get_all_users(self) -> list[dict]:
        cur = await self._db.execute("SELECT * FROM users ORDER BY created_at DESC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_users_by_group(self, group_id: int) -> list[dict]:
        cur = await self._db.execute("SELECT * FROM users WHERE group_id = ?", (group_id,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_distinct_groups(self) -> list[dict]:
        cur = await self._db.execute("""
            SELECT group_id, group_name, COUNT(*) as user_count
            FROM users
            WHERE group_id IS NOT NULL
            GROUP BY group_id
            ORDER BY user_count DESC
        """)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ---------- Schedule cache ----------
    async def get_cached_schedule(self, group_id: int, date_str: str) -> list[dict] | None:
        now = time.time()
        cached = self._l1_cache.get((group_id, date_str))
        if cached:
            ts, data = cached
            if now - ts < 300:  # 5 minutes TTL in RAM
                return data

        cur = await self._db.execute(
            "SELECT lessons_json FROM schedule_cache WHERE group_id = ? AND date = ?",
            (group_id, date_str)
        )
        row = await cur.fetchone()
        if row and row["lessons_json"]:
            try:
                lessons = json.loads(row["lessons_json"])
                # Store in RAM cache
                self._l1_cache[(group_id, date_str)] = (now, lessons)
                # Prune if too large
                if len(self._l1_cache) > 500:
                    cutoff = now - 300
                    self._l1_cache = {k: v for k, v in self._l1_cache.items() if v[0] > cutoff}
                return lessons
            except Exception as e:
                log.error("Cache json error: %s", e)
        return None

    async def set_cached_schedule(self, group_id: int, date_str: str, lessons: list[dict]):
        self._l1_cache[(group_id, date_str)] = (time.time(), lessons)
        lessons_json = json.dumps(lessons, ensure_ascii=False)
        await self._db.execute(
            """INSERT OR REPLACE INTO schedule_cache(group_id, date, lessons_json, updated_at)
               VALUES(?, ?, ?, CURRENT_TIMESTAMP)""",
            (group_id, date_str, lessons_json)
        )
        await self._db.commit()

    # ---------- Admin statistics ----------
    async def get_stats(self) -> dict:
        cur = await self._db.execute("SELECT COUNT(*) as total_users FROM users")
        total_users = (await cur.fetchone())["total_users"]

        cur = await self._db.execute("""
            SELECT COUNT(*) as active_today FROM users
            WHERE datetime(last_active) >= datetime('now', '-1 day')
        """)
        active_today = (await cur.fetchone())["active_today"]

        groups = await self.get_distinct_groups()

        return {
            "total_users": total_users,
            "active_today": active_today,
            "total_groups": len(groups),
            "top_groups": groups[:5]
        }

    async def close(self):
        await self._db.close()
