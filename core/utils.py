import logging

_secrets: set[str] = set()


def add_secret(value: str | None) -> None:
    if value:
        _secrets.add(value)


def redact(text: str) -> str:
    """Hides the bot token and the GitHub token in text that goes to logs or to Telegram."""
    for secret in _secrets:
        text = text.replace(secret, "***")
    return text


class RedactFilter(logging.Filter):
    # aiohttp errors may carry a request URL, and the Bot API URL holds the token
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
            record.msg = f"{record.msg}\n{redact(exc_text)}"
        return True


def utf16_len(text: str) -> int:
    # Telegram counts message and caption length in UTF-16 code units
    return len(text.encode("utf-16-le")) // 2
