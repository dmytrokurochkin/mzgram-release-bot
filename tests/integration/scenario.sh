#!/usr/bin/env bash
# install.sh check inside the Debian container (see Dockerfile and .github/workflows/ci.yml).
# A fake local Bot API on :8081, a fake api.telegram.org on :8082 and a fake GitHub on :8099.
set -euo pipefail

SRC=/src
SECRET=AAFakeTokenForTheInstallCheck_0123456789
TOKEN=123456789:$SECRET
REPO=dmytrokurochkin/MZGram-Android
APP=mzgram-release-bot
ENV_FILE=/opt/$APP/.env
DB=/var/lib/$APP/releases.db

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    journalctl -u "$APP" --no-pager -n 100 >&2 || true
    exit 1
}
ok() { printf 'ok: %s\n' "$*"; }

count() {
    curl -s "http://127.0.0.1:$1/_control/calls" | python3 -c '
import json, sys
method, text = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None
calls = [c for c in json.load(sys.stdin) if c["method"] == method]
if text is not None:
    calls = [c for c in calls if text in json.dumps(c["params"], ensure_ascii=False)]
print(len(calls))
' "${@:2}"
}

add_release() {
    curl -sf -X POST -H 'Content-Type: application/json' -d "$1" http://127.0.0.1:8099/_control/release >/dev/null
}

wait_for() {
    local seconds=$1
    shift
    for _ in $(seq "$seconds"); do
        if "$@"; then return 0; fi
        sleep 1
    done
    return 1
}

posts_are() { [ "$(count 8081 sendMediaGroup)" = "$1" ]; }

release_files() {
    printf '{"MZGram-Android-%s.apk": "apk %s %s", "MZGram-Android-%s-rotation.lineage": "lineage"}' "$1" "$1" "$(head -c 3000 /dev/zero | tr '\0' x)" "$1"
}

run_install() {
    local log=$1
    shift
    # As on a server: bash reads the script from stdin, with no terminal
    if ! env MZGRAM_REPO_URL="file://$SRC" MZGRAM_BRANCH=ci-test MZGRAM_NONINTERACTIVE=1 "$@" bash <"$SRC/install.sh" >"$log" 2>&1; then
        cat "$log"
        fail "install.sh exited with an error"
    fi
    cat "$log"
}

# --- fakes ---
# /src belongs to the runner's user; git (installed by install.sh) must still clone it.
# git is not there yet, so the setting goes straight to root's config
printf '[safe]
	directory = *
' >>/root/.gitconfig
nohup python3 "$SRC/tests/mock_servers.py" bot --port 8081 >/root/mock-local.log 2>&1 &
nohup python3 "$SRC/tests/mock_servers.py" bot --port 8082 >/root/mock-cloud.log 2>&1 &
nohup python3 "$SRC/tests/mock_servers.py" github --port 8099 --repo "$REPO" --repo dmytrokurochkin/MZGram-Desktop >/root/mock-github.log 2>&1 &
for port in 8081 8082 8099; do
    wait_for 30 curl -sf "http://127.0.0.1:$port/_control/calls" -o /dev/null || fail "fake server on $port did not start"
done

# A release that exists before the install must never reach the channel
add_release "{\"repo\": \"$REPO\", \"tag\": \"v0.9.0\", \"files\": $(release_files 0.9.0)}"

# --- first install ---
run_install /root/install-1.log \
    BOT_TOKEN="$TOKEN" CHANNEL_ID=-1001234567890 ADMIN_IDS=42 \
    GITHUB_API_URL=http://127.0.0.1:8099 POLL_INTERVAL=2 \
    MZGRAM_LOGOUT=yes MZGRAM_CLOUD_API_URL=http://127.0.0.1:8082

systemctl is-active --quiet "$APP" || fail "service is not active"
ok "service is active"
grep -qF "$SECRET" /root/install-1.log && fail "the token is in the install output"
ok "install output has no token"
[ "$(stat -c '%a %U' "$ENV_FILE")" = "600 mzgram-release" ] || fail ".env mode/owner: $(stat -c '%a %U' "$ENV_FILE")"
grep -qx 'BOT_API_URL=http://127.0.0.1:8081' "$ENV_FILE" || fail "local Bot API was not detected"
grep -qx 'CHANNEL_ID=-1001234567890' "$ENV_FILE" || fail "CHANNEL_ID not saved"
ok ".env: 600, owner mzgram-release, Bot API detected"
[ "$(count 8082 logOut)" = 1 ] || fail "cloud logOut calls: $(count 8082 logOut)"
[ "$(count 8081 logOut)" = 0 ] || fail "logOut went to the local server"
ok "logOut on the cloud API once"
[ "$(systemctl show -p ProtectSystem --value "$APP")" = strict ] || fail "ProtectSystem is not strict"
[ "$(systemctl show -p User --value "$APP")" = mzgram-release ] || fail "service user"
ok "unit: ProtectSystem=strict, User=mzgram-release"
python3 -c "import sqlite3,sys; s=sqlite3.connect('$DB').execute(\"select status from releases where tag='v0.9.0'\").fetchone(); sys.exit(s != ('skipped',))" ||
    fail "v0.9.0 was not recorded by the backfill"
