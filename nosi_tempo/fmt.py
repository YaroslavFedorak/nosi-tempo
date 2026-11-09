"""Small formatting helpers shared by the bot and the assistant prompt."""

from __future__ import annotations

from datetime import date
from html import escape

from .parse import recur_label

MONTHS = ["січ", "лют", "бер", "квіт", "трав", "черв", "лип", "серп", "вер", "жовт", "лист", "груд"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]


def duration(seconds: int) -> str:
    """5400 -> '1 год 30 хв', 59 -> '<1 хв'."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours and minutes:
        return f"{hours} год {minutes} хв"
    if hours:
        return f"{hours} год"
    if minutes:
        return f"{minutes} хв"
    return "<1 хв"


def clock(seconds: int) -> str:
    """5400 -> '1:30:00'."""
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}"


def day(d: date | str | None, today: date) -> str:
    if not d:
        return ""
    if isinstance(d, str):
        d = date.fromisoformat(d)
    delta = (d - today).days
    if delta == 0:
        return "сьогодні"
    if delta == 1:
        return "завтра"
    if delta == -1:
        return "вчора"
    label = f"{WEEKDAYS_SHORT[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"
    if d.year != today.year:
        label += f" {d.year}"
    return label


def task_meta(task: dict, today: date) -> str:
    """'завтра 18:00, #дім, щотижня' for one task; empty when there is nothing to say."""
    bits = []
    if task.get("due_date"):
        due = day(task["due_date"], today)
        if task.get("due_time"):
            due += f" {task['due_time']}"
        if task["due_date"] < today.isoformat():
            due = f"прострочено: {due}"
        bits.append(due)
    if task.get("project"):
        bits.append(f"#{task['project']}")
    if task.get("recur"):
        bits.append(recur_label(task["recur"]))
    if task.get("tracked"):
        bits.append(duration(task["tracked"]))
    return ", ".join(bits)


PRIORITY_MARK = {1: "!!! ", 2: "!! ", 3: "! ", 4: ""}


def task_line(task: dict, today: date, n: int | None = None) -> str:
    head = f"{n}. " if n is not None else ""
    title = escape(task["title"])
    mark = PRIORITY_MARK.get(task.get("priority", 4), "")
    meta = task_meta(task, today)
    line = f"{head}{mark}<b>{title}</b>"
    if meta:
        line += f"\n    <i>{escape(meta)}</i>"
    return line


def bar(value: int, top: int, width: int = 14) -> str:
    if top <= 0:
        return ""
    filled = round(width * value / top)
    return "█" * filled + "░" * (width - filled)
