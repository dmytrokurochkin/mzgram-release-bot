import os
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# handlers.admin reads config on import; the tests never use a real .env
os.environ.setdefault("ADMIN_IDS", "42")

from core.loader import create_bot  # noqa: E402
from database import Database  # noqa: E402
from services.github import GitHubClient  # noqa: E402
from services.publisher import Publisher  # noqa: E402
from services.releases import ReleaseService  # noqa: E402
from tests.mock_servers import FakeBotApi, FakeGitHub  # noqa: E402

REPO = "dmytrokurochkin/MZGram-Android"
CHANNEL = -1001234567890
ADMIN = 42
TOKEN = "123456:TEST-token_value"


@pytest.fixture
async def bot_api():
    api = FakeBotApi()
    server = TestServer(api.app())
    await server.start_server()
    api.url = str(server.make_url("")).rstrip("/")
    yield api
    await server.close()


@pytest.fixture
async def github():
    fake = FakeGitHub()
    fake.add_repo(REPO)
    server = TestServer(fake.app())
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
    yield fake
    await server.close()


@pytest.fixture
async def make_service(tmp_path, bot_api, github):
    """Builds a service over one database; call it again to simulate a restart."""
    sessions = []
    db = Database(tmp_path / "releases.db")
    await db.init()

    async def factory(**kwargs) -> ReleaseService:
        http = aiohttp.ClientSession()
        bot = create_bot(TOKEN, bot_api.url)
        sessions.extend([http, bot.session])
        options = {
            "db": db,
            "github": GitHubClient(http, github.url),
            "publisher": Publisher(bot, CHANNEL, upload_timeout=60),
            "repos": [REPO],
            "download_dir": tmp_path / "downloads",
            "admin_ids": [ADMIN],
            "poll_interval": 1,
        }
        options.update(kwargs)
        return ReleaseService(**options)

    yield factory
    for session in sessions:
        await session.close()


def files(version: str = "1.0.0") -> dict[str, bytes]:
    return {
        f"MZGram-Android-{version}.apk": f"apk {version}".encode() * 1000,
        f"MZGram-Android-{version}-rotation.lineage": b"lineage",
    }