sleep 6
posts_are 0 || fail "posted during the backfill"
ok "backfill: the old release is recorded, nothing posted"

# --- a new release: one post with all files, pinned ---
add_release "{\"repo\": \"$REPO\", \"tag\": \"v1.0.0\", \"name\": \"MZGram Android 1.0.0\", \"body\": \"- First release\", \"files\": $(release_files 1.0.0)}"
wait_for 60 posts_are 1 || fail "v1.0.0 was not posted"
wait_for 10 test "$(count 8081 pinChatMessage)" = 1 || fail "post not pinned"
curl -s http://127.0.0.1:8081/_control/calls | python3 -c '
import json, sys, hashlib
call = [c for c in json.load(sys.stdin) if c["method"] == "sendMediaGroup"][0]
names = sorted(f["filename"] for f in call["files"].values())
assert names == ["MZGram-Android-1.0.0-rotation.lineage", "MZGram-Android-1.0.0.apk", "SHA256SUMS"], names
apk = [f for f in call["files"].values() if f["filename"].endswith(".apk")][0]
assert apk["sha256"] == hashlib.sha256(("apk 1.0.0 " + "x" * 3000).encode()).hexdigest()
assert "MZGram Android 1.0.0" in json.loads(call["params"]["media"])[-1]["caption"]
' || fail "the post does not hold the right files"
ok "v1.0.0: one post with the apk, the lineage and SHA256SUMS, pinned"

# --- releases that must not be posted ---
add_release "{\"repo\": \"$REPO\", \"tag\": \"v1.0.1\", \"files\": $(release_files 1.0.1), \"bad_sums\": [\"MZGram-Android-1.0.1.apk\"]}"
wait_for 60 test "$(count 8081 sendMessage v1.0.1)" -ge 1 || fail "the admin was not told about the bad checksum"
add_release "{\"repo\": \"$REPO\", \"tag\": \"v1.0.2\", \"files\": $(release_files 1.0.2), \"not_uploaded\": [\"MZGram-Android-1.0.2.apk\"]}"
add_release "{\"repo\": \"$REPO\", \"tag\": \"v1.1.0-beta.1\", \"prerelease\": true, \"files\": $(release_files 1.1.0-beta.1)}"
sleep 8
posts_are 1 || fail "a bad, incomplete or beta release was posted"
ok "wrong SHA-256, missing file and beta: not posted, admin told about the checksum"

# --- restart: nothing posted again ---
systemctl restart "$APP"
sleep 8
systemctl is-active --quiet "$APP" || fail "service did not come back after restart"
posts_are 1 || fail "posted again after restart"
ok "restart: no new posts"

# --- one copy only ---
set +e
timeout 30 runuser -u mzgram-release -- env DATA_DIR=/var/lib/$APP /opt/$APP/.venv/bin/python /opt/$APP/main.py >/root/second.log 2>&1
code=$?
set -e
if [ "$code" != 1 ] || ! grep -q "already running" /root/second.log; then
    cat /root/second.log
    fail "a second copy started (exit $code)"
fi
ok "second copy refused by the lock"

# --- second install: an update that keeps .env and the history ---
before=$(sha256sum "$ENV_FILE")
rows_before=$(python3 -c "import sqlite3; print(sqlite3.connect('$DB').execute('select count(*) from releases').fetchone()[0])")
run_install /root/install-2.log
[ "$(sha256sum "$ENV_FILE")" = "$before" ] || fail ".env changed on the second install"
[ "$(stat -c '%a %U' "$ENV_FILE")" = "600 mzgram-release" ] || fail ".env mode after the second install"
rows_after=$(python3 -c "import sqlite3; print(sqlite3.connect('$DB').execute('select count(*) from releases').fetchone()[0])")
[ "$rows_after" = "$rows_before" ] || fail "release history changed: $rows_before -> $rows_after"
[ "$(count 8082 logOut)" = 1 ] || fail "logOut repeated on the second install"
systemctl is-active --quiet "$APP" || fail "service is not active after the second install"
sleep 6
posts_are 1 || fail "posted again after the second install"
ok "second install: .env, history and channel unchanged"

# --- the token stays out of the logs ---
journalctl -u "$APP" --no-pager | grep -qF "$SECRET" && fail "the token is in the journal"
grep -qF "$SECRET" /root/install-2.log && fail "the token is in the second install output"
ok "the token is not in the journal"

systemd-analyze security "$APP" --no-pager 2>/dev/null | tail -n 1 || true
echo "ALL CHECKS PASSED"
