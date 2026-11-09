from __future__ import annotations

import re
from datetime import datetime

import aiohttp

from .config import Settings
from .db import Store
from .fmt import WEEKDAYS_SHORT, duration, task_meta
from .parse import parse_task

LABELS = {"gemini": "Gemini", "claude": "Claude", "openai": "ChatGPT"}
TIMEOUT = aiohttp.ClientTimeout(total=90)
# асистент додає задачі рядками «ADD: ...», ми їх парсимо і прибираємо з відповіді
RX_ADD = re.compile(r"^\s*ADD:\s*(.+?)\s*$", re.MULTILINE)


class AIError(Exception):
    pass


def pick_provider(settings: Settings, preferred: str | None) -> str:
    available = settings.ai_providers()
    if not available:
        raise AIError(
            "Жоден ключ ШІ не задано. Додай у .env GEMINI_API_KEY (безкоштовний), "
            "ANTHROPIC_API_KEY або OPENAI_API_KEY."
        )
    for name in (preferred, settings.ai_default):
        if name in available:
            return name
    return available[0]


async def build_context(store: Store, user_id: int) -> str:
    today = store.today()
    tasks = await store.tasks(user_id, "all")
    running = await store.running(user_id)
    week = await store.report(user_id, 7)

    lines = [
        f"Зараз {datetime.now(store.tz):%Y-%m-%d %H:%M} ({store.tz.key}), {WEEKDAYS_SHORT[today.weekday()]}.",
        "",
        f"Відкриті задачі ({len(tasks)}):",
    ]
    for t in tasks[:80]:
        meta = task_meta(t, today)
        lines.append(f"- [p{t['priority']}] {t['title']}" + (f" ({meta})" if meta else ""))
    if not tasks:
        lines.append("- немає")
    lines.append("")
    if running:
        elapsed = int(datetime.now().timestamp()) - running["started_at"]
        lines.append(f"Таймер іде: «{running['title']}», вже {duration(elapsed)}.")
    else:
        lines.append("Таймер зараз не запущено.")
    lines.append(f"Час за 7 днів: {duration(week['total'])}.")
    for row in week["by_project"][:8]:
        lines.append(f"- {row['name']}: {duration(row['seconds'])}")
    return "\n".join(lines)


SYSTEM = """Ти — помічник у застосунку Nosi Tempo: задачі та облік часу одного розробника.
Відповідай українською, коротко і по суті, без зайвих вступів. Форматування: звичайний текст,
можна короткі списки з дефісом. Не вигадуй задач чи цифр, яких немає в даних нижче.

Якщо користувач просить створити задачі (або план, який варто записати), додай у кінці
відповіді по одному рядку на задачу у форматі:
ADD: <назва> <дата/час словами> #<проєкт> !<1-4>
Наприклад: ADD: Підготувати звіт завтра о 10:00 #робота !2
Дата, проєкт і пріоритет необовʼязкові. Не пиши ADD-рядків, якщо про це не просили.

Дані користувача:
{context}
"""


async def _claude(s: Settings, session: aiohttp.ClientSession, system: str, msgs: list[dict]) -> str:
    async with session.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": s.anthropic_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"model": s.anthropic_model, "max_tokens": 1500, "system": system, "messages": msgs},
    ) as r:
        data = await r.json(content_type=None)
        if r.status >= 400:
            raise AIError(f"Claude: {data.get('error', {}).get('message', r.status)}")
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


async def _openai(s: Settings, session: aiohttp.ClientSession, system: str, msgs: list[dict]) -> str:
    async with session.post(
        f"{s.openai_base_url}/chat/completions",
        headers={"Authorization": f"Bearer {s.openai_key}"},
        json={"model": s.openai_model, "messages": [{"role": "system", "content": system}, *msgs]},
    ) as r:
        data = await r.json(content_type=None)
        if r.status >= 400:
            err = data.get("error", {})
            raise AIError(f"ChatGPT: {err.get('message', r.status) if isinstance(err, dict) else err}")
        return data["choices"][0]["message"]["content"] or ""


async def _gemini(s: Settings, session: aiohttp.ClientSession, system: str, msgs: list[dict]) -> str:
    contents = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
        for m in msgs
    ]
    async with session.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{s.gemini_model}:generateContent",
        headers={"x-goog-api-key": s.gemini_key},
        json={"systemInstruction": {"parts": [{"text": system}]}, "contents": contents},
    ) as r:
        data = await r.json(content_type=None)
        if r.status >= 400:
            raise AIError(f"Gemini: {data.get('error', {}).get('message', r.status)}")
        candidates = data.get("candidates") or []
        if not candidates:
            raise AIError("Gemini повернув порожню відповідь.")
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)


CALLS = {"claude": _claude, "openai": _openai, "gemini": _gemini}


async def ask(settings: Settings, store: Store, user_id: int, message: str, provider: str | None = None) -> dict:
    name = pick_provider(settings, provider or await store.ai_provider(user_id))
    history = await store.ai_history(user_id, 12)
    msgs = [{"role": h["role"], "content": h["content"]} for h in history]
    msgs.append({"role": "user", "content": message})
    system = SYSTEM.format(context=await build_context(store, user_id))

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            reply = (await CALLS[name](settings, session, system, msgs)).strip()
    except aiohttp.ClientError as exc:
        raise AIError(f"{LABELS[name]}: немає звʼязку ({exc.__class__.__name__}).") from exc
    except TimeoutError as exc:
        raise AIError(f"{LABELS[name]} не відповів за 90 секунд.") from exc

    added = []
    for line in RX_ADD.findall(reply):
        added.append(await store.add_task(user_id, parse_task(line, store.today())))
    visible = RX_ADD.sub("", reply).strip() or "Готово."

    await store.ai_remember(user_id, "user", message)
    await store.ai_remember(user_id, "assistant", reply)
    return {"provider": name, "label": LABELS[name], "reply": visible, "added": added}
