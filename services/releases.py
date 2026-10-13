import asyncio
import html
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from core.utils import redact
from database import Database, ReleaseRow
from services.github import GitHubClient, GitHubError, RateLimited
from services.publisher import MAX_FILES, Publisher, build_caption, notes_to_text, release_title

log = logging.getLogger(__name__)

SUMS_NAME = "SHA256SUMS"
MAX_FILE_SIZE = 2000 * 1024 * 1024  # the local Bot API server limit
FREE_SPACE_MARGIN = 200 * 1024 * 1024
# Files that go to the channel: a name ending with an entry that starts with
# a dot, or a name equal to the entry. The rest (the signing lineage, for
# example) is checked like every file but stays on the release page.
DEFAULT_CHANNEL_FILES = (".apk", ".exe", ".zip", ".tar.xz", ".AppImage", ".deb", ".rpm", SUMS_NAME)
# Rate limited without a reset time in the answer: try again after this
RATE_LIMIT_FALLBACK = 15 * 60
_SUMS_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+)$")


class Incomplete(Exception):
    """The release does not have all its files yet: try again on the next poll."""


class Rejected(Exception):
    """The release must not be posted as it is: wait for the admin."""


def parse_sums(text: str) -> dict[str, str]:
    sums = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _SUMS_LINE.match(line)
        if not match:
            raise Rejected(f"{SUMS_NAME}: unreadable line {line[:80]!r}")
        name = match.group(2).strip()
        if "/" in name or "\\" in name or name in ("", ".", ".."):
            raise Rejected(f"{SUMS_NAME}: bad file name {name!r}")
        sums[name] = match.group(1).lower()
    if not sums:
        raise Rejected(f"{SUMS_NAME} is empty")
    return sums


def for_channel(name: str, patterns) -> bool:
    lowered = name.lower()
    for pattern in patterns:
        if pattern.startswith("."):
            if lowered.endswith(pattern.lower()):
                return True
        elif name == pattern:
            return True
    return False


def compact(release: dict) -> dict:
    """The part of a GitHub release the bot keeps."""
    return {
        "tag_name": release["tag_name"],
        "name": release.get("name") or "",
        "body": release.get("body") or "",
        "html_url": release.get("html_url") or "",
        "prerelease": bool(release.get("prerelease")),
        "published_at": release.get("published_at") or "",
        "assets": [
            {
                "name": asset["name"],
                "size": asset.get("size", 0),
                "digest": asset.get("digest"),
                "state": asset.get("state", "uploaded"),
                "url": asset["browser_download_url"],
            }
            for asset in release.get("assets", [])
        ],
    }


