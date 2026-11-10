# Nosi Tempo

A personal task manager and time tracker as a Telegram bot. Think of a small Todoist and
Toggl Track in one chat, with an LLM assistant that can read your tasks and plan with you.

The bot speaks Ukrainian.

## Features

**Tasks**
- Natural-language quick add: just send a message.
  `Подзвонити мамі завтра о 18:00 #дім !1` becomes a task due tomorrow at 18:00
  in the project «дім» with priority 1.
- Dates (`завтра`, `пт`, `12.11`, `через 3 дні`), times, projects, priorities.
- Recurring tasks (`щодня`, `по буднях`, `щоп'ятниці`, `кожні 2 дні`): completing one moves
  it to the next date instead of closing it.
- Today (with overdue), Inbox, Upcoming and per-project lists with inline buttons:
  done, reschedule, change priority, rename, delete.
- Reminders sent when a task's time comes.

**Time tracking**
- Start a timer from any task card or with `/go title #project`. Starting a new timer stops
  the running one, so there are never two.
- Manual entries: `/log 45m code review #work`, `/log 1:30 design`.
- `/report`: the week's time by project, by day and by task.

**Assistant**
- Gemini, Claude or any OpenAI-compatible model, switchable with `/ai`.
- Sees open tasks, the running timer and the week's time, so it can answer
  `? what should I do first` with your actual data.
- Can create tasks: the model replies with `ADD: ...` lines that go through the same
  quick-add parser.

## Commands

| Command | What it does |
|---|---|
| any text | add a task |
| `? question` | ask the assistant |
| `/today`, `/inbox`, `/upcoming`, `/projects` | task lists |
| `/go title #project`, `/stop`, `/timer` | timer |
| `/log 45m title #project` | add time manually |
| `/report` | weekly report |
| `/ai`, `/forget` | choose the model, clear the conversation |

## Stack

- Python 3.11+, asyncio
- [aiogram 3](https://docs.aiogram.dev/)
- SQLite via aiosqlite
- aiohttp for the LLM APIs
- pytest

## How it works

- **`parse.py`** turns free text into a task. Regexes use custom word boundaries so Cyrillic
  words with apostrophes (`п'ятниця`) are matched whole, and recurrence is parsed before plain
  weekdays so `кожного понеділка` is not read as "next Monday".
- **One running timer per user** is enforced by the database itself, with a partial unique
  index on `entries(user_id) WHERE stopped_at IS NULL`.
- **Reports** split an entry that crosses midnight between the two days.
- **Access**: a middleware lets through only the users listed in `OWNER_IDS`.
- **Reminders** run as a background task next to polling and check due tasks every 30 seconds.
- **AI providers** are called over plain HTTP with no SDKs, so adding one is a single function.

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # set BOT_TOKEN, OWNER_IDS and optionally an AI key
python -m nosi_tempo
```

Docker:

```bash
docker build -t nosi-tempo .
docker run -d --env-file .env -v tempo:/data nosi-tempo
```

## Tests

```bash
pip install pytest
python -m pytest
```

Covered: the quick-add and duration parsers, recurrence rules, task views, timers and reports.

## Structure

```
nosi_tempo/
  __main__.py     entry point: polling and the reminder loop
  bot.py          handlers, inline keyboards, reminders
  db.py           SQLite schema and queries
  parse.py        quick-add and duration parsing
  ai.py           Gemini, Claude and OpenAI-compatible clients
  fmt.py          message formatting
tests/
```
