# セットアップ

[READMEに戻る](../README.md)

## 必要なもの

- Node.js 24.11.1以上
- Discordアプリケーション（OAuthログイン用）

以下はWindows PowerShellでの手順です。Linux・macOSでは設定ファイルを `cp .env.example .env` で作成します。コマンドはリポジトリのルートで実行します。

## ソースと依存関係

```powershell
git clone https://github.com/syamo-game/meshi-archive.git
cd meshi-archive
npm ci
npm run build
Copy-Item .env.example .env
```

## Discord認証

`.env`に次の値を設定します。

| 設定 | 内容 |
| --- | --- |
| `SECRET_KEY` | 認証情報の照合に使う32文字以上のランダムな文字列 |
| `DISCORD_CLIENT_ID` | DiscordアプリケーションのクライアントID |
| `DISCORD_CLIENT_SECRET` | Discordアプリケーションのクライアントシークレット |
| `DISCORD_REDIRECT_URI` | 開発中は `http://localhost:5173/auth/discord/callback` |
| `ADMIN_USER_ID` | 最初の管理者のDiscordユーザーID |

Discord Developer Portalにも同じコールバックURLを登録します。秘密値をGitに追加せず、ローカルの`.env`で管理してください。

## DBと開発サーバー

ローカルでは既定でSQLiteを使います。マイグレーションを適用します。

```sh
npm run migrate
```

1つ目のターミナルでWebサーバーを起動します。

```sh
npm run dev:api
```

2つ目のターミナルでViteを起動します。

```sh
npm run dev:web
```

[http://localhost:5173](http://localhost:5173) を開き、管理者のDiscordアカウントでログインします。ほかのメンバーは「管理 → 利用ユーザー」で追加できます。匿名閲覧やサイト独自のパスワード認証はありません。

ViteはAPI・画像・Discord認証のリクエストをWebサーバーへ転送します。別のポートを使う場合は、Discord Developer Portalと`.env`のコールバックURLも合わせて変更します。

## ビルドした画面を確認する

```sh
npm run build
npm start
```

[http://localhost:8021](http://localhost:8021) を開きます。このURLでログインする場合は、コールバックURLを `http://localhost:8021/auth/discord/callback` に変更し、Webサーバーを再起動してください。開発サーバーが起動中なら、先に終了します。

## Botを起動する

Discord投稿を取り込む場合は、`.env`に`DISCORD_TOKEN`と`OPENAI_API_KEY`、対象の`DISCORD_GUILD_ID`と`DISCORD_CHANNEL_ID`を設定し、別のターミナルで起動します。

```sh
npm run bot
```

AIのモデル設定や認証の詳細は [システム構成](architecture.md) と [認証・環境変数](security.md) を参照してください。

## フォント

日本語フォントには [Noto Sans JP](https://fonts.google.com/noto/specimen/Noto+Sans+JP) を使用しています。[SIL Open Font License](../web/static/fonts/OFL-NotoSansJP.txt) に従って同梱しています。
