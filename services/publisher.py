import html
import re
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InputMediaDocument

from core.utils import utf16_len

CAPTION_LIMIT = 1024
MAX_FILES = 10  # one media group holds 2..10 files
LINK_TEXT = "Усі зміни і файли на GitHub"

_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_EMPHASIS = re.compile(r"(\*\*|__|`)")


def notes_to_text(body: str) -> str:
    """GitHub Markdown release notes as plain text for a caption."""
    lines = []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        line = line.rstrip()
        stripped = line.lstrip()
        if stripped.startswith("#"):
            line = stripped.lstrip("#").strip()
        elif stripped.startswith(("- ", "* ", "+ ")):
            line = "• " + stripped[2:]
        line = _EMPHASIS.sub("", _LINK.sub(r"\1", line))
        lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def release_title(repo: str, release: dict) -> str:
    project = repo.split("/")[-1].replace("-", " ")
    title = (release.get("name") or "").strip() or f"{project} {release['tag_name']}"
    if release.get("prerelease"):
        title += " (beta)"
    return title


def build_caption(title: str, notes: str, url: str, limit: int = CAPTION_LIMIT) -> str:
    """Title, the notes cut to fit, and a link to the release page with the full notes.

    The limit counts visible characters (after the HTML tags are parsed), so the
    budget is spent on plain text and HTML-escaped at the end.
    """
    fixed = utf16_len(title) + utf16_len(LINK_TEXT) + 4  # two "\n\n" separators
    budget = limit - fixed
    notes = notes.strip()
    if utf16_len(notes) > budget:
        kept: list[str] = []
        used = 0
        ellipsis = "…"
        for line in notes.split("\n"):
            cost = utf16_len(line) + (1 if kept else 0)
            if used + cost + 1 + utf16_len(ellipsis) > budget:
                if not kept:
                    # The first line alone is too long: cut it by characters
                    room = budget - utf16_len(ellipsis)
                    cut = ""
                    for char in line:
                        if utf16_len(cut + char) > room:
                            break
                        cut += char
                    kept.append(cut.rstrip())
                break
            kept.append(line)
            used += cost
        notes = "\n".join(kept).rstrip() + ellipsis if kept else ""
        notes = notes if notes != ellipsis else ""
    parts = [f"<b>{html.escape(title)}</b>"]
    if notes:
        parts.append(html.escape(notes))
    parts.append(f'<a href="{html.escape(url, quote=True)}">{LINK_TEXT}</a>')
    return "\n\n".join(parts)


def visible_length(caption: str) -> int:
    text = re.sub(r"<[^>]+>", "", caption)
    return utf16_len(html.unescape(text))


class Publisher:
    def __init__(self, bot: Bot, channel_id: int | str, upload_timeout: int):
        self.bot = bot
        self.channel_id = channel_id
        self.upload_timeout = upload_timeout

    async def send(self, files: list[Path], caption: str) -> list[int]:
        """One post: all files in one media group, the caption under the last file."""
        if len(files) == 1:
            message = await self.bot.send_document(
                self.channel_id,
                FSInputFile(files[0], filename=files[0].name),
                caption=caption,
                request_timeout=self.upload_timeout,
            )
            return [message.message_id]
        media = [
            InputMediaDocument(
                media=FSInputFile(path, filename=path.name),
                caption=caption if index == len(files) - 1 else None,
            )
            for index, path in enumerate(files)
        ]
        messages = await self.bot.send_media_group(self.channel_id, media, request_timeout=self.upload_timeout)
        return [message.message_id for message in messages]

    async def pin(self, message_id: int) -> None:
        await self.bot.pin_chat_message(self.channel_id, message_id, disable_notification=True)

    async def notify(self, admin_ids: list[int], text: str) -> None:
        for admin_id in admin_ids:
            try:
                await self.bot.send_message(admin_id, text)
            except Exception:  # noqa: BLE001 - an admin who never opened the bot must not stop the work
                pass
