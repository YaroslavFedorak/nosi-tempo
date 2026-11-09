"""Quick-add syntax, the same in the bot and in the Mini App.

    Подзвонити мамі завтра о 18:00 #дім !1
    Звіт щоп'ятниці #робота
    Спортзал кожні 2 дні

#проєкт sets the project, !1..!4 the priority (1 is the most urgent),
dates and times are Ukrainian or English words, recurrence words make
the task repeat after it is ticked off.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

WEEKDAYS: dict[str, int] = {}
for idx, names in enumerate(
    [
        "пн понеділок понеділка mon monday",
        "вт вівторок вівторка tue tuesday",
        "ср середа середу середи wed wednesday",
        "чт четвер четверга thu thursday",
        "пт п'ятниця п'ятницю п'ятниці пятниця пятницю fri friday",
        "сб субота суботу суботи sat saturday",
        "нд неділя неділю неділі sun sunday",
    ]
):
    for name in names.split():
        WEEKDAYS[name] = idx

_WD = "|".join(sorted(map(re.escape, WEEKDAYS), key=len, reverse=True))

# Token boundaries that understand Cyrillic and the apostrophe inside words.
L = r"(?<![\w'#])"
R = r"(?![\w'])"


def _rx(body: str) -> re.Pattern[str]:
    return re.compile(L + body + R, re.IGNORECASE)


RX_EVERY_WD = _rx(rf"(?:кожного|кожної|кожну|кожен|every)\s+({_WD})")
RX_EVERY_WD_SHORT = _rx(r"що(понеділка|вівторка|середи|четверга|п'ятниці|пятниці|суботи|неділі)")
RX_EVERY_N = _rx(r"(?:кожні|кожних|every)\s+(\d{1,3})\s*(?:дні|днів|день|дня|days?|d)")
RX_RECUR = {
    "daily": _rx(r"(?:щодня|щоденно|кожного\s+дня|кожен\s+день|every\s+day|daily)"),
    "weekdays": _rx(r"(?:по\s+буднях|у\s+будні|щобудня|weekdays|every\s+weekday)"),
    "weekly": _rx(r"(?:щотижня|щотижнево|кожного\s+тижня|every\s+week|weekly)"),
    "monthly": _rx(r"(?:щомісяця|щомісячно|кожного\s+місяця|every\s+month|monthly)"),
}
RX_IN_DAYS = _rx(r"(?:через|in)\s+(\d{1,3})\s*(?:дні|днів|день|дня|days?)")
RX_IN_WEEK = _rx(r"(?:через\s+тиждень|in\s+a\s+week|next\s+week|наступного\s+тижня)")
RX_ISO = _rx(r"(\d{4})-(\d{2})-(\d{2})")
RX_DMY = _rx(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?")
RX_WORDS = {
    0: _rx(r"(?:сьогодні|сьогодня|today|tod)"),
    1: _rx(r"(?:завтра|tomorrow|tmr)"),
    2: _rx(r"(?:післязавтра)"),
}
RX_WEEKDAY = _rx(rf"(?:(?:у|в|на|до|this|next|on)\s+)?({_WD})")
RX_TIME_PREFIXED = _rx(r"(?:о|об|в|at|@)\s*(\d{1,2})(?::(\d{2}))?")
RX_TIME_BARE = _rx(r"(\d{1,2}):(\d{2})")
RX_PROJECT = re.compile(r"(?<![\w])#([\w'\-/]+)", re.UNICODE)
RX_PRIORITY = _rx(r"(?:!([1-4])|p([1-4]))")
RX_DURATION = re.compile(
    r"(?<![\w.:])(?:(\d+(?:[.,]\d+)?)\s*(?:h|hr|год|г|ч)\.?\s*)?(?:(\d+)\s*(?:m|min|хв|м)\.?)?(?![\w])",
    re.IGNORECASE,
)
RX_HMM = _rx(r"(\d{1,2}):(\d{2})")

SHORT_WD_TO_IDX = {
    "понеділка": 0, "вівторка": 1, "середи": 2, "четверга": 3,
    "п'ятниці": 4, "пятниці": 4, "суботи": 5, "неділі": 6,
}


@dataclass
class Parsed:
    title: str
    project: str | None = None
    priority: int = 4
    due_date: date | None = None
    due_time: str | None = None
    recur: str | None = None


def _norm(text: str) -> str:
    return re.sub(r"[’ʼ`]", "'", text)


def _cut(text: str, m: re.Match[str]) -> str:
    return text[: m.start()] + " " + text[m.end() :]


def _clean(text: str) -> str:
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text.strip(" ,;-–—")


def next_weekday(today: date, wd: int, include_today: bool = False) -> date:
    delta = (wd - today.weekday()) % 7
    if delta == 0 and not include_today:
        delta = 7
    return today + timedelta(days=delta)


def add_months(d: date, n: int) -> date:
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_due(due: date, recur: str, today: date) -> date:
    """The next date a repeating task is due once it has been ticked off."""
    if recur == "weekdays":
        d = max(due, today) + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d
    if recur == "monthly":
        n, d = 1, add_months(due, 1)
        while d <= today:
            n += 1
            d = add_months(due, n)
        return d
    step = {"daily": 1, "weekly": 7}.get(recur)
    if step is None and recur.startswith("every:"):
        step = max(1, int(recur.split(":", 1)[1]))
    step = step or 1
    d = due + timedelta(days=step)
    while d <= today:
        d += timedelta(days=step)
    return d


def recur_label(recur: str | None) -> str:
    if not recur:
        return ""
    if recur.startswith("every:"):
        return f"кожні {recur.split(':', 1)[1]} дн."
    return {
        "daily": "щодня",
        "weekdays": "по буднях",
        "weekly": "щотижня",
        "monthly": "щомісяця",
    }.get(recur, recur)


def parse_task(raw: str, today: date) -> Parsed:
    text = _norm(raw)
    p = Parsed(title="")

    if m := RX_PROJECT.search(text):
        p.project = m.group(1).replace("_", " ")
        text = _cut(text, m)

    if m := RX_PRIORITY.search(text):
        p.priority = int(m.group(1) or m.group(2))
        text = _cut(text, m)

    # Recurrence first: "кожного понеділка" must not be read as a plain weekday.
    if m := RX_EVERY_WD.search(text):
        p.recur = "weekly"
        p.due_date = next_weekday(today, WEEKDAYS[m.group(1).lower()], include_today=True)
        text = _cut(text, m)
    elif m := RX_EVERY_WD_SHORT.search(text):
        p.recur = "weekly"
        p.due_date = next_weekday(today, SHORT_WD_TO_IDX[m.group(1).lower()], include_today=True)
        text = _cut(text, m)
    elif m := RX_EVERY_N.search(text):
        p.recur = f"every:{int(m.group(1))}"
        text = _cut(text, m)
    else:
        for code, rx in RX_RECUR.items():
            if m := rx.search(text):
                p.recur = code
                text = _cut(text, m)
                break

    if p.due_date is None:
        if m := RX_IN_DAYS.search(text):
            p.due_date = today + timedelta(days=int(m.group(1)))
            text = _cut(text, m)
        elif m := RX_IN_WEEK.search(text):
            p.due_date = today + timedelta(days=7)
            text = _cut(text, m)
        elif m := RX_ISO.search(text):
            try:
                p.due_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                text = _cut(text, m)
            except ValueError:
                pass
        elif m := RX_DMY.search(text):
            day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
            try:
                if year:
                    y = int(year)
                    p.due_date = date(y + 2000 if y < 100 else y, month, day)
                else:
                    d = date(today.year, month, day)
                    p.due_date = d if d >= today else date(today.year + 1, month, day)
                text = _cut(text, m)
            except ValueError:
                pass

    if p.due_date is None:
        # післязавтра before завтра, so the longer word wins
        for offset in (2, 1, 0):
            if m := RX_WORDS[offset].search(text):
                p.due_date = today + timedelta(days=offset)
                text = _cut(text, m)
                break

    if p.due_date is None and (m := RX_WEEKDAY.search(text)):
        p.due_date = next_weekday(today, WEEKDAYS[m.group(1).lower()])
        text = _cut(text, m)

    m = RX_TIME_PREFIXED.search(text) or RX_TIME_BARE.search(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if hour < 24 and minute < 60:
            p.due_time = f"{hour:02d}:{minute:02d}"
            text = _cut(text, m)

    if p.recur and p.due_date is None:
        p.due_date = today
        if p.recur == "weekdays" and today.weekday() >= 5:
            p.due_date = next_weekday(today, 0)
    if p.due_time and p.due_date is None:
        p.due_date = today

    p.title = _clean(text) or _clean(_norm(raw))
    return p


def parse_duration(raw: str) -> tuple[int, str]:
    """'1h30m code review' -> (5400, 'code review'). Also '45хв', '1.5г', '1:30'."""
    text = _norm(raw).strip()
    if m := RX_HMM.search(text):
        secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60
        return secs, _clean(_cut(text, m))
    for m in RX_DURATION.finditer(text):
        if not (m.group(1) or m.group(2)):
            continue
        hours = float(m.group(1).replace(",", ".")) if m.group(1) else 0.0
        minutes = int(m.group(2)) if m.group(2) else 0
        secs = int(hours * 3600) + minutes * 60
        if secs > 0:
            return secs, _clean(_cut(text, m))
    return 0, _clean(text)
