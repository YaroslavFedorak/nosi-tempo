from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from html import escape
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)

from . import ai, fmt
from .config import Settings
from .db import Store, now
from .parse import RX_PROJECT, parse_duration, parse_task, recur_label

log = logging.getLogger(__name__)
router = Router()

BTN_TODAY, BTN_INBOX, BTN_NEXT = "Сьогодні", "Вхідні", "Наступні"
BTN_TIMER, BTN_REPORT, BTN_PROJECTS = "Таймер", "Звіт", "Проєкти"
MENU_BUTTONS = {BTN_TODAY, BTN_INBOX, BTN_NEXT, BTN_TIMER, BTN_REPORT, BTN_PROJECTS}
VIEW_TITLES = {"today": "Сьогодні", "inbox": "Вхідні", "upcoming": "Наступні 14 днів", "project": ""}

HELP = """<b>Nosi Tempo</b> — задачі й облік часу.

<b>Додати задачу</b> — просто напиши її:
<code>Подзвонити мамі завтра о 18:00 #дім !1</code>
<code>Звіт щоп'ятниці #робота</code>
<code>Спортзал кожні 2 дні</code>
Дати: сьогодні, завтра, пт, 12.11, через 3 дні. Повтори: щодня, по буднях, щотижня, щомісяця.

<b>Час</b>
/go назва #проєкт — запустити таймер
/stop — зупинити
/log 45m назва #проєкт — дописати час вручну
/report — звіт за тиждень
"""

HELP_AI_ON = """

<b>Асистент</b> — повідомлення зі знаком питання на початку:
<code>? що мені зробити першим сьогодні</code>
/ai — моделі й ключі, /forget — очистити памʼять розмови"""

HELP_AI_OFF = """

/ai — підключити асистента зі своїм API-ключем"""

OFFER = """Можна підключити асистента: він бачить твої задачі й час, підкаже, з чого почати, і сам розпише план.

Для цього потрібен твій власний API-ключ. У Gemini він безкоштовний. Ключ зберігається зашифрованим і використовується лише для твоїх запитів.

Не зараз — без проблем, підключити можна будь-коли командою /ai."""


class Users(BaseMiddleware):

    def __init__(self, store: Store):
        self.store = store

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return None
        await self.store.ensure_user(user.id, user.full_name or "")
        return await handler(event, data)


class ListCb(CallbackData, prefix="l"):
    view: str
    pid: int = 0


class TaskCb(CallbackData, prefix="t"):
    action: str
    id: int
    arg: str = ""
    view: str = "today"


class TimerCb(CallbackData, prefix="tm"):
    action: str
    n: int = 0


class AiCb(CallbackData, prefix="ai"):
    action: str
    provider: str = ""


class Rename(StatesGroup):
    title = State()


class AddKey(StatesGroup):
    key = State()


async def has_ai(store: Store, uid: int) -> bool:
    return bool(await store.ai_key_providers(uid))


def main_kb(ai_on: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_INBOX), KeyboardButton(text=BTN_NEXT)],
        [KeyboardButton(text=BTN_TIMER), KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_PROJECTS)],
    ]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Нова задача або ? питання" if ai_on else "Нова задача",
    )


def ikb(rows: list[list[tuple[str, CallbackData]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=cb.pack()) for text, cb in row] for row in rows if row
        ]
    )


def short(text: str, n: int = 30) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


