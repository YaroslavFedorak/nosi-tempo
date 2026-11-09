from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from . import ai
from .config import Settings
from .db import Store, now
from .parse import RX_PROJECT, parse_duration, parse_task

STATIC = Path(__file__).parent / "web" / "static"
MAX_AGE = 7 * 86400

settings_key = web.AppKey("settings", Settings)
store_key = web.AppKey("store", Store)


def verify_init_data(init_data: str, bot_token: str) -> dict | None:
    # https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received = fields.pop("hash", "")
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    if time.time() - int(fields.get("auth_date", "0")) > MAX_AGE:
        return None
    try:
        return json.loads(fields.get("user", "{}"))
    except json.JSONDecodeError:
        return None


@web.middleware
async def auth(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    settings: Settings = request.app[settings_key]
    user = verify_init_data(request.headers.get("X-Init-Data", ""), settings.bot_token)
    if user is None and settings.dev_user_id and request.remote in ("127.0.0.1", "::1"):
        user = {"id": settings.dev_user_id, "first_name": "dev"}
    if not user or "id" not in user:
        return web.json_response({"error": "Відкрий застосунок з Telegram."}, status=401)
    if not settings.allowed(int(user["id"])):
        return web.json_response({"error": "Це приватний застосунок."}, status=403)
    request["uid"] = int(user["id"])
    await request.app[store_key].ensure_user(request["uid"], user.get("first_name", ""))
    try:
        return await handler(request)
    except ai.AIError as exc:
        return web.json_response({"error": str(exc)}, status=502)


@web.middleware
async def revalidate(request: web.Request, handler):
    # вебвʼю Telegram агресивно кешує статику, тому просимо перевіряти її щоразу
    response = await handler(request)
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


routes = web.RouteTableDef()


def _store(request: web.Request) -> Store:
    return request.app[store_key]


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text='{"error":"Некоректний JSON."}', content_type="application/json")
    return data if isinstance(data, dict) else {}


def _task_or_404(task: dict | None) -> web.Response:
    if not task:
        return web.json_response({"error": "Задачу не знайдено."}, status=404)
    return web.json_response({"task": task})


@routes.get("/api/state")
async def state(request: web.Request) -> web.Response:
    s, uid = _store(request), request["uid"]
    view = request.query.get("view", "today")
    pid = int(request.query.get("project") or 0) or None
    settings: Settings = request.app[settings_key]
    return web.json_response(
        {
            "view": view,
            "today": s.today().isoformat(),
            "now": now(),
            "tz": settings.tz_label,
            "tasks": await s.tasks(uid, view, pid),
            "projects": await s.projects(uid),
            "running": await s.running(uid),
        }
    )


@routes.post("/api/tasks")
async def add_task(request: web.Request) -> web.Response:
    s, data = _store(request), await _body(request)
    text = str(data.get("text", "")).strip()
    if not text:
        return web.json_response({"error": "Порожня задача."}, status=400)
    parsed = parse_task(text, s.today())
    if data.get("project") and not parsed.project:
        parsed.project = str(data["project"])
    return web.json_response({"task": await s.add_task(request["uid"], parsed)})


@routes.patch("/api/tasks/{id:\\d+}")
async def edit_task(request: web.Request) -> web.Response:
    data = await _body(request)
    allowed = {k: data[k] for k in ("title", "notes", "priority", "due_date", "due_time", "recur", "project") if k in data}
    return _task_or_404(await _store(request).update_task(request["uid"], int(request.match_info["id"]), **allowed))


@routes.post("/api/tasks/{id:\\d+}/done")
async def done_task(request: web.Request) -> web.Response:
    return _task_or_404(await _store(request).complete_task(request["uid"], int(request.match_info["id"])))


@routes.post("/api/tasks/{id:\\d+}/undo")
async def undo_task(request: web.Request) -> web.Response:
    return _task_or_404(await _store(request).reopen_task(request["uid"], int(request.match_info["id"])))


