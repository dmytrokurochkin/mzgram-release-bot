#!/usr/bin/env bash
# Встановлення і оновлення MZGram release bot на Debian 12+.
#
#   curl -fsSL https://raw.githubusercontent.com/dmytrokurochkin/mzgram-release-bot/main/install.sh | sudo bash
#
# Повторний запуск оновлює код і залежності; .env і історія релізів лишаються.
# Питання читаються з /dev/tty, тому скрипт працює з конвеєра. Без терміналу
# відповіді беруться зі змінних середовища (sudo їх не передає, див. README):
#   BOT_TOKEN, CHANNEL_ID, BOT_API_URL, ADMIN_IDS
#   необов'язкові: GITHUB_TOKEN, REPOS, POLL_INTERVAL, PUBLISH_PRERELEASES, GITHUB_API_URL, UPLOAD_TIMEOUT
#   MZGRAM_LOGOUT=yes|no: вивести бота з api.telegram.org без питання / не перевіряти
# Токен ніде не друкується і не потрапляє в аргументи процесів.

set -euo pipefail

APP_NAME=mzgram-release-bot
APP_USER=mzgram-release
APP_DIR=/opt/mzgram-release-bot
STATE_DIR=/var/lib/mzgram-release-bot
ENV_FILE=$APP_DIR/.env
UNIT_FILE=/etc/systemd/system/$APP_NAME.service
REPO_URL=${MZGRAM_REPO_URL:-https://github.com/dmytrokurochkin/mzgram-release-bot.git}
BRANCH=${MZGRAM_BRANCH:-main}
CLOUD_API_URL=${MZGRAM_CLOUD_API_URL:-https://api.telegram.org}
LOGOUT_MARKER=$STATE_DIR/.cloud-logout-checked
INSTALL_CMD="curl -fsSL https://raw.githubusercontent.com/dmytrokurochkin/mzgram-release-bot/main/install.sh | sudo bash"

TOKEN=""
API_URL=""
BOT_ID=""
BOT_USERNAME=""
CHANNEL=""
ADMINS=""
HAVE_TTY=0
LAST_ERROR=""

info() { printf '==> %s\n' "$*"; }
warn() { printf 'УВАГА: %s\n' "$*" >&2; }
die() {
    printf 'ПОМИЛКА: %s\n' "$*" >&2
    exit 1
}

ask() {
    local answer
    printf '%s' "$1" >/dev/tty
    IFS= read -r answer </dev/tty || answer=""
    printf '%s' "$answer"
}

ask_secret() {
    local answer
    printf '%s' "$1" >/dev/tty
    IFS= read -rs answer </dev/tty || answer=""
    printf '\n' >/dev/tty
    printf '%s' "$answer"
}

confirm() {
    local answer
    answer=$(ask "$1 [y/N]: ")
    [[ $answer =~ ^([Yy]|[Тт]ак) ]]
}

# The value of KEY in .env (the last one wins, quotes stripped). Never sourced: .env is data.
env_get() {
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | sed -e "s/^[\"']//" -e "s/[\"']\$//"
}

# A JSON field from stdin: json_get ok, json_get result username. Booleans print as true/false.
json_get() {
    python3 -c '
import json, sys
try:
    value = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
for key in sys.argv[1:]:
    value = value.get(key) if isinstance(value, dict) else None
if isinstance(value, bool):
    value = "true" if value else "false"
print("" if value is None else value)
' "$@"
}

# bot_api BASE METHOD [curl args]: the URL with the token goes to curl through stdin,
# so it never shows in the process list; curl's own errors are dropped, they may echo the URL.
bot_api() {
    local base=$1 method=$2
    shift 2
    printf 'url = "%s/bot%s/%s"\n' "$base" "$TOKEN" "$method" |
        curl -sS --max-time 20 --config - "$@" 2>/dev/null
}

detect_tty() {
    if [ "${MZGRAM_NONINTERACTIVE:-0}" != 1 ] && (exec </dev/tty) 2>/dev/null; then
        HAVE_TTY=1
    fi
}

install_packages() {
    local need=() package
    for package in git python3 python3-venv curl ca-certificates; do
        if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q "install ok installed"; then
            need+=("$package")
        fi
    done
    if [ ${#need[@]} -gt 0 ]; then
        info "Встановлюю пакети: ${need[*]}"
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}" >/dev/null
    fi
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
        die "потрібен Python 3.10 або новіший (Debian 12+)"
}

create_user() {
    if ! id -u "$APP_USER" >/dev/null 2>&1; then
        info "Створюю системного користувача $APP_USER"
        useradd --system --user-group --home-dir "$STATE_DIR" --no-create-home \
            --shell /usr/sbin/nologin "$APP_USER"
    fi
    install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$STATE_DIR"
}

update_code() {
    if [ -d "$APP_DIR/.git" ]; then
        info "Оновлюю код у $APP_DIR"
        git -C "$APP_DIR" remote set-url origin "$REPO_URL"
        git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
        # .env, .venv are ignored by git: reset keeps them
        git -C "$APP_DIR" reset --quiet --hard FETCH_HEAD
    else
        if [ -e "$APP_DIR" ] && [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
            die "$APP_DIR вже існує і це не git-копія бота; перенесіть її і запустіть знову"
        fi
        info "Завантажую код у $APP_DIR"
        git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
    fi
    chmod 755 "$APP_DIR"
    if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
        info "Створюю virtualenv"
        python3 -m venv "$APP_DIR/.venv"
    fi
    info "Встановлюю залежності Python"
    "$APP_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check -r "$APP_DIR/requirements.txt"
}

read_token() {
    TOKEN=${BOT_TOKEN:-$(env_get BOT_TOKEN)}
    if [ -z "$TOKEN" ]; then
        [ "$HAVE_TTY" = 1 ] || die "немає BOT_TOKEN: запустіть у терміналі або передайте BOT_TOKEN"
        TOKEN=$(ask_secret "Токен бота від @BotFather (не показується): ")
    fi
    [[ $TOKEN =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] || die "BOT_TOKEN має вигляд 123456789:AA... (від @BotFather)"
}

# getMe on one server. Sets BOT_ID and BOT_USERNAME; LAST_ERROR says why it failed.
try_get_me() {
    local base=$1 body
    if ! body=$(bot_api "$base" getMe); then
        LAST_ERROR="$base не відповідає"
        return 1
    fi
    if [ "$(json_get ok <<<"$body")" != true ]; then
        LAST_ERROR="$base: $(json_get description <<<"$body")"
        return 1
    fi
    BOT_ID=$(json_get result id <<<"$body")
    BOT_USERNAME=$(json_get result username <<<"$body")
}

find_local_api() {
    local candidates url attempt seen
    candidates=("${BOT_API_URL:-}" "$(env_get BOT_API_URL)" "http://127.0.0.1:8081" "http://localhost:8081")
    for attempt in 1 2 3; do
        seen=" "
        for url in "${candidates[@]}"; do
            url=${url%/}
            [ -n "$url" ] || continue
            case "$seen" in *" $url "*) continue ;; esac
            seen+="$url "
            if [[ $url == *api.telegram.org* ]]; then
                warn "$url це хмарний Bot API (файли до 50 МБ), потрібен локальний telegram-bot-api"
                continue
            fi
            if ! try_get_me "$url" && [[ $LAST_ERROR == *Unauthorized* ]]; then
                warn "$LAST_ERROR"
                [ "$HAVE_TTY" = 1 ] || die "сервер $url не прийняв BOT_TOKEN"
                TOKEN=$(ask_secret "Сервер не прийняв токен. Введіть токен ще раз: ")
                [[ $TOKEN =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] || die "BOT_TOKEN має вигляд 123456789:AA..."
                try_get_me "$url" || true
            fi
            if [ -n "$BOT_ID" ]; then
                API_URL=$url
                info "Локальний Bot API: $API_URL, бот @$BOT_USERNAME"
                return 0
            fi
            warn "$LAST_ERROR"
        done
        if [ "$HAVE_TTY" != 1 ] || [ "$attempt" -eq 3 ]; then
            break
        fi
        candidates=("$(ask "URL локального telegram-bot-api (наприклад http://127.0.0.1:8081): ")")
    done
    die "не знайдено локальний telegram-bot-api. Вкажіть BOT_API_URL (сервер має бути запущений з --local)"
}

# A bot that still works through api.telegram.org can lose updates to it, so it
# must log out there once. Checked on the first install only: every request to
# the cloud API logs the bot back in there.
cloud_logout() {
    local mode=${MZGRAM_LOGOUT:-ask} body
    [ ! -f "$LOGOUT_MARKER" ] || return 0
    if [ "$mode" = no ]; then
        return 0
    fi
    if [ "$mode" != yes ] && [ "$HAVE_TTY" != 1 ]; then
        warn "не перевірено, чи бот ще на api.telegram.org; для перевірки і logOut: MZGRAM_LOGOUT=yes"
        return 0
    fi
    info "Перевіряю, чи бот ще працює через хмарний api.telegram.org"
    if ! body=$(bot_api "$CLOUD_API_URL" getMe); then
        warn "api.telegram.org недоступний, перевірку пропущено"
        return 0
    fi
    if [ "$(json_get ok <<<"$body")" != true ]; then
        info "На api.telegram.org бота немає, logOut не потрібен"
        touch "$LOGOUT_MARKER"
        return 0
    fi
    cat <<'EOF'

Бот ще підключений до хмарного api.telegram.org. Поки він там, частина
оновлень (команди адміна) може йти туди, а не на локальний сервер.
logOut виводить бота з api.telegram.org: на локальному сервері він працює
одразу, а повернутися на api.telegram.org зможе лише через 10 хвилин.
Інші програми, які використовують цей токен через api.telegram.org, перестануть працювати.
EOF
    if [ "$mode" != yes ] && ! confirm "Вивести бота з api.telegram.org (logOut)?"; then
        warn "logOut пропущено; щоб зробити його пізніше: MZGRAM_LOGOUT=yes і запустіть встановлення знову"
        return 0
    fi
    body=$(bot_api "$CLOUD_API_URL" logOut) || die "logOut не вдався: api.telegram.org не відповідає"
    [ "$(json_get ok <<<"$body")" = true ] || die "logOut не вдався: $(json_get description <<<"$body")"
    info "Бота виведено з api.telegram.org"
    touch "$LOGOUT_MARKER"
}

# Checks the channel and the bot's rights there. Sets CHANNEL to the numeric chat id.
check_channel() {
    local chat=$1 body ok status title id post edit problems=""
    body=$(bot_api "$API_URL" getChat --data-urlencode "chat_id=$chat") || {
        LAST_ERROR="Bot API не відповідає"
        return 1
    }
    if [ "$(json_get ok <<<"$body")" != true ]; then
        LAST_ERROR="канал $chat недоступний: $(json_get description <<<"$body"). Додайте бота в канал адміністратором"
        return 1
    fi
    id=$(json_get result id <<<"$body")
    title=$(json_get result title <<<"$body")
    body=$(bot_api "$API_URL" getChatMember --data-urlencode "chat_id=$id" --data-urlencode "user_id=$BOT_ID") || {
        LAST_ERROR="Bot API не відповідає"
        return 1
    }
    ok=$(json_get ok <<<"$body")
    status=$(json_get result status <<<"$body")
    post=$(json_get result can_post_messages <<<"$body")
    edit=$(json_get result can_edit_messages <<<"$body")
    if [ "$ok" != true ]; then
        LAST_ERROR="не вдалося перевірити права бота: $(json_get description <<<"$body")"
        return 1
    fi
    if [ "$status" = creator ]; then
        post=true
        edit=true
    elif [ "$status" != administrator ]; then
        LAST_ERROR="бот не адміністратор каналу \"$title\""
        return 1
    fi
    [ "$post" = true ] || problems+=" публікація повідомлень;"
    # Pinning a post in a channel needs the right to edit messages
    [ "$edit" = true ] || problems+=" редагування повідомлень (потрібне для закріплення);"
    if [ -n "$problems" ]; then
        LAST_ERROR="у каналі \"$title\" боту бракує прав:$problems"
        return 1
    fi
    info "Канал \"$title\" ($id): бот адміністратор, може публікувати і закріплювати"
    CHANNEL=$id
}

read_channel() {
    local chat attempt again
    chat=${CHANNEL_ID:-$(env_get CHANNEL_ID)}
    for attempt in 1 2 3; do
        if [ -z "$chat" ]; then
            [ "$HAVE_TTY" = 1 ] || die "немає CHANNEL_ID"
            chat=$(ask "ID каналу (-100...) або його @username: ")
        fi
        if [[ ! $chat =~ ^(-100[0-9]{5,}|@[A-Za-z][A-Za-z0-9_]{3,})$ ]]; then
            warn "CHANNEL_ID має вигляд -1001234567890 або @channel"
            [ "$HAVE_TTY" = 1 ] || die "неправильний CHANNEL_ID"
            chat=""
            continue
        fi
        if check_channel "$chat"; then
            return 0
        fi
        warn "$LAST_ERROR"
        [ "$HAVE_TTY" = 1 ] || die "канал не готовий"
        [ "$attempt" -lt 3 ] || break
        again=$(ask "Виправте права в Telegram і натисніть Enter, або введіть інший ID каналу: ")
        [ -z "$again" ] || chat=$again
    done
    die "канал не готовий: $LAST_ERROR"
}

read_admins() {
    ADMINS=${ADMIN_IDS:-$(env_get ADMIN_IDS)}
    if [ -z "$ADMINS" ] && [ ! -f "$ENV_FILE" ] && [ "$HAVE_TTY" = 1 ]; then
        ADMINS=$(ask "Ваш Telegram ID для команд /status і /repost (кілька через кому, Enter щоб пропустити): ")
    fi
    ADMINS=${ADMINS// /}
    [[ -z $ADMINS || $ADMINS =~ ^[0-9]+(,[0-9]+)*$ ]] || die "ADMIN_IDS: числа через кому"
}

write_env() {
    local base tmp key keys=(BOT_TOKEN CHANNEL_ID BOT_API_URL ADMIN_IDS) value
    for key in GITHUB_TOKEN REPOS POLL_INTERVAL PUBLISH_PRERELEASES GITHUB_API_URL UPLOAD_TIMEOUT; do
        [ -z "${!key:-}" ] || keys+=("$key")
    done
    base=$ENV_FILE
    [ -f "$base" ] || base=$APP_DIR/.env.example
    tmp=$(mktemp "$APP_DIR/.env.XXXXXX")
    (
        export MZ_VAL_BOT_TOKEN="$TOKEN" MZ_VAL_CHANNEL_ID="$CHANNEL" MZ_VAL_BOT_API_URL="$API_URL" MZ_VAL_ADMIN_IDS="$ADMINS"
        for key in "${keys[@]:4}"; do
            value=${!key}
            [[ $value != *$'\n'* ]] || die "$key має один рядок"
            export "MZ_VAL_$key=$value"
        done
        # Values come through ENVIRON, not awk -v: they stay out of the process list
        awk -v keys="${keys[*]}" '
            BEGIN { n = split(keys, k, " "); for (i = 1; i <= n; i++) want[k[i]] = 1 }
            {
                if (match($0, /^[A-Z_]+=/)) {
                    key = substr($0, 1, RLENGTH - 1)
                    if (key in want) {
                        if (!(key in done)) print key "=" ENVIRON["MZ_VAL_" key]
                        done[key] = 1
                        next
                    }
                }
                print
            }
            END { for (i = 1; i <= n; i++) if (!(k[i] in done)) print k[i] "=" ENVIRON["MZ_VAL_" k[i]] }
        ' "$base" >"$tmp"
    )
    chown "$APP_USER:$APP_USER" "$tmp"
    chmod 600 "$tmp"
    if [ -f "$ENV_FILE" ] && cmp -s "$tmp" "$ENV_FILE"; then
        rm -f "$tmp"
    else
        mv -f "$tmp" "$ENV_FILE"
        info "Налаштування збережено в $ENV_FILE (права 600)"
    fi
    chown "$APP_USER:$APP_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
}

install_unit() {
    if ! cmp -s "$APP_DIR/deploy/$APP_NAME.service" "$UNIT_FILE"; then
        install -m 644 "$APP_DIR/deploy/$APP_NAME.service" "$UNIT_FILE"
        info "Встановлено $UNIT_FILE"
    fi
    systemctl daemon-reload
}

first_backfill() {
    # First install only: remember the releases that exist now, so the channel
    # gets only the ones published after the install
    if [ ! -f "$STATE_DIR/releases.db" ]; then
        info "Запам'ятовую наявні релізи (без публікації)"
        (cd "$APP_DIR" && runuser -u "$APP_USER" -- env DATA_DIR="$STATE_DIR" "$APP_DIR/.venv/bin/python" main.py --backfill) ||
            die "перший запуск не вдався, див. повідомлення вище"
    fi
}

start_service() {
    systemctl enable --quiet "$APP_NAME"
    systemctl restart "$APP_NAME"
    sleep 3
    systemctl --no-pager --lines=15 status "$APP_NAME" || true
    if ! systemctl is-active --quiet "$APP_NAME"; then
        die "сервіс не запустився: journalctl -u $APP_NAME -n 50"
    fi
}

main() {
    [ "$(id -u)" -eq 0 ] || die "потрібен root: $INSTALL_CMD"
    command -v apt-get >/dev/null 2>&1 || die "скрипт для Debian (немає apt-get)"
    command -v systemctl >/dev/null 2>&1 || die "потрібен systemd"
    detect_tty

    install_packages
    create_user
    update_code

    read_token
    find_local_api
    cloud_logout
    read_channel
    read_admins
    write_env

    install_unit
    first_backfill
    start_service

    printf '\n'
    info "Готово. MZGram release bot працює."
    info "Логи: journalctl -u $APP_NAME -f"
    info "Оновлення: $INSTALL_CMD"
}

main "$@"
