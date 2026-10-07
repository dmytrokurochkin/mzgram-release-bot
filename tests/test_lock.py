import pytest

from core.lock import AlreadyRunning, InstanceLock
from core.utils import RedactFilter, add_secret, redact


def test_second_copy_cannot_take_the_lock(tmp_path):
    path = tmp_path / "bot.lock"
    with InstanceLock(path):
        with pytest.raises(AlreadyRunning):
            InstanceLock(path).acquire()
    # Released: the next copy starts
    with InstanceLock(path):
        pass


def test_token_is_hidden_in_logs(caplog):
    import logging

    add_secret("999:SECRET")
    logger = logging.getLogger("redact-test")
    caplog.handler.addFilter(RedactFilter())
    try:
        raise RuntimeError("POST http://127.0.0.1:8081/bot999:SECRET/sendMediaGroup failed")
    except RuntimeError:
        logger.exception("send failed for %s", "http://x/bot999:SECRET/getMe")
    assert "999:SECRET" not in caplog.text
    assert "***" in caplog.text
    assert redact("a 999:SECRET b") == "a *** b"
