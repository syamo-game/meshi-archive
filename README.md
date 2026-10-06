# Meshi Archive

Discordで見つけた飲食店を、検索できる一覧にまとめるアプリです。店名や地域を確認・修正し、写真、訪問状況、評価、メモを保存できます。CSVの読み込み・書き出しにも対応しています。

## 手元で起動する

Python 3.11を用意し、PowerShellで次を実行します。Web画面だけならDiscordやOpenAIのAPIキーは不要です。

```powershell
git clone https://github.com/syamo-game/meshi-archive.git
cd meshi-archive
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip==26.2.1 setuptools==83.0.0
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env`の`WEB_PASSWORD`（閲覧用）、`ADMIN_PASSWORD`（編集用）、`SECRET_KEY`（ログイン情報の保護用）に、別々の値を設定します。次のコマンドを3回実行すると、それぞれの値を作れます。

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

`.env`を保存して起動します。DBは初回起動時に作成されます。

```powershell
.\.venv\Scripts\python.exe -m uvicorn web.main:app --host 127.0.0.1 --port 8000
```

[店舗一覧](http://127.0.0.1:8000/)は`WEB_PASSWORD`、[管理画面](http://127.0.0.1:8000/admin/)は`ADMIN_PASSWORD`でログインします。初回は店舗0件です。管理画面からCSVを読み込むか、次のBotで投稿を登録します。

## Discordから登録する

`.env`に`DISCORD_TOKEN`、`OPENAI_API_KEY`、`ADMIN_USER_ID`（操作する自分のDiscord ID）を設定します。`EXTRACTION_MODEL`と`RESOLUTION_MODEL`も、利用できるモデル名に合わせてください。OpenAI APIの利用には料金がかかります。

Discord側ではBotの「Message Content Intent」を有効にし、使うチャンネルの閲覧・履歴の閲覧・送信・リアクションを許可します。別のPowerShellで、同じフォルダから起動します。

```powershell
.\.venv\Scripts\python.exe -m bot.discord_bot
```

`@Bot 店舗のURLや説明`で登録し、`@Bot sync`でそのチャンネルの過去投稿を取り込みます。店や地域を特定できなかった投稿は、管理画面の「データ確認」で修正します。

## 詳しい説明

[開発・テスト](docs/CONTRIBUTING.md) · [外部公開時の設定](docs/security.md) · [内部の仕組み](docs/architecture.md) · [画面の仕様](docs/ui-framework.md)

店舗データ・写真・認証情報は同梱していません。アプリ本体は[MITライセンス](LICENSE)です。[地域データの出典・利用条件](web/data/README.md)と[Bootstrapのライセンス](web/static/vendor/bootstrap-5.3.8/LICENSE)は各ファイルをご覧ください。
