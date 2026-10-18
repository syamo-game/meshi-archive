# Meshi Archive

Discordで共有された飲食店を、写真・評価・メモと一緒に保存するWebアプリです。

## できること

- 店名・エリア・カテゴリから店舗を検索
- 店舗ごとの5つ星評価とメモを共有
- Mapや元投稿から、場所と出典を確認
- 管理画面で店舗・写真・利用ユーザーを編集し、CSVでまとめて更新

## 使い方

1. Discordでログインします。利用できるアカウントは管理者が登録します。
2. 一覧から店舗を探し、カードを押して詳細を開きます。
3. 星を選んで評価し、「メモ」の編集からコメントを残します。

評価とメモは店舗ごとに共有され、ログインしたメンバーが編集できます。星が付いている店舗を訪問済みとして扱います。

Discordの投稿を取り込むBotと、店舗候補を確認・承認する管理画面も備えています。

## ローカルで動かす

Node.js 24.11.1以上とDiscordアプリケーションが必要です。
初回は [セットアップ](docs/setup.md) に沿って、依存関係・Discord認証・DBを準備してください。

準備後、2つのターミナルでWebサーバーとフロントエンドを起動します。

```sh
npm run dev:api
```

```sh
npm run dev:web
```

[http://localhost:5173](http://localhost:5173) を開きます。開発中のDiscord認証のコールバックURLも、このポートに合わせて設定します。

## 開発

画面はReact、APIはHono、Botはdiscord.jsを使い、すべてTypeScriptで実装しています。ローカルではSQLite、本番ではPostgreSQLを使います。

型チェックとテスト:

```sh
npm run typecheck
npm test
```

本番用の画面をビルド:

```sh
npm run build
npm run migrate
npm start
```

ビルドした画面はNode.jsのWebサーバーから配信します。Botは別のターミナルで`npm run bot`を実行します。

## ドキュメント

- [セットアップ](docs/setup.md)
- [システム構成](docs/architecture.md)
- [認証・環境変数](docs/security.md)
- [運用・デプロイ](docs/operations.md)
- [開発ルール](docs/CONTRIBUTING.md)
