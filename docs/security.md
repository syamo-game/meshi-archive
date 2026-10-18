# 認証と環境変数

## Webの認証

Discord OAuthで本人を確認し、設定またはDBに登録された利用許可を照合します。匿名閲覧はできません。

| 設定 | 内容 |
| --- | --- |
| `SECRET_KEY` | 32文字以上のランダム値。認証情報の照合に使用 |
| `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` | Discord OAuthの資格情報 |
| `DISCORD_REDIRECT_URI` | `/auth/discord/callback`で終わるURL。本番はHTTPS |
| `ADMIN_USER_ID` | WebとBotの管理者 |
| `WEB_ADMIN_USER_IDS` | 追加のWeb管理者。カンマ区切り |
| `DISCORD_ALLOWED_USER_IDS` | 設定で許可するメンバー。カンマ区切り |
| `APP_ENV` | `development`または`production` |
| `HTTPS_ONLY` | 本番は`true` |
| `FORWARDED_ALLOW_IPS` | 信頼するリバースプロキシのIPまたは限定したCIDR |
| `APP_READ_ONLY` | `true`で更新を停止 |

一般メンバーは公開中の店舗の共有評価・メモを編集できます。店舗情報・写真・CSV・レビュー・利用許可の変更は管理者に限ります。Botの操作権限は`ADMIN_USER_ID`だけで、Webの権限追加では変わりません。

セッションはランダムなトークンをHttpOnly Cookieへ保存し、DBにはそのハッシュを保存します。管理者・一般メンバーとも発行から24時間で失効します。ログアウトや利用許可の変更でも失効し、再登録・再昇格で古いCookieが復活することはありません。OAuthのstateは10分間有効で、一度だけ使用できます。

更新リクエストはCSRFトークンとレコードのバージョンを検証します。同時編集は409で返し、後から来た変更で上書きしません。利用許可の変更と監査記録は同じトランザクションで保存します。

## BotとAI

Botには`DISCORD_TOKEN`、`ADMIN_USER_ID`、`OPENAI_API_KEY`が必要です。履歴同期には`DISCORD_CHANNEL_ID`を設定します。

| 設定 | 既定値 |
| --- | --- |
| `EXTRACTION_MODEL` | `gpt-6.1-sol` |
| `RESOLUTION_MODEL` | `gpt-6.1-sol` |
| `EXTRACTION_REASONING_EFFORT` | `high` |
| `RESOLUTION_REASONING_EFFORT` | `high` |
| `AI_MAX_CONCURRENCY` | `2` |
| `LOOKUP_CACHE_DAYS` | `30` |
| `SYNC_BATCH_LIMIT` | `500` |
| `WEEKLY_SYNC_ENABLED` | `false` |

起動時にモデルの利用可否を確認します。利用できないモデルを別モデルへ自動で置き換えません。利用料金の集計は設定された単価に基づく概算です。

## 配信と保存

本番ではHTTPSのリバースプロキシを経由し、DBは内部ネットワークだけに接続します。転送ヘッダーは`FORWARDED_ALLOW_IPS`に一致する接続元からだけ受け付けます。ワイルドカードの代わりに実際のプロキシIPを指定してください。

写真は`SHOP_IMAGE_CACHE_DIR`へ保存します。DBと写真はセットでバックアップします。外部ページ・画像は公開IPと許可された取得条件を検証し、内部ネットワークへのアクセスや上限を超えるデータを拒否します。

`.env`、DB、写真、バックアップ、Cookieをリポジトリへ追加せず、権限を制限した保存先で管理します。依存関係は`package-lock.json`で固定し、`npm ci`でインストールします。CIでテスト、`npm audit --omit=dev`、Git履歴のGitleaks検査を実行します。OSとNode.js自体の更新は別途必要です。

## DBの更新

空のDBは`npm run migrate`で初期化します。既存DBはrevision `20261007_01`を対象とし、店舗・投稿・監査を保持したままTS用セッション表を追加します。対応していないrevisionや不完全なスキーマでは処理を停止します。

DB変更前にはWebとBotを止め、復元可能なバックアップを取得します。切り戻しではイメージ・DB・写真・環境設定の組み合わせをそろえます。旧Cookieは移行後に引き継がず、Discordでログインし直します。
