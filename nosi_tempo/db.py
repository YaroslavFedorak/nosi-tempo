"""SQLite storage. One connection for the whole process; every query is scoped to a user."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from .parse import Parsed, next_due

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    ai_provider TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    archived   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS projects_user ON projects(user_id);

CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    title      TEXT NOT NULL,
    notes      TEXT NOT NULL DEFAULT '',
    priority   INTEGER NOT NULL DEFAULT 4,
    due_date   TEXT,
    due_time   TEXT,
    recur      TEXT,
    reminded   INTEGER NOT NULL DEFAULT 0,
    done_at    INTEGER,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_open ON tasks(user_id, done_at, due_date);

CREATE TABLE IF NOT EXISTS entries (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id    INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    title      TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    stopped_at INTEGER
);
CREATE INDEX IF NOT EXISTS entries_time ON entries(user_id, started_at);
CREATE UNIQUE INDEX IF NOT EXISTS entries_running ON entries(user_id) WHERE stopped_at IS NULL;

CREATE TABLE IF NOT EXISTS ai_messages (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""

TASK_COLUMNS = """
    t.id, t.title, t.notes, t.priority, t.due_date, t.due_time, t.recur, t.done_at,
    t.project_id, p.name AS project,
    COALESCE((SELECT SUM(COALESCE(e.stopped_at, :now) - e.started_at)
              FROM entries e WHERE e.task_id = t.id), 0) AS tracked
"""

ENTRY_COLUMNS = """
    e.id, e.title, e.started_at, e.stopped_at, e.task_id, e.project_id, p.name AS project
