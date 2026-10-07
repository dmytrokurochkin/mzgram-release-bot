"""SQLite state: which release is at which step.

Statuses of a release:
    skipped  - seen during the backfill or a prerelease while they are off; never posted
    new      - waiting for its files (or for SHA256SUMS to list all of them)
    verified - files downloaded and checked, ready to post
    posting  - the post is being sent; after a crash here the bot does NOT send again,
               it asks the admin (/mark_posted or /repost), so the channel never gets a duplicate
    posted   - in the channel, message_ids holds the post
    failed   - bad checksum, too many or too big files, or Telegram refused the post;
               waits for /repost
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    repo TEXT NOT NULL,
    tag TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    published_at TEXT NOT NULL DEFAULT '',
    message_ids TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (repo, tag)
);
CREATE TABLE IF NOT EXISTS repos (
    repo TEXT PRIMARY KEY,
    backfilled INTEGER NOT NULL DEFAULT 0,
    etag TEXT
);
"""


@dataclass
class ReleaseRow:
    repo: str
    tag: str
    status: str
    payload: dict
    published_at: str
    message_ids: list[int]
    error: str | None
    updated_at: float


def _row(row: aiosqlite.Row) -> ReleaseRow:
    return ReleaseRow(
        repo=row["repo"],
        tag=row["tag"],
        status=row["status"],
        payload=json.loads(row["payload"]),
        published_at=row["published_at"],
        message_ids=json.loads(row["message_ids"]),
        error=row["error"],
        updated_at=row["updated_at"],
    )


class Database:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.path)

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            await db.executescript(SCHEMA)
            await db.commit()

    # --- repos ---

    async def get_repo(self, repo: str) -> tuple[bool, str | None]:
        async with self._connect() as db:
            async with db.execute("SELECT backfilled, etag FROM repos WHERE repo = ?", (repo,)) as cur:
                row = await cur.fetchone()
        if row is None:
            return False, None
        return bool(row[0]), row[1]

    async def set_repo(self, repo: str, backfilled: bool, etag: str | None) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO repos (repo, backfilled, etag) VALUES (?, ?, ?) "
                "ON CONFLICT (repo) DO UPDATE SET backfilled = excluded.backfilled, etag = excluded.etag",
                (repo, int(backfilled), etag),
            )
            await db.commit()

    # --- releases ---

    async def add_release(self, repo: str, tag: str, status: str, payload: dict, published_at: str) -> bool:
        """Records a release once; UNIQUE (repo, tag) makes a second insert a no-op."""
        now = time.time()
        async with self._connect() as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO releases (repo, tag, status, payload, published_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo, tag, status, json.dumps(payload), published_at, now, now),
            )
            await db.commit()
            return cur.rowcount == 1

    async def update_payload(self, repo: str, tag: str, payload: dict) -> None:
        # Not while a post is on its way or done: the posted set must stay as it was
        async with self._connect() as db:
            await db.execute(
                "UPDATE releases SET payload = ?, updated_at = ? WHERE repo = ? AND tag = ? AND status IN ('new', 'failed', 'skipped')",
                (json.dumps(payload), time.time(), repo, tag),
            )
            await db.commit()

    async def get(self, repo: str, tag: str) -> ReleaseRow | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM releases WHERE repo = ? AND tag = ?", (repo, tag)) as cur:
                row = await cur.fetchone()
        return _row(row) if row else None

    async def with_status(self, *statuses: str) -> list[ReleaseRow]:
        marks = ",".join("?" for _ in statuses)
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM releases WHERE status IN ({marks}) ORDER BY published_at, created_at",
                statuses,
            ) as cur:
                rows = await cur.fetchall()
        return [_row(row) for row in rows]

    async def recent(self, limit: int = 15) -> list[ReleaseRow]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM releases ORDER BY published_at DESC, created_at DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
        return [_row(row) for row in rows]

    async def set_status(
        self,
        repo: str,
        tag: str,
        status: str,
        *,
        expected: tuple[str, ...] | None = None,
        error: str | None = None,
        message_ids: list[int] | None = None,
    ) -> bool:
        """Moves a release to a status; with expected, only from one of those statuses.

        Returns False when the release was not in an expected status, so two paths
        can never both move it to 'posting'.
        """
        sets = ["status = ?", "error = ?", "updated_at = ?"]
        args: list = [status, error, time.time()]
        if message_ids is not None:
            sets.append("message_ids = ?")
            args.append(json.dumps(message_ids))
        sql = f"UPDATE releases SET {', '.join(sets)} WHERE repo = ? AND tag = ?"
        args += [repo, tag]
        if expected:
            sql += f" AND status IN ({','.join('?' for _ in expected)})"
            args += list(expected)
        async with self._connect() as db:
            cur = await db.execute(sql, args)
            await db.commit()
            return cur.rowcount == 1

    async def set_error(self, repo: str, tag: str, error: str) -> bool:
        """Stores an error that does not change the status; True when the error is new.

        The caller tells the admin only about new errors, not every poll.
        """
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE releases SET error = ?, updated_at = ? WHERE repo = ? AND tag = ? AND (error IS NULL OR error != ?)",
                (error, time.time(), repo, tag, error),
            )
            await db.commit()
            return cur.rowcount == 1
