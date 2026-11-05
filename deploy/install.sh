#!/usr/bin/env bash
# Debian 系（antiX / Linux Mint / Ubuntu）用セットアップ。
# リポジトリのルートで sudo ./deploy/install.sh を実行する。
# init は自動判定する: systemd → printbot.service、runit（antiX など）→ /etc/sv/printbot
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "sudo で実行してください" >&2
  exit 1
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/printbot
ETC=/etc/printbot

if [[ -d /run/systemd/system ]]; then
  INIT=systemd
elif command -v sv >/dev/null && [[ -d /etc/sv ]]; then
  INIT=runit
else
  echo "systemd / runit のどちらも見つかりません。手動で常駐化してください" >&2
  INIT=none
fi
echo "init: $INIT"

apt-get install -y python3-venv cups cups-client poppler-utils ipp-usb
# PDF 以外（Office 文書・テキスト）の変換用。GUI 版の LibreOffice が既にあればそれを使う
if ! command -v soffice >/dev/null; then
  apt-get install -y libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui
fi
apt-get install -y fonts-ipafont   # 日本語の文書を変換するためのフォント

id printbot &>/dev/null || useradd --system --home-dir /var/lib/printbot --shell /usr/sbin/nologin printbot
install -d -m 0700 -o printbot -g printbot /var/lib/printbot

mkdir -p "$APP"
cp -r "$SRC/printbot" "$SRC/pyproject.toml" "$APP/"
[[ -x "$APP/.venv/bin/python" ]] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --upgrade pip
"$APP/.venv/bin/pip" install --upgrade "$APP"

install -d -m 0750 -o root -g printbot "$ETC"
[[ -f "$ETC/config.toml" ]] || install -m 0640 -o root -g printbot "$SRC/config.example.toml" "$ETC/config.toml"
[[ -f "$ETC/printbot.env" ]] || install -m 0640 -o root -g printbot "$SRC/.env.example" "$ETC/printbot.env"

# runit のサービスディレクトリを /etc/sv/<name> に置く（ログサービス付き）
install_runit_service() {
  local name=$1
  install -d -m 0755 "/etc/sv/$name" "/etc/sv/$name/log"
  install -m 0755 "$SRC/deploy/runit/$name/"* "/etc/sv/$name/"
  if [[ -x /etc/sv/svlogd/run ]]; then
    ln -sfn /etc/sv/svlogd/run "/etc/sv/$name/log/run"   # antiX 標準のログ設定
  else
    install -m 0755 "$SRC/deploy/runit/svlogd-run" "/etc/sv/$name/log/run"
  fi
}

case $INIT in
  systemd)
    install -m 0644 "$SRC/deploy/printbot.service" /etc/systemd/system/printbot.service
    systemctl daemon-reload
    START="sudo systemctl enable --now printbot"
    RESTART="sudo systemctl restart printbot"
    LOGS="journalctl -u printbot -f"
    ;;
  runit)
    install_runit_service printbot
    # ipp-usb は systemd 前提の udev ルールでしか起動しないので、常駐サービスとして有効化する
    install_runit_service ipp-usb
    [[ -e /etc/service/ipp-usb ]] || ln -s /etc/sv/ipp-usb /etc/service/ipp-usb
    START="sudo ln -s /etc/sv/printbot /etc/service/printbot"
    RESTART="sudo sv restart printbot"
    LOGS="sudo tail -f /var/log/runit/printbot/current"
    ;;
  none)
    START="sudo -u printbot env PRINTBOT_CONFIG=$ETC/config.toml $APP/.venv/bin/python -m printbot"
    RESTART="(手動で再起動)"
    LOGS="(標準エラー出力)"
    ;;
esac

echo
echo "インストール完了。次の手順:"
echo "  1. sudo nano $ETC/printbot.env   (DISCORD_TOKEN を設定)"
echo "  2. sudo nano $ETC/config.toml    (guild_id とプリンター名を設定)"
echo "  3. $START"
echo "  4. $LOGS     (ログ確認)"
echo "更新時: git pull → sudo ./deploy/install.sh → $RESTART"
