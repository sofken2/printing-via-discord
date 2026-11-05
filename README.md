# printing-via-discord

Discord にファイル（PDF・画像・Office 文書など）を送るだけで、サーバー PC に USB 接続したプリンターから印刷できる常駐 Bot です。

- 対象環境: Debian 系 Linux / CUPS / Canon TS6730（USB、片面・カラー対応）
  - 稼働中のサーバー: **antiX 26.1（Debian 13 trixie ベース、init は runit）**
  - systemd 環境（Linux Mint / Ubuntu など）でも動きます。`install.sh` が init を自動判定します
- 実装: Python 3.10+ / discord.py / pypdf / Pillow / LibreOffice（変換用）/ CUPS (`lp`)

## 機能

### 対応ファイル

| 種類 | 拡張子 | 変換方法 |
|---|---|---|
| PDF | `.pdf` | そのまま印刷 |
| 画像 | `.jpg` `.jpeg` `.png` `.gif` `.bmp` `.webp` `.tif` `.tiff` | Pillow で PDF に変換（1 画像 1 ページ、用紙に合わせて拡大縮小）。TIFF は全ページ、GIF は 1 コマ目 |
| Office 文書 | `.docx` `.doc` `.xlsx` `.xls` `.pptx` `.ppt` `.odt` `.ods` `.odp` `.rtf` | LibreOffice で PDF に変換 |
| テキスト | `.txt` `.md` `.csv` | UTF-8 / Shift_JIS / EUC-JP を自動判別して LibreOffice で PDF に変換 |

- ページ数・ページ範囲・上限枚数は、変換後の PDF で数えます
- 変換結果はプレビューで確認できます。Office 文書はフォントの違いでレイアウトが少し崩れることがあります。正確に印刷したい場合は PDF で送ってください
- iPhone の HEIC 画像には対応していません（Discord に送ると通常は JPEG になります）


| コマンド | 内容 |
|---|---|
| `/print file:<ファイル> [copies] [pages] [color]` | ファイルを印刷。PDF 以外は PDF に変換してから、1ページ目のプレビューと合計枚数を表示し、ボタンで確定してから印刷します |
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
- `/status` では、プリンターの状態、エラー（ある場合のみ）、印刷待ち件数、本日の自分の利用枚数を表示します

> 「印刷完了」は、CUPS がプリンターへのデータ送信を終えた時点を指します。紙が実際に出てきたかまでは検知できません。プリンター本体のエラー（紙詰まりなど）は、ドライバが報告したものだけ表示されます。

### ファイルのプライバシー

- `/print` のやり取り（確認画面、プレビュー、状態パネル）はすべて**本人にだけ見える**メッセージで行います。添付したファイルがチャンネルの他のメンバーに見えることはありません
- 完了通知は DM で送ります。DM を受け取れない設定の場合だけチャンネルでメンションしますが、その際**ファイル名は出さず**、ジョブ ID だけを表示します
- `/queue` では、他のメンバーのジョブはファイル名を伏せて表示します
- サーバー PC 上のファイル（元のファイル・変換後の PDF・プレビュー）は CUPS に渡した直後に削除します。CUPS のスプールにも残さないよう、下記の `PreserveJobFiles No` を設定してください
- 注意: スラッシュコマンドで添付したファイルは Discord の CDN に一時的に保存されます。URL を知っている人は有効期限内にアクセスできますが、URL はチャンネルに表示されません

### 安全対策

- 設定した **1 つの Discord サーバーでのみ動作**します。他サーバーに招待されると自動で退出し、DM からは使えません
- 任意でロールによる制限ができます（`allowed_role_ids`）
- ファイルサイズ（既定 10MB）、ページ数（既定 30）、部数（既定 5）、1 人 1 日の枚数（既定 50）に上限があります。`/print` には 10 秒のクールダウンがあります
- PDF のマジックバイトと構造を検証し、壊れた PDF とパスワード付き PDF は拒否します
- 対応していない拡張子は拒否します。Office 文書は中身が拡張子どおりの形式か（マジックバイト）を確認します
- 画像は解像度（6000 万画素）と TIFF のページ数（50）に上限があります
- LibreOffice の変換は 1 件ずつ、90 秒で打ち切ります。マクロは実行されません（LibreOffice の既定のマクロセキュリティ）
- 注意: Office 文書に外部の画像などへのリンクが含まれていると、変換時に LibreOffice がそれを取りに行くことがあります。信頼できるメンバーだけが使うサーバーで運用してください
- 受け取ったファイルは CUPS に渡した直後に削除します

## セットアップ

### 1. プリンターを CUPS に登録する

TS6730 を USB で接続し、次のコマンドで認識を確認します。