class ReleaseService:
    def __init__(
        self,
        db: Database,
        github: GitHubClient,
        publisher: Publisher,
        repos: list[str],
        download_dir: Path,
        admin_ids: list[int],
        publish_prereleases: bool = False,
        poll_interval: int = 600,
        clock=time.time,
        channel_files=DEFAULT_CHANNEL_FILES,
    ):
        self.db = db
        self.github = github
        self.publisher = publisher
        self.repos = repos
        self.download_dir = download_dir
        self.admin_ids = admin_ids
        self.publish_prereleases = publish_prereleases
        self.poll_interval = poll_interval
        self._wake = asyncio.Event()
        self._repo_errors: dict[str, str] = {}
        self.clock = clock
        self.channel_files = tuple(channel_files)
        # The limit is per IP address (or per token), shared by every repo:
        # until this time no repo is asked
        self.paused_until = 0.0

    # --- loop ---

    async def run(self) -> None:
        # Files left by a crash are useless: a release is always downloaded again before a post
        shutil.rmtree(self.download_dir, ignore_errors=True)
        await self.report_stuck()
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - one bad poll must not stop the service
                log.exception("poll failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def wake(self) -> None:
        self._wake.set()

    async def backfill(self) -> None:
        """First run: remember every release that exists now, post none of them."""
        for repo in self.repos:
            backfilled, _ = await self.db.get_repo(repo)
            if not backfilled:
                await self.check_repo(repo)

    async def poll_once(self) -> None:
        for repo in self.repos:
            if self.clock() < self.paused_until:
                break
            try:
                await self.check_repo(repo)
                self._repo_errors.pop(repo, None)
            except RateLimited as exc:
                await self.rate_limited(exc)
                break
            except Exception as exc:  # noqa: BLE001
                error = redact(f"{type(exc).__name__}: {exc}")
                log.warning("%s: %s", repo, error)
                if self._repo_errors.get(repo) != error:
                    self._repo_errors[repo] = error
                    await self.notify(f"⚠️ {repo}: не вдалося прочитати релізи з GitHub\n{error}")
        for row in await self.db.with_status("new", "verified"):
            await self.process(row)

    async def rate_limited(self, exc: RateLimited) -> None:
        """Waits quietly until GitHub gives requests again; one warning per limit hit."""
        now = self.clock()
        if exc.retry_after is not None:
            until = now + exc.retry_after
        elif exc.reset_at is not None:
            until = exc.reset_at
        else:
            until = now + RATE_LIMIT_FALLBACK
        until = max(until, now + 1)
        self.paused_until = until
        when = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%H:%M UTC")
        log.warning("GitHub rate limit, next request at %s: %s", when, redact(str(exc)))
        await self.notify(
            f"⏳ GitHub: ліміт запитів вичерпано, наступна перевірка о {when}.\n"
            "Без GITHUB_TOKEN ліміт 60 запитів на годину на IP-адресу сервера, спільний з іншими програмами на ній. "
            "Токен (install.sh спитає його) дає 5000."
        )

    async def check_repo(self, repo: str) -> None:
        backfilled, etag = await self.db.get_repo(repo)
        releases, new_etag = await self.github.list_releases(repo, etag if backfilled else None)
        if releases is None:
            return
        releases = [release for release in releases if not release.get("draft")]
        for release in releases:
            data = compact(release)
            tag = data["tag_name"]
            if not backfilled:
                status = "skipped"
            elif data["prerelease"] and not self.publish_prereleases:
                status = "skipped"
            else:
                status = "new"
            added = await self.db.add_release(repo, tag, status, data, data["published_at"])
            if added and status == "new":
                log.info("%s %s: new release", repo, tag)
            elif not added:
                await self.db.update_payload(repo, tag, data)
        if not backfilled:
            log.info("%s: backfill, %d existing releases will not be posted", repo, len(releases))
        await self.db.set_repo(repo, True, new_etag)

    async def report_stuck(self) -> None:
        for row in await self.db.with_status("posting"):
            await self.notify(
                f"⚠️ {row.repo} {row.tag}: публікацію перервано, пост міг уже з'явитися в каналі.\n"
                f"Бот не надсилає його вдруге сам. Перевірте канал і надішліть\n"
                f"/mark_posted {row.repo} {row.tag}, якщо пост є;\n"
                f"/repost {row.repo} {row.tag}, щоб опублікувати знову."
            )

    # --- one release ---

    async def process(self, row: ReleaseRow) -> None:
        release = row.payload
        workdir = self.download_dir / row.repo.replace("/", "_") / re.sub(r"[^\w.-]", "_", row.tag)
        try:
            files = await self.fetch_files(release, workdir)
            await self.db.set_status(row.repo, row.tag, "verified", expected=("new", "verified"))
            await self.post(row, release, files)
        except Incomplete as exc:
            if await self.db.set_error(row.repo, row.tag, str(exc)):
                log.info("%s %s: waiting, %s", row.repo, row.tag, exc)
        except Rejected as exc:
            log.error("%s %s: rejected, %s", row.repo, row.tag, exc)
            await self.db.set_status(row.repo, row.tag, "failed", error=str(exc))
            await self.notify(
                f"❌ {row.repo} {row.tag}: не опубліковано.\n{exc}\n"
                f"Після виправлення: /repost {row.repo} {row.tag}"
            )
        except GitHubError as exc:
            # Network trouble on download: the next poll tries again
            if await self.db.set_error(row.repo, row.tag, str(exc)):
                log.warning("%s %s: %s", row.repo, row.tag, exc)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def fetch_files(self, release: dict, workdir: Path) -> list[Path]:
        """Downloads the files SHA256SUMS lists, checks each one, returns the ones for the channel.

        A release is posted only as a whole: every listed file present and every
        checksum right (SHA256SUMS and the digest GitHub computed on upload),
        the files that stay on GitHub included. Only the files that match
        channel_files go into the post, SHA256SUMS last (the caption sits under it).
        """
        assets = {asset["name"]: asset for asset in release["assets"] if asset.get("state", "uploaded") == "uploaded"}
        if SUMS_NAME not in assets:
            raise Incomplete(f"no {SUMS_NAME} yet")
        workdir.mkdir(parents=True, exist_ok=True)
        sums_path = workdir / SUMS_NAME
        await self.github.download(assets[SUMS_NAME]["url"], sums_path, 1024 * 1024)
        sums = parse_sums(sums_path.read_text(encoding="utf-8", errors="replace"))

        missing = [name for name in sums if name not in assets]
        if missing:
            raise Incomplete(f"files not uploaded yet: {', '.join(sorted(missing))}")
        posted = [name for name in sorted(sums) if for_channel(name, self.channel_files)]
        with_sums = for_channel(SUMS_NAME, self.channel_files)
        count = len(posted) + (1 if with_sums else 0)
        if count == 0:
            raise Rejected(f"no file of the release matches CHANNEL_FILES ({', '.join(self.channel_files)})")
        if count > MAX_FILES:
            raise Rejected(f"{count} files for the channel, one post holds {MAX_FILES}")
        too_big = [name for name in posted if assets[name]["size"] > MAX_FILE_SIZE]
        if too_big:
            raise Rejected(f"larger than 2000 MB: {', '.join(too_big)}")
        need = sum(assets[name]["size"] for name in sums) + FREE_SPACE_MARGIN
        free = shutil.disk_usage(workdir).free
        if free < need:
            raise GitHubError(f"not enough disk space: {free // 2**20} MB free, {need // 2**20} MB needed")

        files = []
        for name in sorted(sums):
            # Every file is checked, also the ones that stay on GitHub
            path = workdir / name
            actual, size = await self.github.download(assets[name]["url"], path, MAX_FILE_SIZE)
            if actual != sums[name]:
                raise Rejected(f"{name}: SHA-256 {actual} does not match {SUMS_NAME} ({sums[name]})")
            digest = assets[name].get("digest")
            if digest and digest.startswith("sha256:") and digest[7:].lower() != actual:
                raise Rejected(f"{name}: SHA-256 {actual} does not match the GitHub digest {digest[7:]}")
            if size != assets[name]["size"]:
                raise Rejected(f"{name}: {size} bytes downloaded, GitHub lists {assets[name]['size']}")
            if name in posted:
                files.append(path)
        if with_sums:
            files.append(sums_path)
        return files

    async def post(self, row: ReleaseRow, release: dict, files: list[Path]) -> None:
        # The claim is the guard against duplicates: only one path moves verified -> posting,
        # and a release left in 'posting' is never sent again without the admin
        if not await self.db.set_status(row.repo, row.tag, "posting", expected=("verified",)):
            return
        caption = build_caption(release_title(row.repo, release), notes_to_text(release["body"]), release["html_url"])
        try:
            message_ids = await self.publisher.send(files, caption)
        except TelegramRetryAfter as exc:
            await self.db.set_status(row.repo, row.tag, "verified", error=f"flood wait {exc.retry_after} s")
            return
        except (TelegramNetworkError, TelegramServerError) as exc:
            # Timeout or a server error: the post may be in the channel, so ask the admin
            error = redact(f"{type(exc).__name__}: {exc}")
            log.error("%s %s: post state unknown, %s", row.repo, row.tag, error)
            await self.db.set_error(row.repo, row.tag, error)
            await self.report_stuck()
            return
        except TelegramAPIError as exc:
            # Telegram refused the post, nothing reached the channel
            error = redact(f"{type(exc).__name__}: {exc}")
            await self.db.set_status(row.repo, row.tag, "failed", error=error)
            await self.notify(f"❌ {row.repo} {row.tag}: Telegram не прийняв пост.\n{error}\n/repost {row.repo} {row.tag}")
            return
        await self.db.set_status(row.repo, row.tag, "posted", message_ids=message_ids)
        log.info("%s %s: posted, messages %s", row.repo, row.tag, message_ids)
        try:
            await self.publisher.pin(message_ids[0])
        except TelegramAPIError as exc:
            await self.notify(f"⚠️ {row.repo} {row.tag}: пост є, але не закріплено.\n{redact(str(exc))}")

    # --- admin ---

    def resolve_repo(self, name: str) -> str | None:
        name = name.lower()
        for repo in self.repos:
            if repo.lower() == name or repo.split("/")[-1].lower() == name:
                return repo
        matches = [repo for repo in self.repos if name in repo.lower()]
        return matches[0] if len(matches) == 1 else None

    async def mark_posted(self, repo: str, tag: str, message_id: int | None) -> bool:
        ids = [message_id] if message_id else []
        return await self.db.set_status(repo, tag, "posted", message_ids=ids)

    async def repost(self, repo: str, tag: str) -> bool:
        ok = await self.db.set_status(repo, tag, "new", message_ids=[])
        if ok:
            # Forget the ETag so the next poll reads the release again (fixed files)
            await self.db.set_repo(repo, True, None)
            self.wake()
        return ok

    async def notify(self, text: str) -> None:
        await self.publisher.notify(self.admin_ids, html.escape(redact(text)))
