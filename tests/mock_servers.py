"""Fake Telegram Bot API and GitHub API.

The pytest tests start them in-process. The install.sh check in a Debian
container runs them as separate processes:

    python3 tests/mock_servers.py bot --port 8081
    python3 tests/mock_servers.py github --port 8099 --repo owner/name

Both record what they got; GET /_control/calls returns it. Only aiohttp is
needed, so the container runs them with Debian's python3-aiohttp.
"""

import argparse
import asyncio
import hashlib
import json
import time

from aiohttp import web

BOT_ID = 7000000001


class FakeBotApi:
    def __init__(self, can_post: bool = True, can_edit: bool = True, status: str = "administrator"):
        self.calls: list[dict] = []
        self.tokens: set[str] = set()
        self.next_message_id = 100
        self.member = {"status": status, "can_post_messages": can_post, "can_edit_messages": can_edit}
        self.fail: dict[str, tuple[int, str]] = {}  # method -> (HTTP status, description)
        self.delay: dict[str, float] = {}  # method -> seconds before the answer

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self.handle)
        app.router.add_get("/bot{token}/{method}", self.handle)
        app.router.add_get("/_control/calls", self.control_calls)
        app.router.add_post("/_control/fail", self.control_fail)
        return app

    def count(self, method: str) -> int:
        return sum(1 for call in self.calls if call["method"] == method)

    def of(self, method: str) -> list[dict]:
        return [call for call in self.calls if call["method"] == method]

    async def control_calls(self, request: web.Request) -> web.Response:
        return web.json_response(self.calls)

    async def control_fail(self, request: web.Request) -> web.Response:
        data = await request.json()
        self.fail[data["method"]] = (int(data.get("status", 400)), data.get("description", "Bad Request"))
        return web.json_response({"ok": True})

    @staticmethod
    async def _params(request: web.Request) -> tuple[dict, dict]:
        params: dict = {}
        files: dict = {}
        if request.content_type.startswith("multipart/"):
            reader = await request.multipart()
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.filename:
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := await part.read_chunk(1 << 20):
                        digest.update(chunk)
                        size += len(chunk)
                    files[part.name] = {"filename": part.filename, "sha256": digest.hexdigest(), "size": size}
                else:
                    params[part.name] = await part.text()
        elif request.content_type == "application/json":
            params = await request.json()
        else:
            params = dict(await request.post())
            params.update(request.query)
        return params, files

    def _me(self) -> dict:
        return {"id": BOT_ID, "is_bot": True, "first_name": "MZGram releases", "username": "mzgram_release_test_bot"}

    def _message(self, chat_id, **extra) -> dict:
        self.next_message_id += 1
        chat = {"id": int(chat_id) if str(chat_id).lstrip("-").isdigit() else -1001000000001, "type": "channel", "title": "MZGram"}
        return {"message_id": self.next_message_id, "date": int(time.time()), "chat": chat, **extra}

    async def handle(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        method = request.match_info["method"]
        params, files = await self._params(request)
        self.tokens.add(token)
        if method.lower() != "getupdates":
            self.calls.append({"method": method, "params": params, "files": files})
        if method in self.delay:
            await asyncio.sleep(self.delay[method])
        if method in self.fail:
            status, description = self.fail[method]
            return web.json_response({"ok": False, "error_code": status, "description": description}, status=status)
        name = method.lower()
        if name == "getme":
            result = self._me()
        elif name == "getchat":
            chat_id = params.get("chat_id", "")
            result = {"id": int(chat_id) if str(chat_id).lstrip("-").isdigit() else -1001000000001, "type": "channel", "title": "MZGram"}
        elif name == "getchatmember":
            result = {"user": self._me(), **self.member}
        elif name == "sendmediagroup":
            media = json.loads(params["media"])
            result = [self._message(params["chat_id"], document={"file_id": f"f{i}", "file_unique_id": f"u{i}"}) for i in range(len(media))]
        elif name == "senddocument":
            result = self._message(params["chat_id"], document={"file_id": "f", "file_unique_id": "u"})
        elif name == "sendmessage":
            result = self._message(params["chat_id"], text=params.get("text", ""))
        elif name == "getupdates":
            await asyncio.sleep(min(float(params.get("timeout", 0) or 0), 1.0))
            result = []
        elif name in ("pinchatmessage", "logout", "close", "deletewebhook", "setmycommands"):
            result = True
        else:
            return web.json_response({"ok": False, "error_code": 404, "description": "Not Found: method not found"}, status=404)
        return web.json_response({"ok": True, "result": result})


class FakeGitHub:
    def __init__(self):
        self.releases: dict[str, list[dict]] = {}
        self.blobs: dict[str, bytes] = {}
        self.requests: list[dict] = []
        self.next_id = 1

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/repos/{owner}/{name}/releases", self.list_releases)
        app.router.add_get("/download/{path:.+}", self.download)
        app.router.add_get("/_control/calls", self.control_calls)
        app.router.add_post("/_control/repo", self.control_repo)
        app.router.add_post("/_control/release", self.control_release)
        return app

    def add_repo(self, repo: str) -> None:
        self.releases.setdefault(repo, [])

    def add_release(
        self,
        repo: str,
        tag: str,
        files: dict[str, bytes],
        *,
        body: str = "",
        name: str = "",
        prerelease: bool = False,
        draft: bool = False,
        bad_sums: tuple[str, ...] = (),
        bad_digest: tuple[str, ...] = (),
        not_uploaded: tuple[str, ...] = (),
        with_sums: bool = True,
    ) -> None:
        """A release as the release workflow makes it: the files plus SHA256SUMS.

        bad_sums: SHA256SUMS lists a wrong hash; bad_digest: GitHub's digest is wrong;
        not_uploaded: listed in SHA256SUMS but missing from the release.
        """
        sums = []
        assets = []
        for file_name, content in files.items():
            real = hashlib.sha256(content).hexdigest()
            sums.append(f"{'0' * 64 if file_name in bad_sums else real}  {file_name}\n")
            if file_name in not_uploaded:
                continue
            digest = "1" * 64 if file_name in bad_digest else real
            assets.append(self._asset(repo, tag, file_name, content, digest))
        if with_sums:
            content = "".join(sums).encode()
            assets.append(self._asset(repo, tag, "SHA256SUMS", content, hashlib.sha256(content).hexdigest()))
        self.releases.setdefault(repo, []).insert(0, {
            "id": self.next_id,
            "tag_name": tag,
            "name": name,
            "body": body,
            "draft": draft,
            "prerelease": prerelease,
            "html_url": f"https://github.com/{repo}/releases/tag/{tag}",
            "published_at": f"2026-10-{self.next_id:02d}T12:00:00Z",
            "assets": assets,
        })
        self.next_id += 1

    def _asset(self, repo: str, tag: str, file_name: str, content: bytes, digest: str) -> dict:
        path = f"{repo}/{tag}/{file_name}"
        self.blobs[path] = content
        return {"name": file_name, "size": len(content), "digest": f"sha256:{digest}", "state": "uploaded", "path": path}

    async def list_releases(self, request: web.Request) -> web.Response:
        repo = f"{request.match_info['owner']}/{request.match_info['name']}"
        self.requests.append({"repo": repo, "if_none_match": request.headers.get("If-None-Match"), "auth": "Authorization" in request.headers})
        if repo not in self.releases:
            return web.json_response({"message": "Not Found"}, status=404)
        origin = f"{request.scheme}://{request.host}"
        data = []
        for release in self.releases[repo]:
            release = dict(release)
            release["assets"] = [
                {key: value for key, value in asset.items() if key != "path"} | {"browser_download_url": f"{origin}/download/{asset['path']}"}
                for asset in release["assets"]
            ]
            data.append(release)
        body = json.dumps(data)
        etag = '"' + hashlib.sha256(body.encode()).hexdigest()[:20] + '"'
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag})
        return web.Response(body=body, content_type="application/json", headers={"ETag": etag})

    async def download(self, request: web.Request) -> web.Response:
        path = request.match_info["path"]
        if path not in self.blobs:
            return web.Response(status=404)
        return web.Response(body=self.blobs[path], content_type="application/octet-stream")

    async def control_calls(self, request: web.Request) -> web.Response:
        return web.json_response(self.requests)

    async def control_repo(self, request: web.Request) -> web.Response:
        self.add_repo((await request.json())["repo"])
        return web.json_response({"ok": True})

    async def control_release(self, request: web.Request) -> web.Response:
        data = await request.json()
        files = {name: text.encode() for name, text in data.pop("files").items()}
        for key in ("bad_sums", "bad_digest", "not_uploaded"):
            data[key] = tuple(data.get(key, ()))
        self.add_release(data.pop("repo"), data.pop("tag"), files, **data)
        return web.json_response({"ok": True})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("bot", "github"))
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--repo", action="append", default=[])
    args = parser.parse_args()
    if args.kind == "bot":
        app = FakeBotApi().app()
    else:
        github = FakeGitHub()
        for repo in args.repo:
            github.add_repo(repo)
        app = github.app()
    web.run_app(app, host="127.0.0.1", port=args.port, print=None, access_log=None)


if __name__ == "__main__":
    main()
