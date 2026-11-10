import asyncio
from zoneinfo import ZoneInfo

from nosi_tempo import db
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
