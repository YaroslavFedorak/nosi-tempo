from __future__ import annotations

import re
from datetime import datetime

import aiohttp

from .config import Settings
from .db import Store
from .fmt import WEEKDAYS_SHORT, duration, task_meta
from .parse import parse_task

LABELS = {"gemini": "Gemini", "claude": "Claude", "openai": "ChatGPT"}
KEY_HELP = {
    "gemini": ("aistudio.google.com/apikey", "безкоштовний ліміт"),
    "claude": ("console.anthropic.com/settings/keys", "оплата за використання"),
    "openai": ("platform.openai.com/api-keys", "оплата за використання"),
}
# асистент додає задачі рядками «ADD: ...», ми їх парсимо і прибираємо з відповіді
RX_ADD = re.compile(r"^\s*ADD:\s*(.+?)\s*$", re.MULTILINE)


class AIError(Exception):
    pass


async def pick_provider(store: Store, user_id: int) -> str | None:
    available = await store.ai_key_providers(user_id)
    preferred = await store.ai_provider(user_id)
    if preferred in available:
        return preferred
    return available[0] if available else None


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


SYSTEM = """Ти — помічник у Telegram-боті Nosi Tempo: задачі та облік часу користувача.
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


# модель перевантажена або не відповіла вчасно — варто спробувати наступну
class Busy(Exception):
    pass


ATTEMPT_TIMEOUT = aiohttp.ClientTimeout(total=25)
RETRY_STATUSES = {429, 500, 502, 503, 504}


async def _post(session: aiohttp.ClientSession, label: str, url: str, headers: dict, body: dict) -> dict:
    try:
        async with session.post(url, headers=headers, json=body, timeout=ATTEMPT_TIMEOUT) as r:
            data = await r.json(content_type=None)
            if r.status in RETRY_STATUSES:
                raise Busy()
            if r.status >= 400:
                err = data.get("error", {}) if isinstance(data, dict) else {}
                raise AIError(f"{label}: {err.get('message', r.status) if isinstance(err, dict) else err}")
            return data
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise Busy() from exc


async def _claude(s: Settings, model: str, key: str, session, system: str, msgs: list[dict]) -> str:
    data = await _post(
        session,
        "Claude",
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
        {"model": model, "max_tokens": 1500, "system": system, "messages": msgs},
    )
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


async def _openai(s: Settings, model: str, key: str, session, system: str, msgs: list[dict]) -> str:
    data = await _post(
        session,
        "ChatGPT",
        f"{s.openai_base_url}/chat/completions",
        {"Authorization": f"Bearer {key}"},
        {"model": model, "messages": [{"role": "system", "content": system}, *msgs]},
    )
    return data["choices"][0]["message"]["content"] or ""


async def _gemini(s: Settings, model: str, key: str, session, system: str, msgs: list[dict]) -> str:
    contents = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
        for m in msgs
    ]
    data = await _post(
        session,
        "Gemini",
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": key},
        {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents},
    )
    candidates = data.get("candidates") or []
    if not candidates:
        raise Busy()
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


CALLS = {"claude": _claude, "openai": _openai, "gemini": _gemini}


def models_for(s: Settings, provider: str) -> list[str]:
    raw = {"claude": s.anthropic_model, "openai": s.openai_model, "gemini": s.gemini_model}[provider]
    return [m.strip() for m in raw.split(",") if m.strip()]


def _check_request(s: Settings, provider: str, key: str) -> tuple[str, dict]:
    if provider == "gemini":
        return "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1", {"x-goog-api-key": key}
    if provider == "claude":
        return "https://api.anthropic.com/v1/models?limit=1", {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return f"{s.openai_base_url}/models", {"Authorization": f"Bearer {key}"}


# True — ключ робочий, False — провайдер його відхилив, None — перевірити не вдалося (мережа, ліміти)
async def check_key(s: Settings, provider: str, key: str) -> bool | None:
    url, headers = _check_request(s, provider, key)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(url, headers=headers) as r:
                if r.status in (400, 401, 403):
                    return False
                return r.status < 300 or None
    except (aiohttp.ClientError, TimeoutError):
        return None


async def ask(settings: Settings, store: Store, user_id: int, message: str) -> dict:
    name = await pick_provider(store, user_id)
    key = await store.ai_key(user_id, name) if name else None
    if not name or not key:
        raise AIError("Асистент не підключений. Додай свій ключ командою /ai.")
    history = await store.ai_history(user_id, 12)
    msgs = [{"role": h["role"], "content": h["content"]} for h in history]
    msgs.append({"role": "user", "content": message})
    system = SYSTEM.format(context=await build_context(store, user_id))

    reply = None
    async with aiohttp.ClientSession() as session:
        # моделі з налаштувань пробуємо по черзі, поки одна не відповість
        for model in models_for(settings, name):
            try:
                reply = (await CALLS[name](settings, model, key, session, system, msgs)).strip()
                break
            except Busy:
                continue
    if reply is None:
        raise AIError(f"{LABELS[name]} зараз перевантажений і не відповідає. Спробуй за кілька хвилин.")

    added = []
    for line in RX_ADD.findall(reply):
        added.append(await store.add_task(user_id, parse_task(line, store.today())))
    visible = RX_ADD.sub("", reply).strip() or "Готово."

    await store.ai_remember(user_id, "user", message)
    await store.ai_remember(user_id, "assistant", reply)
    return {"provider": name, "label": LABELS[name], "reply": visible, "added": added}
