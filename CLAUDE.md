# CLAUDE.md

Робочі нотатки для того, хто змінює цей репозиторій: як він влаштований і на що вже наступали.
Мова: код, коментарі, назви, повідомлення комітів англійською; тексти для людей (повідомлення адміну, підпис поста, install.sh, README, .env.example) українською.

## 1. Що це за проєкт

Сервіс, що публікує релізи MZGram з GitHub у Telegram-канал. Публічний репозиторій `dmytrokurochkin/mzgram-release-bot`, секретів у ньому немає. Працює на сервері користувача поруч із уже запущеним локальним `telegram-bot-api`.

| Що | Чим |
|---|---|
| Telegram | aiogram 3.29.1 через локальний Bot API (файли до 2000 МБ) |
| GitHub | REST API з ETag, опитування, без вебхуків |
| Стан | SQLite через aiosqlite |
| Сервер | Debian 12+, systemd, `install.sh` |

## 2. Структура репозиторія

```
main.py                 # точка входу: --backfill або бот + цикл опитування
core/config.py          # змінні з .env на рівні модуля
core/loader.py          # create_bot(): Bot на локальному Bot API
core/lock.py            # InstanceLock: flock, одна копія на DATA_DIR
core/utils.py           # redact() і RedactFilter: токени не потрапляють у логи
database.py             # Database: таблиці releases і repos, переходи статусів
services/github.py      # GitHubClient: список релізів з ETag, потокове завантаження з SHA-256
services/publisher.py   # підпис поста (1024 символи), Publisher: media group, pin, повідомлення адміну
services/releases.py    # ReleaseService: опитування, backfill, перевірка файлів, публікація без дублів
handlers/admin.py       # admin_router: /status /check /repost /mark_posted
install.sh              # встановлення і оновлення однією командою
deploy/*.service        # systemd unit з обмеженнями
tests/                  # pytest + mock_servers.py (підроблені Bot API і GitHub)
tests/integration/      # Debian 12 з systemd для перевірки install.sh у CI
```

## 3. Конфігурація (.env)

| Змінна | Обов'язкова | Призначення |
|---|---|---|
| `BOT_TOKEN` | так | токен бота |
| `CHANNEL_ID` | так | канал, install.sh зберігає числовий ID |
| `BOT_API_URL` | ні | локальний Bot API, типово `http://127.0.0.1:8081` |
| `ADMIN_IDS` | ні | адміни через кому |
| `REPOS` | ні | репозиторії через кому |
| `POLL_INTERVAL` | ні | секунди між опитуваннями, 300 |
| `PUBLISH_PRERELEASES` | ні | `true` публікує і prerelease |
| `GITHUB_TOKEN` | ні | більше лімітів GitHub |
| `GITHUB_API_URL` | ні | для тестів (підроблений GitHub) |
| `DATA_DIR` | ні | ставить systemd: `/var/lib/mzgram-release-bot` |
| `UPLOAD_TIMEOUT` | ні | секунди на надсилання поста, 7200 |

## 4. Як це працює

1. `poll_once()`: для кожного репозиторію `GET /repos/{repo}/releases` з `If-None-Match`. 304 нічого не змінює.
2. Репозиторій, який ще не пройшов backfill: усі релізи записуються як `skipped`, нічого не публікується.
3. Нові релізи: `new` (prerelease без `PUBLISH_PRERELEASES`: `skipped`; чернетки не записуються).
4. `process()` для `new`/`verified`: завантажити `SHA256SUMS`, перевірити, що всі файли з нього є в релізі (інакше `Incomplete`, реліз чекає), завантажити кожен, звірити SHA-256 з `SHA256SUMS` і з `digest` GitHub, розмір (інакше `Rejected`, `failed`).
5. `post()`: атомарний перехід `verified -> posting` (`set_status(..., expected=("verified",))`), один `sendMediaGroup`, потім `posted` з message_ids, потім pin першого повідомлення.
6. Завантаження видаляються після кожного релізу і при старті.

## 5. Пастки, про які треба знати наперед

- **Дублі.** Реліз у `posting` ніколи не надсилається повторно автоматично. Тайм-аут або 5xx від Bot API означає "невідомо, чи пост є", тому реліз лишається в `posting`, адмін вирішує `/mark_posted` або `/repost`. Лише явна відмова Telegram (`TelegramBadRequest` тощо) веде в `failed`, а `RetryAfter` назад у `verified`.
- **Підпис.** Ліміт 1024 рахується у видимих символах UTF-16 після розбору HTML. `build_caption()` ріже простий текст, потім екранує. Підпис ставиться на останній документ групи.
- **`InputMediaDocument` заморожений** (pydantic frozen): caption задавати в конструкторі, не присвоєнням.
- **Pin у каналі** потребує права `can_edit_messages`, не `can_pin_messages`. install.sh перевіряє саме його.
- **logOut і api.telegram.org.** Будь-який запит до хмарного API знову підключає бота там. Тому install.sh перевіряє хмару лише раз (маркер `/var/lib/mzgram-release-bot/.cloud-logout-checked`) і лише з терміналу або `MZGRAM_LOGOUT=yes`.
- **Токен у install.sh** передається curl через `--config -` (stdin), у awk через `ENVIRON`, ніколи в аргументах процесу. Помилки curl відкидаються, бо можуть містити URL з токеном.
- **Змінні в `$(...)`** не повертаються з підоболонки: функції install.sh, які мають встановити глобальну змінну (`try_get_me`, `check_channel`), викликаються без `$(...)`.
- **`git reset --hard` в /opt** не чіпає `.env` і `.venv`, бо вони в `.gitignore`. Не додавати `git clean`.
- **ETag і `/repost`.** Після `/repost` ETag репозиторію скидається, щоб наступне опитування прочитало виправлені файли.

## 6. Правила

- Нова логіка разом із тестом у `tests/`. Перед комітом: `python -m pytest`, `shellcheck -x install.sh tests/integration/scenario.sh`.
- Перевірка install.sh: CI-джоба `install` (Debian 12, systemd, підроблені сервери). Локально без Docker її не запустити.
- Не запускати бота з реальним токеном і реальним каналом для перевірок.
- Коміти: Conventional Commits, тема англійською; без трейлерів співавторства.
