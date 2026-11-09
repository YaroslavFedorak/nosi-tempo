from datetime import date

import pytest

from nosi_tempo.parse import next_due, parse_duration, parse_task

MON = date(2026, 11, 9)  # a Monday


def test_plain_title_stays_whole():
    p = parse_task("Купити хліб", MON)
    assert (p.title, p.due_date, p.project, p.priority) == ("Купити хліб", None, None, 4)


def test_tomorrow_time_project_priority():
    p = parse_task("Подзвонити мамі завтра о 18:00 #дім !1", MON)
    assert p.title == "Подзвонити мамі"
    assert p.due_date == date(2026, 11, 10)
    assert p.due_time == "18:00"
    assert p.project == "дім"
    assert p.priority == 1


def test_day_after_tomorrow_beats_tomorrow():
    assert parse_task("Зустріч післязавтра", MON).due_date == date(2026, 11, 11)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Звіт у пт", date(2026, 11, 13)),
        ("Звіт в п'ятницю", date(2026, 11, 13)),
        ("Звіт в пʼятницю", date(2026, 11, 13)),
        ("Планування пн", date(2026, 11, 16)),  # next Monday, not today
        ("Оплата 12.11", date(2026, 11, 12)),
        ("Оплата 1.03", date(2027, 3, 1)),
        ("Оплата 2026-12-01", date(2026, 12, 1)),
        ("Відпочити через 3 дні", date(2026, 11, 12)),
        ("Call mom tomorrow", date(2026, 11, 10)),
    ],
)
def test_dates(text, expected):
    assert parse_task(text, MON).due_date == expected


def test_words_inside_other_words_are_left_alone():
    p = parse_task("Прочитати Завтракова і пташку", MON)
    assert p.due_date is None
    assert p.title == "Прочитати Завтракова і пташку"


def test_time_alone_means_today():
    p = parse_task("Стендап о 10", MON)
    assert (p.title, p.due_date, p.due_time) == ("Стендап", MON, "10:00")


@pytest.mark.parametrize(
    "text, recur, due",
    [
        ("Спортзал щодня", "daily", MON),
        ("Ліки кожні 2 дні", "every:2", MON),
        ("Звіт щоп'ятниці #робота", "weekly", date(2026, 11, 13)),
        ("Ретро кожного понеділка", "weekly", MON),
        ("Рахунки щомісяця", "monthly", MON),
        ("Пошта по буднях", "weekdays", MON),
    ],
)
def test_recurrence(text, recur, due):
    p = parse_task(text, MON)
    assert p.recur == recur
    assert p.due_date == due


def test_next_due_skips_past_days():
    assert next_due(date(2026, 11, 5), "daily", MON) == date(2026, 11, 10)
    assert next_due(MON, "weekly", MON) == date(2026, 11, 16)
    assert next_due(date(2026, 1, 31), "monthly", date(2026, 1, 31)) == date(2026, 2, 28)
    assert next_due(date(2026, 11, 13), "weekdays", date(2026, 11, 13)) == date(2026, 11, 16)
    assert next_due(MON, "every:3", MON) == date(2026, 11, 12)


@pytest.mark.parametrize(
    "text, seconds, title",
    [
        ("45m рев'ю коду", 2700, "рев'ю коду"),
        ("1h30m дизайн", 5400, "дизайн"),
        ("1г 15хв дзвінок", 4500, "дзвінок"),
        ("1.5h читання", 5400, "читання"),
        ("1:30 зустріч", 5400, "зустріч"),
        ("зустріч 20хв", 1200, "зустріч"),
        ("просто текст", 0, "просто текст"),
    ],
)
def test_duration(text, seconds, title):
    assert parse_duration(text) == (seconds, title)
