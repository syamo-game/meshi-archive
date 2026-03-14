# Meshi Archive - システム仕様書

**作成日:** 2026-03-14
**バージョン:** 1.0
**対象リポジトリ:** meshi-archive

---

## 1. システム概要

**Meshi Archive** は、Discordチャンネルに投稿された飲食店情報を自動収集・管理するシステムです。
Discordボットがメッセージを監視し、OpenAI GPT-4o-mini によるAI解析で店舗情報を抽出、PostgreSQLデータベースに保存します。
Streamlit製のWebダッシュボードで一覧閲覧・フィルタリング・CSV入出力が可能です。

---

## 2. システム構成

```
Discord
  └─ on_mention → Discord Bot (Python)
                     └─ OpenAI GPT-4o-mini (抽出)
                     └─ PostgreSQL / SQLite (保存)
                               └─ Streamlit Web UI (閲覧・編集)
```

### デプロイ構成（Render.com）

| サービス | 種別 | 説明 |
|---|---|---|
| meshi-archive-web | Web Service | Streamlit ダッシュボード |
| meshi-archive-bot | Worker Service | Discord ボット |
| meshi-archive-db | PostgreSQL | 本番データベース |

---

## 3. 技術スタック

| 分類 | 技術 | バージョン |
|---|---|---|
| 言語 | Python | 3.11.9 |
| Discord ライブラリ | discord.py | >=2.3.2 |
| AI / LLM | OpenAI API (GPT-4o-mini) | >=1.14.0 |
| ORM | SQLAlchemy | >=2.0.30 |
| Web UI | Streamlit | >=1.36.0 |
| データ処理 | Pandas | >=2.2.2 |
| DB (本番) | PostgreSQL | - |
| DB (開発) | SQLite | (フォールバック) |
| DB アダプタ | psycopg2-binary | 2.9.9 |
| 環境変数 | python-dotenv | >=1.0.1 |

---

## 4. ディレクトリ構成

```
meshi-archive/
├── bot/
│   ├── discord_bot.py          # Discordボット メインエントリ
│   ├── restaurant_extractor.py # OpenAI による店舗情報抽出
│   └── sync_logic.py           # 過去メッセージ一括同期ロジック
├── db/
│   ├── __init__.py
│   ├── database.py             # SQLAlchemy エンジン・セッション管理
│   └── models.py               # ORM モデル定義
├── web/
│   └── streamlit_app.py        # Streamlit ダッシュボード
├── docs/
│   └── specification.md        # 本仕様書
├── render.yaml                 # Render.com デプロイ設定
├── requirements.txt            # Python 依存ライブラリ
└── .python-version             # Python バージョン固定 (3.11.9)
```

---

## 5. データベース設計

### 5-1. messages テーブル

Discordメッセージの処理履歴を管理します。

| カラム名 | 型 | 制約 | 説明 |
|---|---|---|---|
| message_id | String | PK | Discord メッセージ ID |
| processed_at | DateTime | default: utcnow | 処理日時 |
| is_target | Boolean | default: True | 飲食店情報として対象か否か |

### 5-2. shops テーブル

抽出された飲食店情報を保存します。

| カラム名 | 型 | 制約 | 説明 |
|---|---|---|---|
| id | Integer | PK, autoincrement | 内部ID |
| message_id | String | FK(messages) | 元メッセージID |
| shop_name | String | NOT NULL | 店名（正式名称に補正済み） |
| area | String | nullable | エリア（駅名・市区町村名） |
| category | String | nullable | カテゴリ（寿司・居酒屋 等） |
| url | Text | nullable | 参照URL |
| is_visited | Boolean | default: False | 訪問済みフラグ |
| created_at | DateTime | default: utcnow | 登録日時 |

---

## 6. 機能仕様

### 6-1. Discord ボット

**エントリポイント:** `bot/discord_bot.py`

#### 起動条件
- `DISCORD_TOKEN` 環境変数が設定されていること
- ボットへのメンション（`@bot`）でコマンドがトリガーされる
- セキュリティ: `ADMIN_USER_ID` と一致するユーザーのみ操作可能

#### コマンド一覧

| コマンド | 説明 |
|---|---|
| `@bot sync` | チャンネルの過去メッセージ（最大500件）を一括処理 |
| `@bot <URL/テキスト>` | リアルタイムで飲食店情報を抽出・登録 |

#### リアルタイム処理フロー

```
1. ボットへのメンションを検知
2. ⏳ リアクションを付与（処理中）
3. メッセージ重複チェック（messages テーブル参照）
4. テキスト + Embed情報を結合してAI解析に送信
5. 解析結果に応じてリアクション付与:
   ✅ 登録成功
   ⏭️ スキップ（飲食店ではない）
   ❌ 解析失敗
   ⚠️ 例外エラー
6. DB に Message / Shop レコードを保存
7. ⏳ リアクションを削除（処理完了）
```

#### 一括同期（sync）フロー

```
1. チャンネルの過去メッセージを最大500件取得
2. 最後に処理した message_id から再開（差分同期）
3. 各メッセージに対し:
   a. 重複チェック
   b. AI解析（parse_restaurant_info）
   c. URLフォールバック抽出（_extract_url）
   d. DBへの保存
   e. 4秒待機（APIレート制限対策）
```

