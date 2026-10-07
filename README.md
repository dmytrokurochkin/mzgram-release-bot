<div align="center">

# MZGram release bot

Публікує нові релізи [MZGram](https://github.com/dmytrokurochkin/MZGram-Android) з GitHub у Telegram-канал: один пост на реліз, з усіма файлами і списком змін.

[![CI](https://github.com/dmytrokurochkin/mzgram-release-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/dmytrokurochkin/mzgram-release-bot/actions/workflows/ci.yml)

</div>

## Що робить

- Кожні 5 хвилин питає GitHub про нові релізи `dmytrokurochkin/MZGram-Android` і `dmytrokurochkin/MZGram-Desktop` (без вебхуків, нічого не відкриває назовні).
- Завантажує файли релізу і перевіряє SHA-256 кожного: за `SHA256SUMS` з релізу і за контрольною сумою, яку GitHub порахував при завантаженні.
- Публікує в канал один пост: усі файли однією групою, під ними назва, зміни і посилання на сторінку релізу (повний список змін там). Пост закріплюється.
- Не публікує реліз, якщо бракує файлу, не збігається контрольна сума, файлів більше 10 або файл більший за 2000 МБ. Адмін отримує повідомлення.
- Ніколи не публікує реліз двічі: стан кожного релізу в SQLite, `UNIQUE (repo, tag)`, працює лише одна копія бота. Якщо зв'язок обірвався під час надсилання, бот не надсилає вдруге сам, а питає адміна.
- Перший запуск лише запам'ятовує наявні релізи: у канал ідуть тільки ті, що вийдуть після встановлення.
- Бета-релізи (prerelease) поки пропускає, вмикається `PUBLISH_PRERELEASES=true`.

Файли до 2000 МБ надсилаються через локальний [telegram-bot-api](https://github.com/tdlib/telegram-bot-api), який уже має працювати на сервері. Інсталятор його не збирає і не змінює, лише знаходить.

## Встановлення на сервер

Debian 12 або новіший, systemd, запущений локальний `telegram-bot-api` з `--local`.

Підготуйте:

1. Бота в [@BotFather](https://t.me/BotFather) і його токен.
2. Канал. Додайте бота адміністратором з правами **Публікація повідомлень** і **Редагування повідомлень** (без другого бот не може закріпити пост).
3. Свій Telegram ID (наприклад, з [@userinfobot](https://t.me/userinfobot)), щоб отримувати помилки і керувати ботом. Натисніть у бота /start.

Одна команда:

```bash
curl -fsSL https://raw.githubusercontent.com/dmytrokurochkin/mzgram-release-bot/main/install.sh | sudo bash
```

Інсталятор:

- ставить `git`, `python3-venv`, `curl`, створює системного користувача `mzgram-release`, кладе код у `/opt/mzgram-release-bot` з virtualenv;
- питає токен (не показується на екрані і не пишеться в логи), ID каналу, ваш Telegram ID;
- знаходить локальний Bot API (`http://127.0.0.1:8081` або питає адресу) і перевіряє токен через `getMe`;
- перевіряє, що бот адміністратор каналу з правом публікувати і закріплювати;
- якщо бот ще підключений до хмарного `api.telegram.org`, пропонує вивести його звідти (`logOut`). Після цього бот 10 хвилин не зможе повернутися на `api.telegram.org`; на локальному сервері працює одразу;
- зберігає налаштування в `/opt/mzgram-release-bot/.env` (права 600), стан у `/var/lib/mzgram-release-bot`;
- встановлює і запускає systemd-сервіс `mzgram-release-bot` з обмеженнями (`ProtectSystem=strict`, без прав root).

Оновлення: та сама команда. Код і залежності оновлюються, `.env` і історія релізів лишаються.

Без терміналу (автоматизація) відповіді передаються змінними; `sudo` їх сам не передає, тому через `env` після `sudo`:

```bash
curl -fsSL https://raw.githubusercontent.com/dmytrokurochkin/mzgram-release-bot/main/install.sh \
  | sudo env BOT_TOKEN=... CHANNEL_ID=-100... ADMIN_IDS=... MZGRAM_LOGOUT=yes bash
```

Так токен видно в історії оболонки і в списку процесів, тому інтерактивний варіант кращий.

## Керування

Логи:

```bash
journalctl -u mzgram-release-bot -f
```

Команди адміна в особистих повідомленнях боту:

| Команда | Що робить |
|---|---|
| `/status` | останні релізи і їхній стан |
| `/check` | перевірити GitHub зараз |
| `/repost <repo> <tag>` | опублікувати реліз (знову), наприклад `/repost android v1.2.0` |
| `/mark_posted <repo> <tag> [message_id]` | позначити реліз опублікованим, нічого не надсилаючи |

Решта налаштувань у [.env.example](.env.example): список репозиторіїв, інтервал, бета-релізи, токен GitHub.

## Що чекає бот від релізу

Реліз на GitHub (не чернетка) з файлом `SHA256SUMS` у форматі `sha256sum`:

```
3f2a...e9  MZGram-Android-1.2.0.apk
9b1c...07  MZGram-Android-1.2.0-rotation.lineage
```

Публікуються всі файли зі списку і сам `SHA256SUMS`. Поки бракує файлу зі списку, реліз чекає. Збірки MZGram створюють реліз як чернетку, завантажують файли і лише потім публікують, тож бот бачить реліз уже повним.

## Розробка

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Тести працюють з підробленими Bot API і GitHub (`tests/mock_servers.py`). CI додатково запускає `install.sh` у контейнері Debian 12 з systemd і перевіряє встановлення, публікацію, повторний запуск і оновлення.

## Ліцензія

MIT
