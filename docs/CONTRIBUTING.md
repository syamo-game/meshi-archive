# 開発・テスト

## テストを実行する

READMEの環境にNode.js 24とテスト用の依存パッケージを追加します。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

通常はSQLiteとJavaScriptのテストを実行します。PostgreSQL専用テストには、破棄できる`test_*`または`*_test`という名前のDBを用意し、接続先を`TEST_POSTGRES_URL`、`ALLOW_DESTRUCTIVE_POSTGRES_MIGRATION_TEST`を`1`に設定します。実データのあるDBの代わりに、テスト専用DBを使ってください。

Dockerでの実行方法です。

```powershell
docker build --target test -t meshi-archive:test .
docker run --rm meshi-archive:test python -m pytest -q
```

GitHub ActionsではPostgreSQL 16も使って全テストを実行し、Gitleaksで秘密情報、pip-auditで依存パッケージを検査します。コンテナの発行や本番への反映は行いません。

## 変更するとき

- 関数には引数と戻り値の型を付け、外部から受け取る値は検証します。
- 例外は処理段階と対象が分かる形で記録します。秘密情報はログへ出さず、設定不足は明示的にエラーにします。
- テストでは不具合の再現条件と期待する結果を確認します。失敗したテストは削除する代わりに、原因を直すか未解決として報告します。
- コミットには`fix:`や`docs:`などの接頭辞を付け、72文字未満で変更の目的を書きます。
- コメントは英語で短く書き、コードだけでは伝わらない判断理由を残します。処理の読み上げや作業履歴の代わりに、名前と型で意図を伝えます。

仕様は[内部の仕組み](architecture.md)と[画面の仕様](ui-framework.md)を確認してください。同じ説明を複数の文書へ繰り返す代わりに、該当箇所へリンクします。
