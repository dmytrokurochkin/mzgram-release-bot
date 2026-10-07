import hashlib
import json

from tests.conftest import ADMIN, CHANNEL, REPO, files


def admin_texts(bot_api) -> list[str]:
    return [call["params"]["text"] for call in bot_api.of("sendMessage") if call["params"]["chat_id"] == str(ADMIN)]


async def test_first_run_backfills_without_posting(make_service, bot_api, github):
    github.add_release(REPO, "v0.9.0", files("0.9.0"))
    service = await make_service()
    await service.poll_once()
    await service.poll_once()

    assert bot_api.count("sendMediaGroup") == 0
    row = await service.db.get(REPO, "v0.9.0")
    assert row.status == "skipped"


async def test_new_release_is_posted_once_with_all_files_and_pinned(make_service, bot_api, github):
    service = await make_service()
    await service.backfill()
    content = files("1.0.0")
    github.add_release(REPO, "v1.0.0", content, name="MZGram Android 1.0.0", body="## Changes\n- Faster start\n- [Fix](https://x) for crash")
    await service.poll_once()

    assert bot_api.count("sendMediaGroup") == 1
    call = bot_api.of("sendMediaGroup")[0]
    assert call["params"]["chat_id"] == str(CHANNEL)
    uploaded = {item["filename"]: item["sha256"] for item in call["files"].values()}
    for name, data in content.items():
        assert uploaded[name] == hashlib.sha256(data).hexdigest()
    assert "SHA256SUMS" in uploaded
    media = json.loads(call["params"]["media"])
    assert len(media) == 3
    assert all(item["type"] == "document" for item in media)
    assert "caption" not in media[0]
    caption = media[-1]["caption"]
    assert media[-1]["parse_mode"] == "HTML"
    assert "<b>MZGram Android 1.0.0</b>" in caption
    assert "• Faster start" in caption and "• Fix for crash" in caption
    assert "https://github.com/dmytrokurochkin/MZGram-Android/releases/tag/v1.0.0" in caption

    row = await service.db.get(REPO, "v1.0.0")
    assert row.status == "posted"
    assert len(row.message_ids) == 3
    pins = bot_api.of("pinChatMessage")
    assert len(pins) == 1 and pins[0]["params"]["message_id"] == str(row.message_ids[0])
    assert not list((service.download_dir).rglob("*.*")), "downloads are removed after the post"


async def test_second_poll_and_restart_post_nothing(make_service, bot_api, github):
    service = await make_service()
    await service.backfill()
    github.add_release(REPO, "v1.0.0", files())
    await service.poll_once()
    await service.poll_once()
    restarted = await make_service()
    await restarted.poll_once()

    assert bot_api.count("sendMediaGroup") == 1


async def test_crash_between_send_and_record_never_posts_twice(make_service, bot_api, github):
    service = await make_service()
    await service.backfill()
    github.add_release(REPO, "v1.0.0", files())

    original = service.db.set_status

    async def crash_on_posted(repo, tag, status, **kwargs):
        if status == "posted":
            raise SystemExit("power cut")
        return await original(repo, tag, status, **kwargs)

    service.db.set_status = crash_on_posted
    try:
        await service.poll_once()
    except SystemExit:
        pass
    service.db.set_status = original
    assert bot_api.count("sendMediaGroup") == 1

    restarted = await make_service()
    await restarted.report_stuck()
    await restarted.poll_once()
    await restarted.poll_once()

    assert bot_api.count("sendMediaGroup") == 1
    assert (await restarted.db.get(REPO, "v1.0.0")).status == "posting"
    assert any("/mark_posted" in text and "v1.0.0" in text for text in admin_texts(bot_api))

    assert await restarted.mark_posted(REPO, "v1.0.0", 555)
    row = await restarted.db.get(REPO, "v1.0.0")
    assert row.status == "posted" and row.message_ids == [555]


async def test_timeout_on_send_leaves_the_release_for_the_admin(make_service, bot_api, github):
    service = await make_service(publisher=None)
    from core.loader import create_bot
    from services.publisher import Publisher
    from tests.conftest import TOKEN

    bot = create_bot(TOKEN, bot_api.url)
    service.publisher = Publisher(bot, CHANNEL, upload_timeout=1)
    bot_api.delay["sendMediaGroup"] = 3
    try:
        await service.backfill()
        github.add_release(REPO, "v1.0.0", files())
        await service.poll_once()
        await service.poll_once()
    finally:
        await bot.session.close()

    assert bot_api.count("sendMediaGroup") == 1
    assert (await service.db.get(REPO, "v1.0.0")).status == "posting"
    assert any("/repost" in text for text in admin_texts(bot_api))


async def test_wrong_sha256_in_sums_is_not_posted(make_service, bot_api, github):
    service = await make_service()
    await service.backfill()
    content = files()
    apk = next(name for name in content if name.endswith(".apk"))
    github.add_release(REPO, "v1.0.0", content, bad_sums=(apk,))
    await service.poll_once()
    await service.poll_once()

    assert bot_api.count("sendMediaGroup") == 0
    row = await service.db.get(REPO, "v1.0.0")
    assert row.status == "failed" and "SHA-256" in row.error
    assert any("v1.0.0" in text for text in admin_texts(bot_api))