"""

EDITABLE = {"title", "notes", "priority", "due_date", "due_time", "recur", "project_id"}


def now() -> int:
    return int(time.time())


class Store:
    def __init__(self, path: Path, tz: ZoneInfo):
        self.path = path
        self.tz = tz
        self.db: aiosqlite.Connection | None = None

    # -- lifecycle -----------------------------------------------------------

    async def open(self) -> None:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    async def _all(self, sql: str, params: Any = ()) -> list[dict]:
        async with self.db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def _one(self, sql: str, params: Any = ()) -> dict | None:
        async with self.db.execute(sql, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def _run(self, sql: str, params: Any = ()) -> int:
        cur = await self.db.execute(sql, params)
        await self.db.commit()
        return cur.lastrowid

    def today(self) -> date:
        return datetime.now(self.tz).date()

    def local(self, ts: int) -> datetime:
        return datetime.fromtimestamp(ts, self.tz)

    def day_start(self, d: date) -> int:
        return int(datetime(d.year, d.month, d.day, tzinfo=self.tz).timestamp())

    # -- users ---------------------------------------------------------------

    async def ensure_user(self, user_id: int, name: str) -> None:
        await self._run(
            "INSERT INTO users (id, name, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
            (user_id, name, now()),
        )

    async def ai_provider(self, user_id: int) -> str | None:
        row = await self._one("SELECT ai_provider FROM users WHERE id = ?", (user_id,))
        return row["ai_provider"] if row else None

    async def set_ai_provider(self, user_id: int, provider: str) -> None:
        await self._run("UPDATE users SET ai_provider = ? WHERE id = ?", (provider, user_id))

    # -- projects ------------------------------------------------------------

    async def projects(self, user_id: int) -> list[dict]:
        rows = await self._all(
            """
            SELECT p.id, p.name,
                   (SELECT COUNT(*) FROM tasks t
                    WHERE t.project_id = p.id AND t.done_at IS NULL) AS open
            FROM projects p
            WHERE p.user_id = ? AND p.archived = 0
            """,
            (user_id,),
        )
        return sorted(rows, key=lambda r: r["name"].casefold())

    async def project_id(self, user_id: int, name: str | None, create: bool = True) -> int | None:
        if not name:
            return None
        # SQLite's NOCASE folds ASCII only, so Cyrillic names are matched here
        key = name.strip().casefold()
        rows = await self._all("SELECT id, name, archived FROM projects WHERE user_id = ?", (user_id,))
        row = next((r for r in rows if r["name"].casefold() == key), None)
        if row:
            if row["archived"]:
                await self._run("UPDATE projects SET archived = 0 WHERE id = ?", (row["id"],))
            return row["id"]
        if not create:
            return None
        return await self._run(
            "INSERT INTO projects (user_id, name, created_at) VALUES (?, ?, ?)",
            (user_id, name.strip()[:60], now()),
        )

    async def archive_project(self, user_id: int, project_id: int) -> None:
        await self._run(
            "UPDATE projects SET archived = 1 WHERE id = ? AND user_id = ?", (project_id, user_id)
        )

    # -- tasks ---------------------------------------------------------------

    async def add_task(self, user_id: int, parsed: Parsed, notes: str = "") -> dict:
        project_id = await self.project_id(user_id, parsed.project)
        task_id = await self._run(
            """
            INSERT INTO tasks (user_id, project_id, title, notes, priority,
                               due_date, due_time, recur, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                project_id,
                parsed.title[:300],
                notes,
                parsed.priority,
                parsed.due_date.isoformat() if parsed.due_date else None,
                parsed.due_time,
                parsed.recur,
                now(),
            ),
        )
        return await self.task(user_id, task_id)

    async def task(self, user_id: int, task_id: int) -> dict | None:
        return await self._one(
            f"""
            SELECT {TASK_COLUMNS}
            FROM tasks t LEFT JOIN projects p ON p.id = t.project_id
            WHERE t.id = :id AND t.user_id = :uid
            """,
            {"id": task_id, "uid": user_id, "now": now()},
        )

    async def tasks(self, user_id: int, view: str = "today", project_id: int | None = None) -> list[dict]:
        today = self.today().isoformat()
        params: dict[str, Any] = {"uid": user_id, "now": now(), "today": today}
        where = "t.user_id = :uid AND t.done_at IS NULL"
        if view == "today":
            where += " AND t.due_date IS NOT NULL AND t.due_date <= :today"
        elif view == "upcoming":
            params["until"] = (self.today() + timedelta(days=14)).isoformat()
            where += " AND t.due_date > :today AND t.due_date <= :until"
        elif view == "inbox":
            where += " AND t.project_id IS NULL"
        elif view == "project":
            params["pid"] = project_id
            where += " AND t.project_id = :pid"
        elif view == "all":
            pass
        elif view == "done":
            params["since"] = now() - 7 * 86400
            where = "t.user_id = :uid AND t.done_at >= :since"
        return await self._all(
            f"""
            SELECT {TASK_COLUMNS}
            FROM tasks t LEFT JOIN projects p ON p.id = t.project_id
            WHERE {where}
            ORDER BY {"t.done_at DESC" if view == "done" else
                      "t.due_date IS NULL, t.due_date, t.due_time IS NULL, t.due_time, t.priority, t.id"}
            LIMIT 200
            """,
            params,
        )

    async def update_task(self, user_id: int, task_id: int, **fields: Any) -> dict | None:
        if "project" in fields:
            fields["project_id"] = await self.project_id(user_id, fields.pop("project"))
        fields = {k: v for k, v in fields.items() if k in EDITABLE}
        if not fields:
            return await self.task(user_id, task_id)
        if {"due_date", "due_time"} & fields.keys():
            fields["reminded"] = 0
        cols = ", ".join(f"{k} = :{k}" for k in fields)
        await self._run(
            f"UPDATE tasks SET {cols} WHERE id = :id AND user_id = :uid",
            {**fields, "id": task_id, "uid": user_id},
        )
        return await self.task(user_id, task_id)

    async def complete_task(self, user_id: int, task_id: int) -> dict | None:
        """Ticks a task off. A repeating one moves to its next date and stays open."""
        task = await self.task(user_id, task_id)
        if not task or task["done_at"]:
            return task
        if task["recur"]:
            base = date.fromisoformat(task["due_date"]) if task["due_date"] else self.today()
            nxt = next_due(base, task["recur"], self.today())
            await self._run(
                "UPDATE tasks SET due_date = ?, reminded = 0 WHERE id = ?",
                (nxt.isoformat(), task_id),
            )
        else:
            await self._run("UPDATE tasks SET done_at = ? WHERE id = ?", (now(), task_id))
        return await self.task(user_id, task_id)

    async def reopen_task(self, user_id: int, task_id: int) -> dict | None:
        await self._run(
            "UPDATE tasks SET done_at = NULL WHERE id = ? AND user_id = ?", (task_id, user_id)
        )
        return await self.task(user_id, task_id)

    async def delete_task(self, user_id: int, task_id: int) -> None:
        await self._run("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))

    async def due_reminders(self) -> list[dict]:
        """Open tasks with a due time that has passed and no reminder sent yet."""
        current = datetime.now(self.tz)
        rows = await self._all(
            """
            SELECT t.id, t.user_id, t.title, t.due_date, t.due_time
            FROM tasks t
            WHERE t.done_at IS NULL AND t.reminded = 0
              AND t.due_time IS NOT NULL AND t.due_date IS NOT NULL
              AND t.due_date <= ?
            """,
            (current.date().isoformat(),),
        )
        due = []
        for r in rows:
            at = datetime.fromisoformat(f"{r['due_date']}T{r['due_time']}").replace(tzinfo=self.tz)
            if at <= current:
                due.append(r)
        return due

    async def mark_reminded(self, task_id: int) -> None:
        await self._run("UPDATE tasks SET reminded = 1 WHERE id = ?", (task_id,))

    # -- time entries --------------------------------------------------------

    async def running(self, user_id: int) -> dict | None:
        return await self._one(
            f"""
            SELECT {ENTRY_COLUMNS}
            FROM entries e LEFT JOIN projects p ON p.id = e.project_id
            WHERE e.user_id = ? AND e.stopped_at IS NULL
            """,
            (user_id,),
        )

    async def start(
        self,
        user_id: int,
        title: str = "",
        task_id: int | None = None,
        project: str | None = None,
        project_id: int | None = None,
    ) -> dict:
        """Starts a timer. Whatever was running stops at the same second."""
        stamp = now()
        await self.stop(user_id, stamp)
        if task_id:
            task = await self.task(user_id, task_id)
            if task:
                title = title or task["title"]
                project_id = project_id or task["project_id"]
        if project and not project_id:
            project_id = await self.project_id(user_id, project)
        await self._run(
            "INSERT INTO entries (user_id, task_id, project_id, title, started_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, task_id, project_id, (title or "Без назви")[:200], stamp),
        )
        return await self.running(user_id)

    async def stop(self, user_id: int, stamp: int | None = None) -> dict | None:
        entry = await self.running(user_id)
        if not entry:
            return None
        stamp = stamp or now()
        await self._run("UPDATE entries SET stopped_at = ? WHERE id = ?", (stamp, entry["id"]))
        entry["stopped_at"] = stamp
        return entry

    async def log(
        self, user_id: int, title: str, seconds: int, project: str | None = None, ended_at: int | None = None
    ) -> dict:
        """A manual entry that ends now (or at ended_at) and lasted `seconds`."""
        end = ended_at or now()
        project_id = await self.project_id(user_id, project)
        entry_id = await self._run(
            "INSERT INTO entries (user_id, project_id, title, started_at, stopped_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, project_id, (title or "Без назви")[:200], end - seconds, end),
        )
        return await self._one(
            f"SELECT {ENTRY_COLUMNS} FROM entries e LEFT JOIN projects p ON p.id = e.project_id WHERE e.id = ?",
            (entry_id,),
        )

    async def delete_entry(self, user_id: int, entry_id: int) -> None:
        await self._run("DELETE FROM entries WHERE id = ? AND user_id = ?", (entry_id, user_id))

    async def entries(self, user_id: int, since: int, until: int | None = None) -> list[dict]:
        return await self._all(
            f"""
            SELECT {ENTRY_COLUMNS}
            FROM entries e LEFT JOIN projects p ON p.id = e.project_id
            WHERE e.user_id = ? AND e.started_at < ? AND COALESCE(e.stopped_at, ?) > ?
            ORDER BY e.started_at DESC
            """,
            (user_id, until or now() + 1, now(), since),
        )

    async def recent_titles(self, user_id: int, limit: int = 5) -> list[dict]:
        return await self._all(
            """
            SELECT e.title, e.task_id, e.project_id, p.name AS project, MAX(e.started_at) AS last
            FROM entries e LEFT JOIN projects p ON p.id = e.project_id
            WHERE e.user_id = ? AND e.stopped_at IS NOT NULL
            GROUP BY e.title, e.project_id
            ORDER BY last DESC
            LIMIT ?
            """,
            (user_id, limit),
        )

    async def report(self, user_id: int, days: int = 7) -> dict:
        """Time per project and per day for the last `days` days, today included."""
        today = self.today()
        first = today - timedelta(days=days - 1)
        since = self.day_start(first)
        stamp = now()
        rows = await self.entries(user_id, since)

        by_project: dict[str, int] = {}
        by_task: dict[str, int] = {}
        by_day = {(first + timedelta(days=i)).isoformat(): 0 for i in range(days)}
        total = 0
        for e in rows:
            start = max(e["started_at"], since)
            end = e["stopped_at"] or stamp
            if end <= start:
                continue
            secs = end - start
            total += secs
            key = e["project"] or "Без проєкту"
            by_project[key] = by_project.get(key, 0) + secs
            by_task[e["title"]] = by_task.get(e["title"], 0) + secs
            # split across midnights so each day gets its own share
            cursor = start
            while cursor < end:
                day = self.local(cursor).date()
                next_midnight = self.day_start(day + timedelta(days=1))
                chunk = min(end, next_midnight) - cursor
                if day.isoformat() in by_day:
                    by_day[day.isoformat()] += chunk
                cursor += chunk

        def ranked(d: dict[str, int]) -> list[dict]:
            return [{"name": k, "seconds": v} for k, v in sorted(d.items(), key=lambda kv: -kv[1])]

        return {
            "days": days,
            "from": first.isoformat(),
            "to": today.isoformat(),
            "total": total,
            "by_project": ranked(by_project),
            "by_task": ranked(by_task)[:10],
            "by_day": [{"date": k, "seconds": v} for k, v in by_day.items()],
        }

    # -- assistant history ---------------------------------------------------

    async def ai_history(self, user_id: int, limit: int = 20) -> list[dict]:
        rows = await self._all(
            "SELECT role, content, created_at FROM ai_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return rows[::-1]

    async def ai_remember(self, user_id: int, role: str, content: str) -> None:
        await self._run(
            "INSERT INTO ai_messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, content, now()),
        )

    async def ai_forget(self, user_id: int) -> None:
        await self._run("DELETE FROM ai_messages WHERE user_id = ?", (user_id,))
