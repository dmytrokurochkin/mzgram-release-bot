import hashlib
from pathlib import Path

import aiohttp

API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "mzgram-release-bot",
}


class GitHubError(RuntimeError):
    pass


class RateLimited(GitHubError):
    """GitHub refused because the request limit is spent (403 or 429).

    reset_at: when the limit comes back (X-RateLimit-Reset, epoch seconds);
    retry_after: seconds to wait (Retry-After, the secondary limit).
    """

    def __init__(self, message: str, reset_at: float | None, retry_after: float | None):
        super().__init__(message)
        self.reset_at = reset_at
        self.retry_after = retry_after


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class GitHubClient:
    def __init__(self, session: aiohttp.ClientSession, api_url: str, token: str | None = None):
        self.session = session
        self.api_url = api_url.rstrip("/")
        self.token = token

    async def list_releases(self, repo: str, etag: str | None = None) -> tuple[list[dict] | None, str | None]:
        """Newest releases of a repo, or (None, etag) when nothing changed since etag.

        Without a token GitHub allows 60 requests an hour per IP address,
        shared with every other program on that address, and a 304 answer
        counts too. With a token the limit is 5000 an hour and a 304 answer
        to a conditional request does not count.
        """
        headers = dict(API_HEADERS)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if etag:
            headers["If-None-Match"] = etag
        url = f"{self.api_url}/repos/{repo}/releases"
        async with self.session.get(url, params={"per_page": "30"}, headers=headers) as resp:
            if resp.status == 304:
                return None, etag
            if resp.status != 200:
                text = (await resp.text())[:200]
                limited = resp.status == 429 or (
                    resp.status == 403
                    and (resp.headers.get("X-RateLimit-Remaining") == "0" or "rate limit" in text.lower())
                )
                if limited:
                    raise RateLimited(
                        f"{repo}: GitHub answered {resp.status}: {text}",
                        reset_at=_number(resp.headers.get("X-RateLimit-Reset")),
                        retry_after=_number(resp.headers.get("Retry-After")),
                    )
                raise GitHubError(f"{repo}: GitHub answered {resp.status}: {text}")
            return await resp.json(), resp.headers.get("ETag")

    async def download(self, url: str, dest: Path, max_size: int) -> tuple[str, int]:
        """Streams a release file to dest and returns its SHA-256 and size.

        No Authorization header: the files of a public repo are public, and the
        download redirects to another host that must not get the token.
        """
        digest = hashlib.sha256()
        size = 0
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=300)
        async with self.session.get(url, headers={"User-Agent": API_HEADERS["User-Agent"]}, timeout=timeout) as resp:
            if resp.status != 200:
                raise GitHubError(f"download of {dest.name} failed: HTTP {resp.status}")
            with dest.open("wb") as file:
                async for chunk in resp.content.iter_chunked(1 << 20):
                    size += len(chunk)
                    if size > max_size:
                        raise GitHubError(f"{dest.name} is larger than {max_size} bytes")
                    digest.update(chunk)
                    file.write(chunk)
        return digest.hexdigest(), size