async def render_list(store: Store, uid: int, view: str, pid: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    tasks = await store.tasks(uid, view, pid or None)
    today = store.today()
    title = VIEW_TITLES.get(view, "")
    if view == "project":
        projects = {p["id"]: p["name"] for p in await store.projects(uid)}
        title = f"#{projects.get(pid, 'проєкт')}"

    if not tasks:
        empty = {
            "today": "На сьогодні нічого. Напиши задачу — і вона тут зʼявиться, якщо додати «сьогодні».",
            "inbox": "Вхідні порожні. Будь-яке повідомлення без #проєкту потрапляє сюди.",
            "upcoming": "На найближчі два тижні нічого не заплановано.",
        }.get(view, "У проєкті немає відкритих задач.")
        text = f"<b>{escape(title)}</b>\n\n{empty}"
    else:
        lines = [fmt.task_line(t, today, i + 1) for i, t in enumerate(tasks[:30])]
        more = f"\n\n…і ще {len(tasks) - 30}" if len(tasks) > 30 else ""
        text = f"<b>{escape(title)}</b>  {len(tasks)}\n\n" + "\n".join(lines) + more

    rows: list[list[tuple[str, CallbackData]]] = []
    for i, t in enumerate(tasks[:15]):
        rows.append(
            [
                (f"{i + 1}. {short(t['title'], 26)}", TaskCb(action="open", id=t["id"], view=view)),
                ("Готово", TaskCb(action="ldone", id=t["id"], view=view, arg=str(pid))),
            ]
        )
    switch = [(name, ListCb(view=v)) for v, name in (("today", "Сьогодні"), ("inbox", "Вхідні"), ("upcoming", "Наступні")) if v != view]
    rows.append(switch)
    return text, ikb(rows)


def task_card(task: dict, today: date, view: str) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"<b>{escape(task['title'])}</b>"]
    if task["due_date"]:
        due = fmt.day(task["due_date"], today) + (f" о {task['due_time']}" if task["due_time"] else "")
        lines.append(f"Коли: {due}")
    if task["recur"]:
        lines.append(f"Повтор: {recur_label(task['recur'])}")
    lines.append(f"Проєкт: {'#' + escape(task['project']) if task['project'] else 'Вхідні'}")
    lines.append(f"Пріоритет: {task['priority']}")
    if task["tracked"]:
        lines.append(f"Витрачено: {fmt.duration(task['tracked'])}")
    if task["notes"]:
        lines.append(f"\n{escape(task['notes'])}")
    if task["done_at"]:
        lines.append("\nВиконано.")

    tid = task["id"]

    def cb(action: str, arg: str = "") -> TaskCb:
        return TaskCb(action=action, id=tid, arg=arg, view=view)

    first = [("Повернути", cb("undo"))] if task["done_at"] else [("Готово", cb("done")), ("Старт таймера", cb("go"))]
    rows = [
        first,
        [("Сьогодні", cb("due", "0")), ("Завтра", cb("due", "1")), ("+7 днів", cb("due", "7")), ("Без дати", cb("due", "x"))],
        [("Пріоритет " + str(p), cb("pri", str(p))) for p in (1, 2, 3, 4) if p != task["priority"]],
        [("Перейменувати", cb("ren")), ("Видалити", cb("del"))],
        [("До списку", ListCb(view=view if view in VIEW_TITLES else "today"))],
    ]
    return "\n".join(lines), ikb(rows)


