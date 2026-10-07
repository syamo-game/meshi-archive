# Meshi Archive

Discordで見つけた飲食店を、検索できる一覧にまとめるアプリです。店名や地域を確認・修正し、写真、訪問状況、評価、メモを保存できます。閲覧と管理のログインはDiscord OAuthに統一しています。

## 手元で起動する

Python 3.11以降を用意し、PowerShellで次を実行します。

```powershell
git clone https://github.com/syamo-game/meshi-archive.git
cd meshi-archive
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip==26.2.1 setuptools==83.0.0
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env`に`SECRET_KEY`、`DISCORD_CLIENT_ID`、`DISCORD_CLIENT_SECRET`、`DISCORD_REDIRECT_URI`、管理者の`ADMIN_USER_ID`を設定します。ローカルのcallbackは`http://127.0.0.1:8000/auth/discord/callback`です。Discord側にも同じcallbackを登録してください。`SECRET_KEY`は次のコマンドで作れます。

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

DBを作成して起動します。

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn web.main:app --host 127.0.0.1 --port 8000
```

[店舗一覧](http://127.0.0.1:8000/)と[管理画面](http://127.0.0.1:8000/admin/)でDiscordにログインします。初回は店舗0件です。管理画面では利用ユーザーの追加・編集・利用許可の取り消しを行えます。店舗情報は編集用CSVをダウンロードし、変更内容を確認してから保存します。旧`WEB_PASSWORD`と`ADMIN_PASSWORD`は使用しません。

## Discordから登録する

`.env`に`DISCORD_TOKEN`、`OPENAI_API_KEY`、`ADMIN_USER_ID`を設定します。`EXTRACTION_MODEL`と`RESOLUTION_MODEL`は利用できるモデル名に合わせてください。OpenAI APIの利用には料金がかかります。

Discord側ではBotの「Message Content Intent」を有効にし、使うチャンネルの閲覧・履歴の閲覧・送信・リアクションを許可します。別のPowerShellで同じフォルダから起動します。

```powershell
.\.venv\Scripts\python.exe -m bot.discord_bot
```

`@Bot 店舗のURLや説明`で登録し、`@Bot sync`で過去投稿を取り込みます。特定できなかった投稿は管理画面の「登録内容の確認」で修正します。AIによる再読み込みは空欄だけを補い、写真はチェックした1枚を登録します。

## 詳しい説明

[開発・テスト](docs/CONTRIBUTING.md) · [外部公開時の設定](docs/security.md) · [内部の仕組み](docs/architecture.md) · [画面の仕様](docs/ui-framework.md) · [利用ユーザーの管理](docs/discord-user-management.md)

本番用のCompose、接続先、デプロイ処理は同梱していません。店舗データ・写真・認証情報も含みません。アプリ本体は[MITライセンス](LICENSE)です。[地域データの出典・利用条件](web/data/README.md)と[Bootstrapのライセンス](web/static/vendor/bootstrap-5.3.8/LICENSE)は各ファイルをご覧ください。