async def test_wrong_github_digest_is_not_posted(make_service, bot_api, github):
    service = await make_service()
    await service.backfill()
    content = files()
    apk = next(name for name in content if name.endswith(".apk"))
    github.add_release(REPO, "v1.0.0", content, bad_digest=(apk,))
    await service.poll_once()

    assert bot_api.count("sendMediaGroup") == 0
    assert "digest" in (await service.db.get(REPO, "v1.0.0")).error


async def test_incomplete_release_waits_and_posts_when_complete(make_service, bot_api, github):
    service = await make_service()
    await service.backfill()
    content = files()
    apk = next(name for name in content if name.endswith(".apk"))
    github.add_release(REPO, "v1.0.0", content, not_uploaded=(apk,))
    await service.poll_once()
    await service.poll_once()

    assert bot_api.count("sendMediaGroup") == 0
    row = await service.db.get(REPO, "v1.0.0")
    assert row.status == "new" and apk in row.error

    # The missing file arrives
    github.releases[REPO].pop(0)
    github.add_release(REPO, "v1.0.0", content)
    await service.poll_once()
    assert bot_api.count("sendMediaGroup") == 1


async def test_release_without_sums_is_not_posted(make_service, bot_api, github):
    service = await make_service()
    await service.backfill()
    github.add_release(REPO, "v1.0.0", files(), with_sums=False)
    await service.poll_once()

    assert bot_api.count("sendMediaGroup") == 0
    assert (await service.db.get(REPO, "v1.0.0")).status == "new"


async def test_more_than_ten_files_is_rejected(make_service, bot_api, github):
    service = await make_service()
    await service.backfill()
    github.add_release(REPO, "v1.0.0", {f"file{i}.bin": bytes([i]) for i in range(10)})
    await service.poll_once()

    assert bot_api.count("sendMediaGroup") == 0
    assert (await service.db.get(REPO, "v1.0.0")).status == "failed"


async def test_prereleases_and_drafts_are_skipped_by_default(make_service, bot_api, github):
    service = await make_service()
    await service.backfill()
    github.add_release(REPO, "v1.1.0-beta.1", files("1.1.0-beta.1"), prerelease=True)
    github.add_release(REPO, "v1.1.0", files("1.1.0"), draft=True)
    await service.poll_once()

    assert bot_api.count("sendMediaGroup") == 0
    assert (await service.db.get(REPO, "v1.1.0-beta.1")).status == "skipped"
    assert await service.db.get(REPO, "v1.1.0") is None


async def test_prereleases_are_posted_when_enabled(make_service, bot_api, github):
    service = await make_service(publish_prereleases=True)
    await service.backfill()
    github.add_release(REPO, "v1.1.0-beta.1", files("1.1.0-beta.1"), prerelease=True)
    await service.poll_once()

    assert bot_api.count("sendMediaGroup") == 1
    assert "(beta)" in json.loads(bot_api.of("sendMediaGroup")[0]["params"]["media"])[-1]["caption"]


async def test_telegram_refusal_marks_failed_and_repost_posts(make_service, bot_api, github):
    service = await make_service()
    await service.backfill()
    github.add_release(REPO, "v1.0.0", files())
    bot_api.fail["sendMediaGroup"] = (400, "Bad Request: not enough rights")
    await service.poll_once()
    await service.poll_once()

    assert (await service.db.get(REPO, "v1.0.0")).status == "failed"
    assert bot_api.count("sendMediaGroup") == 1

    del bot_api.fail["sendMediaGroup"]
    assert await service.repost(REPO, "v1.0.0")
    await service.poll_once()
    assert (await service.db.get(REPO, "v1.0.0")).status == "posted"
    assert bot_api.count("sendMediaGroup") == 2


async def test_pin_failure_keeps_the_post(make_service, bot_api, github):
    service = await make_service()
    await service.backfill()
    github.add_release(REPO, "v1.0.0", files())
    bot_api.fail["pinChatMessage"] = (400, "Bad Request: not enough rights to pin a message")
    await service.poll_once()

    assert (await service.db.get(REPO, "v1.0.0")).status == "posted"
    assert any("закріплено" in text for text in admin_texts(bot_api))


async def test_unchanged_releases_use_etag(make_service, github):
    service = await make_service()
    await service.backfill()
    await service.poll_once()
    await service.poll_once()

    conditional = [request for request in github.requests if request["if_none_match"]]
    assert len(conditional) == 2


async def test_github_error_is_reported_once(make_service, bot_api, github):
    service = await make_service(repos=[REPO, "dmytrokurochkin/missing"])
    await service.poll_once()
    await service.poll_once()

    reports = [text for text in admin_texts(bot_api) if "missing" in text]
    assert len(reports) == 1


async def test_resolve_repo(make_service):
    service = await make_service(repos=[REPO, "dmytrokurochkin/MZGram-Desktop"])
    assert service.resolve_repo("android") == REPO
    assert service.resolve_repo("MZGram-Desktop") == "dmytrokurochkin/MZGram-Desktop"
    assert service.resolve_repo("mzgram") is None