async def render_timer(store: Store, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    running = await store.running(uid)
    report = await store.report(uid, 1)
    today_total = f"Сьогодні разом: {fmt.duration(report['total'])}"
    if running:
        elapsed = now() - running["started_at"]
        started = store.local(running["started_at"]).strftime("%H:%M")
        project = f"\n#{escape(running['project'])}" if running["project"] else ""
        text = (
            f"<b>{escape(running['title'])}</b>{project}\n\n"
            f"<code>{fmt.clock(elapsed)}</code>  з {started}\n\n{today_total}"
        )
        return text, ikb([[("Стоп", TimerCb(action="stop")), ("Оновити", TimerCb(action="show"))]])

    recent = await store.recent_titles(uid, 5)
    text = f"Таймер не запущено.\n{today_total}\n\nЗапусти: <code>/go назва #проєкт</code>"
    if recent:
        text += "\nабо продовж одне з останніх:"
    rows = [[(short(r["title"], 40), TimerCb(action="again", n=i))] for i, r in enumerate(recent)]
    return text, ikb(rows)


async def render_report(store: Store, uid: int, days: int = 7) -> str:
    r = await store.report(uid, days)
    if not r["total"]:
        return "За останні 7 днів часу не записано. Запусти таймер з картки задачі або через /go."
    top = r["by_project"][0]["seconds"]
    lines = [f"<b>Тиждень</b>  {fmt.duration(r['total'])}", "", "<b>Проєкти</b>", "<pre>"]
    for row in r["by_project"]:
        lines.append(f"{short(row['name'], 16):<16} {fmt.bar(row['seconds'], top, 10)} {fmt.duration(row['seconds'])}")
    lines += ["</pre>", "<b>Дні</b>", "<pre>"]
    peak = max(d["seconds"] for d in r["by_day"]) or 1
    for d in r["by_day"]:
        day = date.fromisoformat(d["date"])
        label = f"{fmt.WEEKDAYS_SHORT[day.weekday()]} {day.day:>2}"
        lines.append(f"{label}  {fmt.bar(d['seconds'], peak, 12)} {fmt.duration(d['seconds']) if d['seconds'] else ''}")
    lines.append("</pre>")
    if r["by_task"]:
        lines.append("<b>Найбільше часу</b>")
        for row in r["by_task"][:5]:
            lines.append(f"{escape(short(row['name'], 40))} — {fmt.duration(row['seconds'])}")
    return "\n".join(lines)


async def edit_or_send(cq: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None) -> None:
    try:
        await cq.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            await cq.message.answer(text, reply_markup=kb)


async def send_help(message: Message, store: Store) -> None:
    ai_on = await has_ai(store, message.from_user.id)
    await message.answer(HELP + (HELP_AI_ON if ai_on else HELP_AI_OFF), reply_markup=main_kb(ai_on))


def provider_rows(providers: list[str]) -> list[list[tuple[str, CallbackData]]]:
    return [[(f"Підключити {ai.LABELS[p]}", AiCb(action="add", provider=p))] for p in providers]


@router.message(CommandStart())
async def on_start(message: Message, state: FSMContext, store: Store) -> None:
    await state.clear()
    await send_help(message, store)
    uid = message.from_user.id
    if not await store.ai_offered(uid) and not await has_ai(store, uid):
        await store.mark_ai_offered(uid)
        rows = provider_rows(list(ai.LABELS)) + [[("Не зараз", AiCb(action="skip"))]]
        await message.answer(OFFER, reply_markup=ikb(rows))


@router.message(Command("help"))
async def on_help(message: Message, store: Store) -> None:
    await send_help(message, store)


@router.message(Command("today"))
@router.message(F.text == BTN_TODAY)
async def on_today(message: Message, store: Store) -> None:
    text, kb = await render_list(store, message.from_user.id, "today")
    await message.answer(text, reply_markup=kb)


@router.message(Command("inbox"))
@router.message(F.text == BTN_INBOX)
async def on_inbox(message: Message, store: Store) -> None:
    text, kb = await render_list(store, message.from_user.id, "inbox")
    await message.answer(text, reply_markup=kb)


@router.message(Command("upcoming"))
@router.message(F.text == BTN_NEXT)
async def on_upcoming(message: Message, store: Store) -> None:
    text, kb = await render_list(store, message.from_user.id, "upcoming")
    await message.answer(text, reply_markup=kb)


@router.message(Command("projects"))
@router.message(F.text == BTN_PROJECTS)
async def on_projects(message: Message, store: Store) -> None:
    projects = await store.projects(message.from_user.id)
    if not projects:
        await message.answer("Проєктів ще немає. Додай задачу з #назвою — проєкт створиться сам.")
        return
    rows = [[(f"#{short(p['name'], 28)}  {p['open']}", ListCb(view="project", pid=p["id"]))] for p in projects]
    await message.answer("<b>Проєкти</b>", reply_markup=ikb(rows))


@router.callback_query(ListCb.filter())
async def on_list_cb(cq: CallbackQuery, callback_data: ListCb, store: Store) -> None:
    text, kb = await render_list(store, cq.from_user.id, callback_data.view, callback_data.pid)
    await edit_or_send(cq, text, kb)
    await cq.answer()


@router.callback_query(TaskCb.filter())
async def on_task_cb(cq: CallbackQuery, callback_data: TaskCb, store: Store, state: FSMContext) -> None:
    uid, tid, action, view = cq.from_user.id, callback_data.id, callback_data.action, callback_data.view
    task = await store.task(uid, tid)
    if not task:
        await cq.answer("Задачу вже видалено.", show_alert=True)
        return

    today = store.today()
    note = None
    if action in ("done", "ldone"):
        task = await store.complete_task(uid, tid)
        note = f"Наступного разу: {fmt.day(task['due_date'], today)}" if task["recur"] else "Готово"
        if action == "ldone":
            text, kb = await render_list(store, uid, view, int(callback_data.arg or 0))
            await edit_or_send(cq, text, kb)
            await cq.answer(note)
            return
    elif action == "undo":
        task = await store.reopen_task(uid, tid)
    elif action == "go":
        await store.start(uid, task_id=tid)
        text, kb = await render_timer(store, uid)
        await edit_or_send(cq, text, kb)
        await cq.answer("Таймер пішов")
        return
    elif action == "due":
        arg = callback_data.arg
        due = None if arg == "x" else (today + timedelta(days=int(arg))).isoformat()
        fields: dict[str, Any] = {"due_date": due}
        if due is None:
            fields.update(due_time=None, recur=None)
        task = await store.update_task(uid, tid, **fields)
    elif action == "pri":
        task = await store.update_task(uid, tid, priority=int(callback_data.arg))
    elif action == "del":
        await store.delete_task(uid, tid)
        text, kb = await render_list(store, uid, view if view in VIEW_TITLES else "today")
        await edit_or_send(cq, text, kb)
        await cq.answer("Видалено")
        return
    elif action == "ren":
        await state.set_state(Rename.title)
        await state.update_data(task_id=tid)
        await cq.message.answer(f"Нова назва для «{escape(task['title'])}»:")
        await cq.answer()
        return

    text, kb = task_card(task, today, view)
    await edit_or_send(cq, text, kb)
    await cq.answer(note)


@router.message(Rename.title, F.text, ~F.text.in_(MENU_BUTTONS), ~F.text.startswith("/"))
async def on_rename(message: Message, state: FSMContext, store: Store) -> None:
    data = await state.get_data()
    await state.clear()
    task = await store.update_task(message.from_user.id, data["task_id"], title=message.text.strip()[:300])
    if task:
        text, kb = task_card(task, store.today(), "today")
        await message.answer(text, reply_markup=kb)


@router.message(Command("timer"))
@router.message(F.text == BTN_TIMER)
async def on_timer(message: Message, store: Store) -> None:
    text, kb = await render_timer(store, message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.message(Command("go"))
async def on_go(message: Message, command: CommandObject, store: Store) -> None:
    args = (command.args or "").strip()
    if not args:
        text, kb = await render_timer(store, message.from_user.id)
        await message.answer(text, reply_markup=kb)
        return
    project = None
    if m := RX_PROJECT.search(args):
        project = m.group(1).replace("_", " ")
        args = (args[: m.start()] + args[m.end() :]).strip()
    await store.start(message.from_user.id, title=args, project=project)
    text, kb = await render_timer(store, message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.message(Command("stop"))
async def on_stop(message: Message, store: Store) -> None:
    entry = await store.stop(message.from_user.id)
    if not entry:
        await message.answer("Таймер і так не йде.")
        return
    spent = entry["stopped_at"] - entry["started_at"]
    await message.answer(f"Зупинено: <b>{escape(entry['title'])}</b>, {fmt.duration(spent)}.")


@router.message(Command("log"))
async def on_log(message: Message, command: CommandObject, store: Store) -> None:
    args = (command.args or "").strip()
    project = None
    if m := RX_PROJECT.search(args):
        project = m.group(1).replace("_", " ")
        args = (args[: m.start()] + args[m.end() :]).strip()
    seconds, title = parse_duration(args)
    if not seconds:
        await message.answer("Формат: <code>/log 45m назва #проєкт</code> або <code>/log 1:30 рев'ю</code>")
        return
    entry = await store.log(message.from_user.id, title, seconds, project)
    tail = f" у #{escape(entry['project'])}" if entry["project"] else ""
    await message.answer(f"Записано {fmt.duration(seconds)}: <b>{escape(entry['title'])}</b>{tail}.")


@router.callback_query(TimerCb.filter())
async def on_timer_cb(cq: CallbackQuery, callback_data: TimerCb, store: Store) -> None:
    uid = cq.from_user.id
    note = None
    if callback_data.action == "stop":
        entry = await store.stop(uid)
        if entry:
            note = f"Записано {fmt.duration(entry['stopped_at'] - entry['started_at'])}"
    elif callback_data.action == "again":
        recent = await store.recent_titles(uid, 5)
        if callback_data.n < len(recent):
            r = recent[callback_data.n]
            await store.start(uid, title=r["title"], task_id=r["task_id"], project_id=r["project_id"])
    text, kb = await render_timer(store, uid)
    await edit_or_send(cq, text, kb)
    await cq.answer(note)


@router.message(Command("report"))
@router.message(F.text == BTN_REPORT)
async def on_report(message: Message, store: Store) -> None:
    await message.answer(await render_report(store, message.from_user.id))


async def render_ai(store: Store, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    connected = await store.ai_key_providers(uid)
    missing = [p for p in ai.LABELS if p not in connected]
    if not connected:
        text = OFFER.rsplit("\n\n", 1)[0] + "\n\nОбери, чий ключ додати:"
        return text, ikb(provider_rows(missing))

    current = await ai.pick_provider(store, uid)
    rows: list[list[tuple[str, CallbackData]]] = []
    for p in connected:
        mark = "● " if p == current else ""
        rows.append(
            [(mark + ai.LABELS[p], AiCb(action="use", provider=p)), ("Видалити ключ", AiCb(action="del", provider=p))]
        )
    rows += provider_rows(missing)
    text = f"Зараз відповідає <b>{ai.LABELS[current]}</b>.\n\nНатисни на модель, щоб відповідала вона."
    return text, ikb(rows)


@router.message(Command("ai"))
async def on_ai(message: Message, command: CommandObject, state: FSMContext, settings: Settings, store: Store) -> None:
    await state.clear()
    if command.args and await has_ai(store, message.from_user.id):
        await answer_ai(message, command.args, settings, store)
        return
    text, kb = await render_ai(store, message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(AiCb.filter())
async def on_ai_cb(cq: CallbackQuery, callback_data: AiCb, state: FSMContext, store: Store) -> None:
    uid, action, provider = cq.from_user.id, callback_data.action, callback_data.provider
    if action == "skip":
        await state.clear()
        await edit_or_send(cq, "Добре. Якщо передумаєш — /ai.", None)
        await cq.answer()
        return
    if action == "add":
        url, price = ai.KEY_HELP[provider]
        await state.set_state(AddKey.key)
        await state.update_data(provider=provider)
        await edit_or_send(
            cq,
            f"<b>{ai.LABELS[provider]}</b>, {price}.\n\n"
            f"1. Відкрий {url} і створи ключ.\n"
            "2. Надішли його сюди одним повідомленням.\n\n"
            "Повідомлення з ключем я одразу видалю з чату.",
            ikb([[("Скасувати", AiCb(action="cancel"))]]),
        )
        await cq.answer()
        return
    note = None
    if action == "cancel":
        await state.clear()
    elif action == "use":
        await store.set_ai_provider(uid, provider)
    elif action == "del":
        await store.delete_ai_key(uid, provider)
        note = f"Ключ {ai.LABELS[provider]} видалено"
    text, kb = await render_ai(store, uid)
    await edit_or_send(cq, text, kb)
    await cq.answer(note)


@router.message(AddKey.key, F.text, ~F.text.in_(MENU_BUTTONS), ~F.text.startswith("/"))
async def on_key(message: Message, state: FSMContext, settings: Settings, store: Store) -> None:
    provider = (await state.get_data())["provider"]
    key = message.text.strip()
    if len(key) < 20 or any(c.isspace() for c in key):
        # схоже на звичайне повідомлення: виходимо з режиму ключа і обробляємо його як завжди
        await state.clear()
        await on_text(message, settings, store)
        return
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    await message.bot.send_chat_action(message.chat.id, "typing")
    ok = await ai.check_key(settings, provider, key)
    if ok is False:
        await message.answer(f"{ai.LABELS[provider]} не прийняв цей ключ. Перевір його і надішли ще раз.")
        return
    await state.clear()
    await store.set_ai_key(message.from_user.id, provider, key)
    note = "" if ok else " Перевірити його зараз не вдалося, тож якщо щось не так — /ai."
    await message.answer(
        f"Готово, підключено {ai.LABELS[provider]}.{note}\n\n"
        "Пиши питання зі знаком <code>?</code> на початку, наприклад:\n"
        "<code>? з чого мені почати сьогодні</code>",
        reply_markup=main_kb(True),
    )


@router.message(Command("forget"))
async def on_forget(message: Message, store: Store) -> None:
    await store.ai_forget(message.from_user.id)
    await message.answer("Памʼять розмови очищено.")


async def answer_ai(message: Message, question: str, settings: Settings, store: Store) -> None:
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        result = await ai.ask(settings, store, message.from_user.id, question)
    except ai.AIError as exc:
        await message.answer(escape(str(exc)))
        return
    text = escape(result["reply"])
    if result["added"]:
        text += "\n\n<b>Додано:</b>\n" + "\n".join(f"— {escape(t['title'])}" for t in result["added"])
    text += f"\n\n<i>{result['label']}</i>"
    for i in range(0, len(text), 4000):
        await message.answer(text[i : i + 4000])


# має бути останнім хендлером: ловить будь-який текст як нову задачу


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, settings: Settings, store: Store) -> None:
    text = message.text.strip()
    if text.startswith("?") and await has_ai(store, message.from_user.id):
        await answer_ai(message, text.lstrip("? ").strip() or text, settings, store)
        return
    task = await store.add_task(message.from_user.id, parse_task(text, store.today()))
    card, kb = task_card(task, store.today(), "today")
    await message.answer("Додано\n\n" + card, reply_markup=kb)


async def reminders(bot: Bot, store: Store) -> None:
    while True:
        try:
            for task in await store.due_reminders():
                await store.mark_reminded(task["id"])
                kb = ikb(
                    [
                        [
                            ("Готово", TaskCb(action="done", id=task["id"])),
                            ("Старт таймера", TaskCb(action="go", id=task["id"])),
                        ],
                        [("Завтра", TaskCb(action="due", id=task["id"], arg="1"))],
                    ]
                )
                await bot.send_message(task["user_id"], f"Час: <b>{escape(task['title'])}</b>", reply_markup=kb)
        except asyncio.CancelledError:
            raise
        except Exception:  # помилка відправки не повинна зупиняти цикл
            log.exception("помилка в циклі нагадувань")
        await asyncio.sleep(30)


async def setup(bot: Bot, dp: Dispatcher, store: Store) -> None:
    users = Users(store)
    dp.message.outer_middleware(users)
    dp.callback_query.outer_middleware(users)
    dp.include_router(router)

    await bot.set_my_commands(
        [
            BotCommand(command="today", description="Задачі на сьогодні"),
            BotCommand(command="inbox", description="Вхідні"),
            BotCommand(command="upcoming", description="Наступні 14 днів"),
            BotCommand(command="timer", description="Таймер"),
            BotCommand(command="go", description="Запустити таймер: /go назва #проєкт"),
            BotCommand(command="stop", description="Зупинити таймер"),
            BotCommand(command="log", description="Дописати час: /log 45m назва"),
            BotCommand(command="report", description="Звіт за тиждень"),
            BotCommand(command="ai", description="Асистент: підключити або налаштувати"),
            BotCommand(command="help", description="Як користуватися"),
        ]
    )
