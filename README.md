# printing-via-discord

Discord に PDF を送るだけで、サーバー PC に USB 接続したプリンターから印刷できる常駐 Bot です。

- 対象環境: Linux Mint（Ubuntu 系）/ CUPS / Canon TS6730（USB、片面・カラー対応）
- 実装: Python 3.10+ / discord.py / pypdf / CUPS (`lp`)

## 機能

| コマンド | 内容 |
|---|---|
| `/print file:<PDF> [copies] [pages] [color]` | PDF を印刷。1ページ目のプレビューと合計枚数を表示し、ボタンで確定してから印刷します |
| `/queue` | 印刷待ちジョブ一覧 |
| `/cancel job_id` | ジョブ取消（自分のジョブのみ。管理者は全ジョブ） |
| `/status` | プリンター状態・待ち件数・本日の自分の利用枚数 |

- 印刷完了時に、コマンドを実行したチャンネルでメンション通知します（送れない場合は DM）
- 90 秒以上印刷待ちのままだと、電源・用紙の確認を促す通知を出します
- Bot を再起動しても、印刷待ちジョブの監視を再開します

### 安全対策

- 設定した **1 つの Discord サーバーでのみ動作**します。他サーバーに招待されると自動で退出し、DM からは使えません
- 任意でロールによる制限ができます（`allowed_role_ids`）
- ファイルサイズ（既定 10MB）、ページ数（既定 30）、部数（既定 5）、1 人 1 日の枚数（既定 50）に上限があります。`/print` には 10 秒のクールダウンがあります
- PDF のマジックバイトと構造を検証し、壊れた PDF とパスワード付き PDF は拒否します
- 受け取った PDF は CUPS に渡した直後に削除します

## セットアップ

### 1. プリンターを CUPS に登録する

TS6730 を USB で接続し、次のコマンドで認識を確認します。

```bash
lsusb | grep -i canon
lpinfo -v
```

次のいずれかの方法で登録します（上から順に試してください）。

**A. ドライバなし（IPP-over-USB）**

```bash
sudo apt install ipp-usb
systemctl status ipp-usb
lpinfo -v | grep -i ipp       # ipp://localhost:60000/... が出れば対応
sudo lpadmin -p TS6730 -E -v "<上で出た ipp:// の URI>" -m everywhere
```

**B. Canon 純正ドライバ（cnijfilter2）**

Canon のサポートサイトから TS6730 用の「IJ Printer Driver for Linux」(.deb) をダウンロードしてインストールし、`sudo cnijlgmon3` を実行するか、Mint の「プリンター」設定画面から追加します。

**C. Gutenprint**

`sudo apt install printer-driver-gutenprint` をインストールし、設定画面から TS6730（または近い機種）を選びます。

登録できたら、手動で印刷できることを確認します。

```bash
lpstat -p -d
lp -d TS6730 /usr/share/cups/data/testprint
```

カラー/モノクロのオプション名を確認します。

```bash
lpoptions -p TS6730 -l
```

`ColorModel` や `CNIJGrayScale` などの項目が表示された場合は、その名前を `config.toml` の `color_options` / `mono_options` に書きます（例: `["ColorModel=Gray"]`）。ドライバなし（A）の場合は既定値の `print-color-mode=...` のままで動きます。

### 2. CUPS を外部に公開しない

`/etc/cups/cupsd.conf` を次のように設定します。

```
Listen localhost:631
Browsing No
# 印刷データを完了後に残さない
PreserveJobFiles No
```

設定したら次を実行します。

```bash
sudo systemctl restart cups
sudo systemctl disable --now cups-browsed   # 使わないなら停止（過去に脆弱性あり）
```

### 3. Discord Bot を作成する

1. [Developer Portal](https://discord.com/developers/applications) でアプリを作成し、Bot タブでトークンを取得します
2. Bot タブで **Public Bot を OFF** にします。特権 Intent は不要です
3. OAuth2 → URL Generator で scope に `bot` と `applications.commands`、権限に `Send Messages` と `Attach Files` を選び、生成された URL から自分のサーバーに招待します
4. Discord の開発者モードを ON にし、サーバー名を右クリックして「ID をコピー」で `guild_id` を取得します

### 4. インストールと常駐化

```bash
git clone https://github.com/Tatsubooom/printing-via-discord.git
cd printing-via-discord
sudo ./deploy/install.sh

sudo nano /etc/printbot/printbot.env   # DISCORD_TOKEN=...
sudo nano /etc/printbot/config.toml    # guild_id, printer.name など
sudo systemctl enable --now printbot
journalctl -u printbot -f
```

インストールすると、Bot は専用ユーザー `printbot` で動き、OS 起動時に自動起動し、異常終了しても 5 秒後に再起動します。

更新するときは、`git pull` のあとにもう一度 `sudo ./deploy/install.sh` と `sudo systemctl restart printbot` を実行します。

## 開発

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest

cp config.example.toml config.toml
cp .env.example .env
.venv/bin/python -m printbot    # カレントの .env と config.toml を読みます
```

## トラブルシューティング

| 症状 | 確認すること |
|---|---|
| コマンドが表示されない | `guild_id` が正しいか、招待時に `applications.commands` を付けたか |
| 「印刷待ちです」の通知が来る | プリンターの電源、用紙、インク。`lpstat -l -p TS6730` の出力 |
| プリンターが停止中になる | `sudo cupsenable TS6730` で再開。`journalctl -u cups` も確認 |
| モノクロにならない | `lpoptions -p TS6730 -l` を見て `mono_options` を直す |
| プレビューが出ない | `sudo apt install poppler-utils` |
