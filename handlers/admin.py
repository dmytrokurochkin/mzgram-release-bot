import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from core import config
from services.releases import ReleaseService

admin_router = Router()
# Everyone else gets no answer at all: the bot is not a public bot
admin_router.message.filter(F.from_user.id.in_(set(config.ADMIN_IDS)))

STATUS_ICONS = {
    "skipped": "⏭",
    "new": "🆕",
    "verified": "✅",
    "posting": "⏳",
    "posted": "📣",
    "failed": "❌",
}

HELP = (
    "/status: останні релізи і їхній стан\n"
    "/check: перевірити GitHub зараз\n"
    "/mark_posted &lt;repo&gt; &lt;tag&gt; [message_id]: пост уже є в каналі, не публікувати\n"
    "/repost &lt;repo&gt; &lt;tag&gt;: опублікувати реліз (знову)\n"
    "repo: повна назва або її частина, наприклад android"
)


@admin_router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@admin_router.message(Command("status"))
async def cmd_status(message: Message, service: ReleaseService) -> None:
    rows = await service.db.recent(15)
    if not rows:
        await message.answer("Релізів ще немає.")
        return
    lines = []
    for row in rows:
        line = f"{STATUS_ICONS.get(row.status, '•')} {html.escape(row.repo)} {html.escape(row.tag)}: {row.status}"
        if row.error and row.status != "posted":
            line += f"\n    {html.escape(row.error[:200])}"
        lines.append(line)
    await message.answer("\n".join(lines))


@admin_router.message(Command("check"))
async def cmd_check(message: Message, service: ReleaseService) -> None:
    service.wake()
    await message.answer("Перевіряю GitHub.")


def _parse(command: CommandObject, service: ReleaseService) -> tuple[str, str, list[str]] | None:
    args = (command.args or "").split()
    if len(args) < 2:
        return None
    repo = service.resolve_repo(args[0])
    if repo is None:
        return None
    return repo, args[1], args[2:]


@admin_router.message(Command("mark_posted"))
async def cmd_mark_posted(message: Message, command: CommandObject, service: ReleaseService) -> None:
    parsed = _parse(command, service)
    if parsed is None:
        await message.answer("Формат: /mark_posted &lt;repo&gt; &lt;tag&gt; [message_id]")
        return
    repo, tag, rest = parsed
    message_id = int(rest[0]) if rest and rest[0].isdigit() else None
    if await service.mark_posted(repo, tag, message_id):
        await message.answer(f"📣 {html.escape(repo)} {html.escape(tag)}: позначено як опублікований.")
    else:
        await message.answer(f"Не знайдено {html.escape(repo)} {html.escape(tag)}.")


@admin_router.message(Command("repost"))
async def cmd_repost(message: Message, command: CommandObject, service: ReleaseService) -> None:
    parsed = _parse(command, service)
    if parsed is None:
        await message.answer("Формат: /repost &lt;repo&gt; &lt;tag&gt;")
        return
    repo, tag, _ = parsed
    if await service.repost(repo, tag):
        await message.answer(f"🆕 {html.escape(repo)} {html.escape(tag)}: опублікую найближчим часом.")
    else:
        await message.answer(f"Не знайдено {html.escape(repo)} {html.escape(tag)}.")