---

### 6-2. AI 抽出モジュール

**ファイル:** `bot/restaurant_extractor.py`
**モデル:** `gpt-4.1`（OpenAI）
**応答形式:** JSON Object

#### 抽出項目

| フィールド | 型 | 説明 |
|---|---|---|
| ignore | Boolean | 飲食店情報でない場合 true |
| shop_name | String / null | 正式店名（自動補正あり） |
| area | String / null | 最も具体的なエリア名 |
| category | String / null | 業態・ジャンル |
| url | String / null | テキスト中に含まれるURL |

#### 対象判定ルール（AI プロンプト）
- 外食・テイクアウト・デリバリー・お取り寄せ・惣菜等は「対象」
- YouTubeグルメ動画、食べログ・X のリンクも「対象」
- 単なる会話の相槌・食べ物と無関係な話題は「対象外（ignore: true）」

---

### 6-3. Web ダッシュボード

**ファイル:** `web/streamlit_app.py`
**フレームワーク:** Streamlit
**デフォルトポート:** 8501（本番は `$PORT` 環境変数で動的指定）

#### 認証
- `WEB_PASSWORD` 環境変数が設定されている場合、ログインフォームを表示
- セッション単位で認証状態を管理

#### サイドバー機能

| 機能 | 説明 |
|---|---|
| エリアフィルター | ドロップダウンでエリア絞り込み |
| 訪問状況フィルター | All / 未訪問 / 訪問済み |
| CSV ダウンロード | フィルタ結果を UTF-8 BOM CSV でエクスポート |
| CSV インポート（マスター同期） | アップロードCSVをマスターとしてDB全件同期 |

#### メインパネル機能

| 機能 | 説明 |
|---|---|
| 店舗一覧テーブル | shop.name / shop.area / shop.category / status.is_visited / url を表示 |
| URL リンク | url カラムはクリッカブルリンクとして表示 |
| JSON プレビュー | 最新レコードを Kibana 風の JSON ビューで表示 |

#### 表示カラム仕様

| カラム | 表示 | CSV出力 |
|---|---|---|
| _id (id) | 非表示 | 含む |
| @timestamp (created_at) | 非表示 | 含む |
| message_id | 非表示 | 含む |
| shop.name | 表示 | 含む |
| shop.area | 表示 | 含む |
| shop.category | 表示 | 含む |
| status.is_visited | 表示（チェックボックス） | 含む |
| url | 表示（リンク） | 含む |

#### CSV インポート仕様
- 必須カラム: `message_id`, `shop.name`
- message_id の科学的記数法（例: `1.47725e+18`）を自動的に整数文字列へ変換
- インポートロジック:
  - CSVに存在するレコード → 更新（Update）
  - CSVにない既存レコード → 削除（Delete）
  - CSVに新規レコード → 挿入（Insert）

---

## 7. 環境変数

| 変数名 | 必須 | 説明 |
|---|---|---|
| DATABASE_URL | 本番必須 | PostgreSQL 接続文字列（未設定時は SQLite にフォールバック） |
| DISCORD_TOKEN | 必須 | Discord ボットトークン |
| OPENAI_API_KEY | 必須 | OpenAI API キー |
| ADMIN_USER_ID | 推奨 | ボット操作を許可する Discord ユーザー ID |
| WEB_PASSWORD | 推奨 | Web ダッシュボード認証パスワード |
| PORT | 本番自動 | Streamlit 起動ポート（Render.com が自動設定） |

---

## 8. デプロイ手順（Render.com）

1. Render.com にリポジトリを連携
2. `render.yaml` に基づき自動でサービスが作成される
3. 以下の環境変数を Render ダッシュボードで設定:
   - `DISCORD_TOKEN`
   - `OPENAI_API_KEY`
   - `ADMIN_USER_ID`
   - `WEB_PASSWORD`
4. `DATABASE_URL` は PostgreSQL サービスから自動注入される

---

## 9. ローカル開発環境

### セットアップ

```bash
# 依存ライブラリのインストール
pip install -r requirements.txt

# 環境変数の設定
cp .env.example .env  # .env ファイルに各変数を設定

# Streamlit Web UI の起動
streamlit run web/streamlit_app.py

# Discord ボットの起動
python -m bot.discord_bot
```

### ローカルDB
- `DATABASE_URL` が未設定の場合、SQLite（`meshi.db`）を自動使用

---

## 10. 既知の制約・注意事項

| 項目 | 内容 |
|---|---|
| 同期上限 | `sync` コマンドは最大500メッセージまで処理 |
| レート制限 | sync中は各メッセージ処理後に4秒のウェイトを挿入 |
| Discord ID | message_id は String 型で保存（BigInteger オーバーフロー対策） |
| 科学的記数法 | CSV の message_id が科学的記数法の場合、自動変換処理あり |
| Python バージョン | psycopg2-binary の互換性のため 3.11.9 に固定 |
| 管理者制限 | `ADMIN_USER_ID` 未設定時はすべてのユーザーがボット操作可能（非推奨） |
