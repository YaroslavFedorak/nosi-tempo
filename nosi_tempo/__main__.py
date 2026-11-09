from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

from . import bot as chat
from .config import load_settings
from .db import Store
from .server import create_app


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # --web-only піднімає лише Mini App, для локальної розробки інтерфейсу
    web_only = "--web-only" in sys.argv
    settings = load_settings(require_token=not web_only)

    store = Store(settings.db_path, settings.tz)
    await store.open()

    runner = web.AppRunner(create_app(settings, store), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, settings.web_host, settings.web_port).start()
    logging.info("Mini App: http://%s:%s (публічна адреса: %s)", settings.web_host, settings.web_port, settings.webapp_url or "не задана")

    if web_only:
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            await store.close()
        return

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(settings=settings, store=store)
    await chat.setup(bot, dp, settings, store)
    reminders = asyncio.create_task(chat.reminders(bot, store))

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        reminders.cancel()
        await runner.cleanup()
        await store.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
