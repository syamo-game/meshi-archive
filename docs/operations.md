# 運用手順

## 起動とデプロイ

Node.js 24.11.1以上とPostgreSQL 16を使用します。本番Composeは`db`、`migrate`、`web`、`bot`の4サービスです。WebとBotは同じNode.jsイメージを使い、写真用の名前付きボリュームを共有します。

GitHub ActionsはPRでテストと依存関係・秘密情報の検査を行います。mainへのpushで`ghcr.io/syamo-game/meshi-archive:sha-<commit>`を発行し、本番反映は`Deploy`の手動実行で`deploy=true`を指定した場合に限ります。

リポジトリのSecretsには`VPS_HOST`、`VPS_USER`、`VPS_SSH_KEY`、`VPS_SSH_KNOWN_HOSTS`を設定します。`production` Environmentの承認条件とmainの保護ルールは運用に合わせて設定してください。

ワークフローは本番の排他ロックを取得し、DB接続先・写真ボリューム・空き容量を検証します。WebとBotを停止してDB・写真・環境変数・Compose・使用中イメージを保存し、チェックサムを確認してから新しいComposeと固定SHAイメージへ切り替えます。旧データに対応するバックアップは削除しません。

初回はリバースプロキシと外部ネットワーク`proxy`、本番の`.env`を用意します。Webの接続先は`meshi-web:8000`です。DBを公開ポートへ直接接続しません。

## 確認

```sh
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail 100 web bot migrate
docker compose -f docker-compose.prod.yml exec -T web node -e 'fetch("http://localhost:8000/healthz").then(r=>{if(!r.ok)process.exit(1)})'
```

Discordログイン、一覧と詳細、評価・メモ、管理画面、写真、Botの接続を確認します。ヘルスチェックだけでは外部OAuthやAIの動作までは確認できません。

## 運用コマンド

以下はリポジトリのルートで、ビルド後に実行します。本番コンテナでは`npm run ops --`を`node server/dist/operations/cli.js`に置き換えてください。

```sh
npm run ops -- report
npm run ops -- duplicates
npm run ops -- duplicates --apply
npm run ops -- cache-images --limit 100 --apply
npm run ops -- restore-sources --message-id 123456789012345678 --apply
npm run ops -- evaluate --limit 10 --concurrency 2
```

重複統合・画像キャッシュ・出典復元は`--apply`を付けた場合だけ保存します。出典復元は保存しない場合もDiscordへ問い合わせます。再評価はAIを呼び出してレビュー差分を保存します。人手で確定した言及や、処理中に編集された内容は上書きしません。

画像キャッシュを作り直す場合は`--force`を付けます。出典復元で投稿のチャンネルが未保存なら`--channel-id`を指定します。件数指定の`--limit`と投稿指定の`--message-id`は併用できません。

## Discord履歴同期

`npm run sync`は設定済みチャンネルの履歴を1回取得します。AIの呼び出しとDB更新を行います。毎週の自動同期は`WEEKLY_SYNC_ENABLED=true`で有効になり、月曜04:00（Asia/Tokyo）に実行します。

失敗した投稿より先へ進捗を進めず、再実行時にその投稿から再開します。同じチャンネルの同期はDBロックで重複実行を防ぎます。

## SQLiteからPostgreSQLへの移送

移送先は初期化済みで、アプリのデータが空のDBを用意します。`SOURCE_DATABASE_URL`、`TARGET_DATABASE_URL`、移送先と完全一致する`CONFIRM_TARGET_DATABASE_URL`を設定します。

```sh
npm run ops -- transfer --apply
npm run ops -- transfer --verify-only
```

全テーブルの件数と内容のハッシュを照合します。移送先にデータがある場合や照合に失敗した場合は中止し、書き込みを取り消します。日時はタイムゾーンと小数秒を維持します。

## バックアップと復旧

DBだけでなく、写真ボリューム、`.env`、Compose、実行イメージのSHAを保存します。`pg_dump -Fc`の出力を`pg_restore -l`で読み、写真アーカイブの一覧とチェックサムも確認します。書き込みを止めた状態で取得し、バックアップ先の権限を制限してください。

復旧時もWebとBotを停止します。バックアップのチェックサムを確認し、DBと写真を復元して対応する固定SHAイメージで起動します。DBのrevision、件数、ログイン、写真、Botの接続を確認してから通常運用へ戻します。失敗中の移行を隠すために監査データや復旧マーカーを削除しません。

`APP_READ_ONLY=true`ではWebの更新を拒否し、Botは起動しません。復旧・データ補正の途中では読取専用設定だけに頼らず、WebとBotを停止してください。
