import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties

from config import BOT_TOKEN, get_proxy
from db import Database
from handlers import router
from notifier import schedule_checker_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("main")


async def main():
    log.info("Starting OmGTU Schedule Bot (@omgturaspibot)...")

    # 1. Initialize Database
    db = await Database.create()
    log.info("Database initialized.")

    # 2. Configure proxy if available (VPS Xray VLESS proxy)
    proxy_url = get_proxy()
    session = None
    if proxy_url:
        log.info(f"Using HTTP proxy for Telegram API: {proxy_url}")
        session = AiohttpSession(proxy=proxy_url)

    # 3. Create Bot & Dispatcher
    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode="HTML")
    )
    dp = Dispatcher()
    dp["db"] = db
    dp.include_router(router)

    # 4. Start background hourly schedule checker
    checker_task = asyncio.create_task(schedule_checker_loop(db, bot))
    log.info("Background schedule checker started.")

    # 5. Start Polling
    try:
        log.info("Bot polling started.")
        await dp.start_polling(bot)
    finally:
        checker_task.cancel()
        await bot.session.close()
        await db.close()
        from schedule_service import close_http_session
        await close_http_session()
        log.info("OmGTU Schedule Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot interrupted.")
