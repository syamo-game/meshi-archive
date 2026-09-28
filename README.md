# Meshi Archive

Discordの飲食店投稿を保存し、店舗候補を確認してから一覧へ載せるアプリケーションです。Python、FastAPI、Jinja2を使い、SQLiteまたはPostgreSQLに保存します。

## できること

- 投稿の本文・リンク・添付情報を保存し、出典付きの店舗候補を作成
- 店名・支店名・地域・カテゴリを管理画面で確認・修正
- 店舗検索、地域の絞り込み、訪問状態・評価・メモの編集
- 店舗写真のアップロード、表示、拡大
- 登録日時を保持したCSV入出力
- Discordの過去投稿の同期と、重複候補の確認

この配布物には店舗DB、投稿、店舗写真、認証情報、本番サーバーの設定を含めていません。空のDBから起動します。

## ローカル起動（Windows / PowerShell）

Python 3.11を使用します。テストにはNode.js 24も必要です。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip==26.2.1 setuptools==83.0.0
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

起動前に`.env`の`WEB_PASSWORD`、`ADMIN_PASSWORD`、`SECRET_KEY`をそれぞれ異なるランダム値で設定してください。次のコマンドを3回実行すると、設定に使える値を生成できます。

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

`.env`を保存してから起動します。

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn web.main:app --host 127.0.0.1 --port 8000
```

ブラウザで `http://127.0.0.1:8000/` を開きます。閲覧には`WEB_PASSWORD`、`/admin/`での編集には`ADMIN_PASSWORD`を使います。ローカルで閲覧認証を省く場合だけ、`APP_ENV=development`のまま`ALLOW_ANONYMOUS_READ=true`を指定します。パスワードを空にしただけでは閲覧できません。

Webだけの起動にDiscordやOpenAIのキーは不要です。投稿の自動処理には、別途Discord BotとOpenAI APIの設定が必要です。API利用には料金がかかります。

## Discord Bot

`.env`に`DISCORD_TOKEN`、`ADMIN_USER_ID`、`OPENAI_API_KEY`を設定し、Botを使うサーバー・チャンネルの権限を確認してください。`ADMIN_USER_ID`は操作を許可するDiscord利用者のIDです。抽出・解決モデルは`EXTRACTION_MODEL`と`RESOLUTION_MODEL`で指定し、利用するアカウントで使えるモデルを選びます。

```powershell
.\.venv\Scripts\python.exe -m bot.discord_bot
```

Discord OAuthを使う場合は、クライアントID・シークレット・コールバックURL・許可利用者IDも設定します。パスワードによるログインだけなら不要です。

## テストとCI

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

SQLiteとJavaScriptの回帰テストを実行します。PostgreSQL専用テストは、専用のテストDBを設定した場合に実行します。実データがあるDBは指定せず、破棄できる`test_*`または`*_test`という名前のDBを使ってください。

```powershell
$env:TEST_POSTGRES_URL = "postgresql+psycopg2://USER:PASSWORD@127.0.0.1:5432/meshi_archive_test"
$env:ALLOW_DESTRUCTIVE_POSTGRES_MIGRATION_TEST = "1"
.\.venv\Scripts\python.exe -m pytest -q
```

Dockerでもテストできます。

```powershell
docker build --target test -t meshi-archive:test .
docker run --rm meshi-archive:test python -m pytest -q
```

GitHub ActionsはPostgreSQL 16を使ったテスト、Dockerビルド、Gitleaksによる秘密情報検査、pip-auditによる依存関係検査を実行します。コンテナの発行やサーバーへのデプロイは行いません。

## 文書

- [システム構成](docs/architecture.md)
- [認証と公開時の設定](docs/security.md)
- [画面の仕様](docs/ui-framework.md)
- [開発ルール](docs/CONTRIBUTING.md)
- [地域データの出典と利用条件](web/data/README.md)
- [同梱Bootstrapのライセンス](web/static/vendor/bootstrap-5.3.8/LICENSE)

アプリ本体のライセンスは未設定です。
