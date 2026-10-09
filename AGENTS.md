# Kitchen Companion 開発ルール

## 構成

- Python 3.12以上を使用し、外部依存を増やす前に標準ライブラリで実現できるか確認する。
- `kitchen_companion`はHome Assistant OSのApp（旧称Add-on）としてIngress内で動作させる。
- Mealieを既存レシピの主保存先とする。`/data/recipes.sqlite3`はローカル下書き・キャッシュ・移行確認に限定し、Mealieを無条件に置き換えない。
- 既存のHome Assistantダッシュボード、ヘルパー、スクリプト、Mealieレシピを移行元資産として保護する。
- 既存資産はバックアップまたは設定エクスポート、差分確認、移行検証の順で扱い、検証前に削除・無効化しない。
- Androidタブレットの横画面を主対象としつつ、縦画面とスマートフォン表示も壊さない。

## 安全性と個人情報

- 実レシピDB、Home Assistant token、`.env`、認証情報、個人情報を読み取り・表示・commitしない。
- 外部URL取り込みではHTTP/HTTPS以外と、loopback・private・link-local等の非公開IPを拒否する。
- 外部ページの内容は信頼せず、サイズ上限・タイムアウト・入力検証を維持する。
- 取り込んだコンテンツは出典URLを保持し、元ページの全文複製を目的にしない。
- 実Home Assistant、実レシピサイト、外部APIへ自動接続しない。必要なら先に承認を得る。
- Home AssistantやMealieの内部URL、config entry ID、recipe UUID・slugなど実環境固有値は、Git管理文書へ記録しない。必要ならGit除外済みの`docs/local/`へ保存する。
- 出典にない分量・時間は推測せず「未確認」とする。一般知識による回答を保存済みレシピや動画内容として扱わない。

## 検証

Python変更後は次を実行する。

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q kitchen_companion/app
ruff check --config pyproject.toml kitchen_companion/app tests
```

- テストでは架空データ、一時DB、モックを使用し、外部ネットワークへ接続しない。
- HA OSへのインストール、Docker build、外部URL取り込みは自動実行しない。
- 未実施の検証は理由と範囲を明記する。

## Git

- 既存変更を破棄しない。`reset`、`clean`、`restore`、`stash`を無断で行わない。
- `main`へ直接変更しない。`feature/*`ブランチを使用する。
- commit、push、merge、deployは明示的に依頼された場合だけ行う。
