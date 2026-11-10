import asyncio
import sqlite3
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet

from nosi_tempo import ai, db
from nosi_tempo.config import Settings
from nosi_tempo.db import Store
from nosi_tempo.parse import parse_task

UID = 42


def test_tasks_views_and_projects(tmp_path):
    async def go():
        s = Store(tmp_path / "a.db", ZoneInfo("Europe/Kyiv"))
        await s.open()
        await s.ensure_user(UID, "me")
        today = s.today()
        await s.add_task(UID, parse_task("сьогодні звіт #Робота !2", today))
        await s.add_task(UID, parse_task("хліб", today))
        await s.add_task(UID, parse_task("завтра ретро #робота", today))

        assert [t["title"] for t in await s.tasks(UID, "today")] == ["звіт"]
        assert [t["title"] for t in await s.tasks(UID, "inbox")] == ["хліб"]
        assert [t["title"] for t in await s.tasks(UID, "upcoming")] == ["ретро"]
        projects = await s.projects(UID)
        assert len(projects) == 1 and projects[0]["open"] == 2  # #Робота і #робота — один проєкт
        await s.close()

    asyncio.run(go())


def test_complete_plain_and_repeating(tmp_path):
    async def go():
        s = Store(tmp_path / "b.db", ZoneInfo("Europe/Kyiv"))
        await s.open()
        await s.ensure_user(UID, "me")
        today = s.today()
        once = await s.add_task(UID, parse_task("раз", today))
        daily = await s.add_task(UID, parse_task("щодня зарядка", today))

        done = await s.complete_task(UID, once["id"])
        assert done["done_at"]
        moved = await s.complete_task(UID, daily["id"])
        assert moved["done_at"] is None and moved["due_date"] > today.isoformat()

        assert (await s.reopen_task(UID, once["id"]))["done_at"] is None
        assert await s.task(UID + 1, once["id"]) is None  # чужі задачі не видно
        await s.close()

    asyncio.run(go())


def test_timer_and_report(tmp_path, monkeypatch):
    async def go():
        s = Store(tmp_path / "c.db", ZoneInfo("Europe/Kyiv"))
        await s.open()
        await s.ensure_user(UID, "me")
        clock = [1_800_000_000]
        monkeypatch.setattr(db, "now", lambda: clock[0])
        monkeypatch.setattr(s, "today", lambda: s.local(clock[0]).date())

        task = await s.add_task(UID, parse_task("код #api", s.today()))
        await s.start(UID, task_id=task["id"])
        clock[0] += 1500
        await s.start(UID, title="пошта")  # зупиняє попередній
        clock[0] += 300
        stopped = await s.stop(UID)
        assert stopped["title"] == "пошта"
        assert await s.running(UID) is None
        await s.log(UID, "дзвінок", 600, "api")

        r = await s.report(UID, 7)
        assert r["total"] == 2400
        assert {p["name"]: p["seconds"] for p in r["by_project"]} == {"api": 2100, "Без проєкту": 300}
        assert (await s.task(UID, task["id"]))["tracked"] == 1500
        await s.close()

    asyncio.run(go())


def test_ai_keys_are_encrypted_per_user(tmp_path):
    async def go():
        s = Store(tmp_path / "k.db", ZoneInfo("Europe/Kyiv"))
        await s.open()
        await s.ensure_user(UID, "me")
        await s.ensure_user(UID + 1, "other")

        assert not await s.ai_offered(UID)
        await s.mark_ai_offered(UID)
        assert await s.ai_offered(UID)

        await s.set_ai_key(UID, "gemini", "AIza-secret-value-123456")
        await s.set_ai_key(UID, "claude", "sk-ant-secret-value-123456")
        assert await s.ai_key(UID, "gemini") == "AIza-secret-value-123456"
        assert await s.ai_key(UID + 1, "gemini") is None
        assert await s.ai_provider(UID) == "claude"  # останній доданий стає активним

        raw = await s._one("SELECT secret FROM ai_keys WHERE provider = 'gemini'")
        assert b"secret-value" not in raw["secret"]

        await s.delete_ai_key(UID, "claude")
        assert await s.ai_key_providers(UID) == ["gemini"]
        assert await s.ai_provider(UID) == "gemini"
        await s.close()

    asyncio.run(go())


def test_wrong_secret_key_reads_as_missing(tmp_path):
    async def go():
        path = tmp_path / "w.db"
        s = Store(path, ZoneInfo("Europe/Kyiv"), Fernet.generate_key())
        await s.open()
        await s.ensure_user(UID, "me")
        await s.set_ai_key(UID, "gemini", "AIza-secret-value-123456")
        await s.close()

        s = Store(path, ZoneInfo("Europe/Kyiv"), Fernet.generate_key())
        await s.open()
        assert await s.ai_key(UID, "gemini") is None
        await s.close()

    asyncio.run(go())


def test_old_database_gets_new_column(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', ai_provider TEXT, created_at INTEGER NOT NULL)")
    con.execute("INSERT INTO users (id, name, created_at) VALUES (1, 'old', 0)")
    con.commit()
    con.close()

    async def go():
        s = Store(path, ZoneInfo("Europe/Kyiv"))
        await s.open()
        assert not await s.ai_offered(1)
        await s.close()

    asyncio.run(go())


def test_assistant_needs_a_key(tmp_path):
    async def go():
        s = Store(tmp_path / "a.db", ZoneInfo("Europe/Kyiv"))
        await s.open()
        await s.ensure_user(UID, "me")
        settings = Settings(
            bot_token="x", tz=s.tz, db_path=s.path, secret_key=b"",
            anthropic_model="", openai_model="", openai_base_url="", gemini_model="",
        )
        with pytest.raises(ai.AIError):
            await ai.ask(settings, s, UID, "привіт")
        await s.close()

    asyncio.run(go())