```bash
lsusb | grep -i canon
lpinfo -v
```

次のいずれかの方法で登録します（上から順に試してください）。

**A. ドライバなし（IPP-over-USB）— 推奨。TS6730 は対応済み（USB インターフェース 7/1/4）**

```bash
sudo apt install ipp-usb
sudo ipp-usb check            # "Canon TS6730 series" が出れば対応
```

ipp-usb は udev ルールから **systemd 経由で**起動される作りのため、runit の antiX では自動では起動しません。
`sudo ./deploy/install.sh`（手順 4）を実行すると runit サービス `/etc/sv/ipp-usb` が登録・有効化されます。先に手動で試す場合は次のとおりです。

```bash
sudo sv status ipp-usb        # runit（antiX）: run: ipp-usb ... なら起動中
systemctl status ipp-usb      # systemd 環境の場合
ipptool -tv ipp://localhost:60000/ipp/print get-printer-attributes.test | head   # 応答があれば OK
sudo lpadmin -p TS6730 -E -v ipp://localhost:60000/ipp/print -m everywhere
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
# runit（antiX）
sudo sv restart cups
# systemd 環境
sudo systemctl restart cups
sudo systemctl disable --now cups-browsed   # 使わないなら停止（過去に脆弱性あり）
```

> antiX の既定の `cupsd.conf` は `Browsing Yes` になっているので `No` に変更してください。cups-browsed は antiX 26.1 では未インストールです。

### 3. Discord Bot を作成する

1. [Developer Portal](https://discord.com/developers/applications) でアプリを作成し、Bot タブでトークンを取得します
2. Bot タブで **Public Bot を OFF** にします。特権 Intent は不要です
3. OAuth2 → URL Generator で scope に `bot` と `applications.commands`、権限に `Send Messages` と `Attach Files` を選び、生成された URL から自分のサーバーに招待します
4. Discord の開発者モードを ON にし、サーバー名を右クリックして「ID をコピー」で `guild_id` を取得します

### 4. インストールと常駐化

```bash
git clone https://github.com/sofken2/printing-via-discord.git
cd printing-via-discord
sudo ./deploy/install.sh

sudo nano /etc/printbot/printbot.env   # DISCORD_TOKEN=...
sudo nano /etc/printbot/config.toml    # guild_id, printer.name など
```

起動とログ確認は init によって異なります（`install.sh` の最後にも表示されます）。

| | runit（antiX） | systemd（Mint / Ubuntu） |
|---|---|---|
| 有効化・起動 | `sudo ln -s /etc/sv/printbot /etc/service/printbot` | `sudo systemctl enable --now printbot` |
| 状態 | `sudo sv status printbot` | `systemctl status printbot` |
| 再起動 | `sudo sv restart printbot` | `sudo systemctl restart printbot` |
| 停止（無効化） | `sudo rm /etc/service/printbot` | `sudo systemctl disable --now printbot` |
| ログ | `sudo tail -f /var/log/runit/printbot/current` | `journalctl -u printbot -f` |

インストールすると、Bot は専用ユーザー `printbot` で動き、OS 起動時に自動起動し、異常終了しても 5 秒後に再起動します。
runit 環境では ipp-usb もサービス化され（`/etc/sv/ipp-usb`）、自動で起動します。

更新するときは、`git pull` のあとにもう一度 `sudo ./deploy/install.sh` を実行し、上の表の「再起動」を実行します。

## 開発

```bash
sudo apt install python3-venv   # antiX では未インストールのことがある
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
| `/opt/printbot/.venv` が作れない（`ensurepip` がない） | `sudo apt install python3-venv` |
| 完了通知が DM で届かない | Discord の「サーバーにいるメンバーからのダイレクトメッセージを許可する」が ON になっているか |
| 状態が「取得できませんでした」になる | CUPS が動いているか（`sudo sv status cups` / `systemctl status cups`）。`config.toml` の `cups_server` |
| プリンターが見つからない（`ipp://localhost:60000` に繋がらない） | ipp-usb が動いているか（`sudo sv status ipp-usb`）。ログは `/var/log/ipp-usb/` |
| プリンターが停止中になる | `sudo cupsenable TS6730` で再開。`/var/log/cups/error_log`（systemd なら `journalctl -u cups`）も確認 |
| モノクロにならない | `lpoptions -p TS6730 -l` を見て `mono_options` を直す |
| プレビューが出ない | `sudo apt install poppler-utils` |
| Office 文書で「LibreOffice がない」と出る | `sudo apt install libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui` |
| 変換した文書の日本語が文字化け・豆腐になる | `sudo apt install fonts-ipafont`（または `fonts-noto-cjk`） |
