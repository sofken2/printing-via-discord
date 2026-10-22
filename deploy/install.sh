#!/usr/bin/env bash
# Linux Mint / Ubuntu 用セットアップ。リポジトリのルートで sudo ./deploy/install.sh を実行する。
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "sudo で実行してください" >&2
  exit 1
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/printbot
ETC=/etc/printbot

apt-get install -y python3-venv cups cups-client poppler-utils

id printbot &>/dev/null || useradd --system --home-dir /var/lib/printbot --shell /usr/sbin/nologin printbot

mkdir -p "$APP"
cp -r "$SRC/printbot" "$SRC/pyproject.toml" "$APP/"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --upgrade pip
"$APP/.venv/bin/pip" install "$APP"

install -d -m 0750 -o root -g printbot "$ETC"
[[ -f "$ETC/config.toml" ]] || install -m 0640 -o root -g printbot "$SRC/config.example.toml" "$ETC/config.toml"
[[ -f "$ETC/printbot.env" ]] || install -m 0640 -o root -g printbot "$SRC/.env.example" "$ETC/printbot.env"

install -m 0644 "$SRC/deploy/printbot.service" /etc/systemd/system/printbot.service
systemctl daemon-reload

echo
echo "インストール完了。次の手順:"
echo "  1. sudo nano $ETC/printbot.env   (DISCORD_TOKEN を設定)"
echo "  2. sudo nano $ETC/config.toml    (guild_id とプリンター名を設定)"
echo "  3. sudo systemctl enable --now printbot"
echo "  4. journalctl -u printbot -f     (ログ確認)"