@routes.delete("/api/tasks/{id:\\d+}")
async def delete_task(request: web.Request) -> web.Response:
    await _store(request).delete_task(request["uid"], int(request.match_info["id"]))
    return web.json_response({"ok": True})


@routes.post("/api/timer/start")
async def timer_start(request: web.Request) -> web.Response:
    s, data = _store(request), await _body(request)
    title = str(data.get("title", "")).strip()
    project = None
    if m := RX_PROJECT.search(title):
        project = m.group(1).replace("_", " ")
        title = (title[: m.start()] + title[m.end() :]).strip()
    running = await s.start(
        request["uid"],
        title=title,
        task_id=data.get("task_id"),
        project=project,
        project_id=data.get("project_id"),
    )
    return web.json_response({"running": running})


@routes.post("/api/timer/stop")
async def timer_stop(request: web.Request) -> web.Response:
    return web.json_response({"stopped": await _store(request).stop(request["uid"])})


@routes.get("/api/entries")
async def entries(request: web.Request) -> web.Response:
    s = _store(request)
    days = min(int(request.query.get("days", 7)), 92)
    since = s.day_start(s.today()) - (days - 1) * 86400
    return web.json_response(
        {
            "entries": await s.entries(request["uid"], since),
            "running": await s.running(request["uid"]),
            "recent": await s.recent_titles(request["uid"], 5),
            "now": now(),
        }
    )


@routes.post("/api/entries")
async def log_entry(request: web.Request) -> web.Response:
    s, data = _store(request), await _body(request)
    text = str(data.get("text", "")).strip()
    project = None
    if m := RX_PROJECT.search(text):
        project = m.group(1).replace("_", " ")
        text = (text[: m.start()] + text[m.end() :]).strip()
    seconds, title = parse_duration(text)
    if not seconds:
        return web.json_response({"error": "Вкажи тривалість: 45m, 1h30m або 1:30."}, status=400)
    return web.json_response({"entry": await s.log(request["uid"], title, seconds, project)})


@routes.delete("/api/entries/{id:\\d+}")
async def delete_entry(request: web.Request) -> web.Response:
    await _store(request).delete_entry(request["uid"], int(request.match_info["id"]))
    return web.json_response({"ok": True})


@routes.get("/api/report")
async def report(request: web.Request) -> web.Response:
    days = {"today": 1, "week": 7, "month": 30}.get(request.query.get("range", "week"), 7)
    return web.json_response(await _store(request).report(request["uid"], days))


@routes.get("/api/ai")
async def ai_state(request: web.Request) -> web.Response:
    s, settings = _store(request), request.app[settings_key]
    available = settings.ai_providers()
    current = ai.pick_provider(settings, await s.ai_provider(request["uid"])) if available else None
    return web.json_response(
        {
            "providers": [{"id": p, "label": ai.LABELS[p]} for p in available],
            "current": current,
            "history": await s.ai_history(request["uid"], 30),
        }
    )


@routes.post("/api/ai")
async def ai_ask(request: web.Request) -> web.Response:
    s, data = _store(request), await _body(request)
    message = str(data.get("message", "")).strip()
    if not message:
        return web.json_response({"error": "Порожнє повідомлення."}, status=400)
    provider = data.get("provider")
    if provider in ai.LABELS:
        await s.set_ai_provider(request["uid"], provider)
    result = await ai.ask(request.app[settings_key], s, request["uid"], message, provider)
    return web.json_response(result)


@routes.delete("/api/ai")
async def ai_forget(request: web.Request) -> web.Response:
    await _store(request).ai_forget(request["uid"])
    return web.json_response({"ok": True})


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def create_app(settings: Settings, store: Store) -> web.Application:
    app = web.Application(middlewares=[revalidate, auth], client_max_size=256 * 1024)
    app[settings_key] = settings
    app[store_key] = store
    app.add_routes(routes)
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_static("/static/", STATIC)
    return app
