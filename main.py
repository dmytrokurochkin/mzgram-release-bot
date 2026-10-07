import argparse
import asyncio
import contextlib
import logging
import sys

import aiohttp
from aiogram import Dispatcher

from core import config
from core.lock import AlreadyRunning, InstanceLock
from core.loader import create_bot
from core.utils import RedactFilter, add_secret
from database import Database
from handlers.admin import admin_router
from services.github import GitHubClient
from services.publisher import Publisher
from services.releases import ReleaseService

log = logging.getLogger("mzgram-release-bot")


def setup_logging() -> None:
    add_secret(config.BOT_TOKEN)
    add_secret(config.GITHUB_TOKEN)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])


async def run(backfill_only: bool) -> None:
    db = Database(config.DB_PATH)
    await db.init()
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        github = GitHubClient(http, config.GITHUB_API_URL, config.GITHUB_TOKEN)
        bot = create_bot(config.BOT_TOKEN, config.BOT_API_URL)
        try:
            service = ReleaseService(
                db=db,
                github=github,
                publisher=Publisher(bot, config.CHANNEL_ID, config.UPLOAD_TIMEOUT),
                repos=config.REPOS,
                download_dir=config.DOWNLOAD_DIR,
                admin_ids=config.ADMIN_IDS,
                publish_prereleases=config.PUBLISH_PRERELEASES,
                poll_interval=config.POLL_INTERVAL,
            )
            if backfill_only:
                await service.backfill()
                log.info("backfill done: existing releases are recorded and will not be posted")
                return
            dp = Dispatcher(service=service)
            dp.include_router(admin_router)
            loop_task = asyncio.create_task(service.run())
            try:
                await dp.start_polling(bot, allowed_updates=["message"])
            finally:
                loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop_task
        finally:
            await bot.session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Posts MZGram releases from GitHub to a Telegram channel")
    parser.add_argument("--backfill", action="store_true", help="record the existing releases without posting, then exit")
    args = parser.parse_args()

    setup_logging()
    missing = config.missing()
    if missing:
        log.error("not set in .env: %s", ", ".join(missing))
        return 2
    try:
        with InstanceLock(config.LOCK_PATH):
            asyncio.run(run(args.backfill))
    except AlreadyRunning as exc:
        log.error("%s: one copy of the bot is already running", exc)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
