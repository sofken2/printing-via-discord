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

- 印刷を確定すると、確認メッセージがジョブの**状態パネル**に変わり、自動で更新されます（最大約 14 分）

  | 表示 | 意味 |
  |---|---|
  | 🕒 印刷待ち | CUPS のキューで順番待ち |
  | 📨 データ送信中・印刷中 | プリンターへデータを送信中 |
  | ⏸️ 保留中 / ⛔ 停止中 | 保留中、またはプリンターのエラーで停止中 |
  | ✅ 印刷完了 | プリンターへの送信が正常に完了 |
  | ❌ 印刷失敗 | エラーで中止（理由も表示） |
  | 🗑️ 取り消し | 取り消し済み |

- 用紙切れ・インク切れ・カバー開き・オフラインなど、プリンターが報告した問題も表示します
- **印刷の成功・失敗は DM で通知**します。失敗した場合は理由も送ります。90 秒以上印刷が進まない場合も DM で知らせます
- Bot を再起動しても、印刷待ちジョブの監視を再開します
- `/status` では、プリンターの状態、エラー、インク残量（ドライバが報告する場合）を表示します

> 「印刷完了」は、CUPS がプリンターへのデータ送信を終えた時点を指します。紙が実際に出てきたかまでは検知できません。プリンター本体のエラー（紙詰まりなど）は、ドライバが報告したものだけ表示されます。

### ファイルのプライバシー

- `/print` のやり取り（確認画面、プレビュー、状態パネル）はすべて**本人にだけ見える**メッセージで行います。添付した PDF がチャンネルの他のメンバーに見えることはありません
- 完了通知は DM で送ります。DM を受け取れない設定の場合だけチャンネルでメンションしますが、その際**ファイル名は出さず**、ジョブ ID だけを表示します
- `/queue` では、他のメンバーのジョブはファイル名を伏せて表示します
- サーバー PC 上の PDF は CUPS に渡した直後に削除します。CUPS のスプールにも残さないよう、下記の `PreserveJobFiles No` を設定してください
- 注意: スラッシュコマンドで添付したファイルは Discord の CDN に一時的に保存されます。URL を知っている人は有効期限内にアクセスできますが、URL はチャンネルに表示されません

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
# 印刷データを完了後に残さない（ジョブの状態履歴は残るので、成功/失敗の判定には影響しない）
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
| 「まだ印刷されていません」の通知が来る | プリンターの電源、用紙、インク。`lpstat -l -p TS6730` の出力 |
| 完了通知が DM で届かない | Discord の「サーバーにいるメンバーからのダイレクトメッセージを許可する」が ON になっているか |
| 状態が「取得できませんでした」になる | CUPS が動いているか（`systemctl status cups`）。`config.toml` の `cups_server` |
| プリンターが停止中になる | `sudo cupsenable TS6730` で再開。`journalctl -u cups` も確認 |
| モノクロにならない | `lpoptions -p TS6730 -l` を見て `mono_options` を直す |
| プレビューが出ない | `sudo apt install poppler-utils` |
