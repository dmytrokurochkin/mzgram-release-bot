from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode


def create_bot(token: str, api_url: str) -> Bot:
    """Bot bound to the local telegram-bot-api server.

    Files still go up as multipart uploads, so the server may run in another
    container or on another host; local mode only changes how files are downloaded.
    """
    session = AiohttpSession(api=TelegramAPIServer.from_base(api_url, is_local=True))
    return Bot(
        token=token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
