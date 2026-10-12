# Kitchen Companion

AndroidタブレットとHome Assistant OSで使う、個人向けの料理アシスタントです。

## 現在の位置づけ

このリポジトリは新しいタブレットUIの試作版です。実環境にはすでにHome Assistantの「調理アシスト」
ダッシュボード、工程案内スクリプト、ヘルパー、Mealieレシピが存在します。それらは削除対象ではなく、
このアプリが段階的に引き継ぐ移行元です。

- **Mealie**: 確認済みレシピの主保存先
- **Home Assistant**: 工程状態、Echo読み上げ、Assist会話、将来の家電・買い物リスト連携
- **Kitchen Companion**: タブレットでの検索、写真一覧、工程表示、動画操作、保存前確認
- **SQLite**: 現段階ではローカル下書きと開発用データ。Mealieの代替ではありません

実HA OSへの新App導入と、既存設定からの切り替えはまだ実施していません。移行計画と確認済みの現状は
[`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md)にまとめています。

## 現在できること

- 料理名・材料・タグからレシピを検索
- 手入力でレシピと動画URLを登録
- `schema.org/Recipe`対応ページをURLから下書きとして取り込み
- 人数を変えて材料の分量を自動計算
- 1工程ずつ大きく表示する調理ナビ
- ブラウザの音声入力と日本語読み上げ
- 調理中の音声認識を自動再接続し、画面復帰時にもマイク待受を再開
- 音声またはボタンでタイマーを開始
- 実HAモードでは専用スクリプトへタイマー開始・停止を送り、Alexa発報へ接続可能
- SQLiteへローカル下書きを保存
- Stage専用の読み取り専用Mealieモックを検索・表示
- Mealie画像を認証情報をブラウザへ渡さず同一オリジンで中継
- 「調理アシスト」と「レシピ取り込み」を分離し、Mealie・Web URL・動画URLを出典別に選択
- Mealieまたはレシピページに動画URLがある場合は、レシピ詳細から動画を開ける形で保持
- 動画ページの公開メタデータからタイトル・説明・サムネイルを自動入力し、Recipe構造化データまたは説明欄に明記された材料・工程も反映
- 工程操作の一意request IDと永続状態による二重実行防止

外部ページから取り込んだ内容は、必ず保存前に確認してください。出典URLはレシピに残ります。
Mealieへの確認後保存、既存HA工程スクリプトとの接続、Echo読み上げは次の統合段階です。
Mealieから取得した基準人数や材料の量が空欄の場合は推測せず、`未確認`として表示し、人数換算を無効にします。

## 開発環境で試す

Python 3.12以上を使用します。外部パッケージは不要です。

```sh
cd kitchen_companion
python3 -m app.server --db ../data/dev.sqlite3
```

ブラウザで `http://127.0.0.1:8099` を開きます。Androidから同じネットワーク経由で開く場合に限り
`--host 0.0.0.0`を指定してください。インターネットへ直接公開しないでください。

Stage VMにはHome AssistantもMealieもありません。既定の`COOKING_ASSISTANT_MODE=stage`では、工程操作と
Mealieカタログをローカルのモックで置き換え、画面上にも「Stage（HA・Mealieモック）」と表示されます。このモードは実HA・Mealieへ
接続も書き込みもしません。

HA OS上で試作する場合はApp設定を`mode: prototype`にし、`timer_script_entity_id`へ専用の
Alexaタイマースクリプトを設定できます。このモードはレシピと工程案内をモックのまま維持し、タイマーボタンだけが
指定したHome Assistant scriptを呼びます。Mealieやその他のHome Assistant entityへは接続しません。

## 実環境の接続設定（まだ切り替えない）

Home Assistant Appとして動かす場合、HA認証は公式のSupervisorプロキシと自動提供される
`SUPERVISOR_TOKEN`を使います。長期アクセストークンを手入力する必要はありません。

MealieのURLとtoken、呼び出す料理工程scriptのentity IDは、Git管理外のApp設定ディレクトリにある
`integrations.json`から読み取ります。形式は`kitchen_companion/integrations.example.json`を参照してください。
Alexaタイマーは同じ設定の`timer_script_entity_id`へ`operation`と`minutes`を渡します。実際のAlexa
通知先はHome Assistant側の専用scriptだけに固定し、AppやGit管理文書へ実entity IDを記録しません。
Appにはこのディレクトリが読み取り専用でmountされます。tokenをリポジトリ、README、画面、ログへ
貼り付けないでください。

App設定の`mode`は既定で`stage`です。`home_assistant`への変更は、バックアップと読み取り検証が完了し、
実環境での接続を明示的に承認した後だけ行います。

## Home Assistant OSへ入れる

Home AssistantのApp Storeでリポジトリ管理画面を開き、次のURLをカスタムリポジトリとして追加します。

```text
https://github.com/hazuki07/kitchen-companion-haos
```

一覧へ表示された`Kitchen Companion`をインストールします。初回はApp設定の`mode: stage`を維持し、
実Home Assistant・Mealieへ接続しないモック状態で画面を確認してください。

起動後はサイドバーの「料理アシスタント」から開けます。Ingressを使用するため、外部ポートは
公開しません。SQLiteはApp専用の`/data`へ保存されますが、移行完了後もMealieを主保存先とします。

現時点では実HAへインストールせず、既存ダッシュボードと設定を先にエクスポートしてください。

## 音声について

検索と調理コマンドはAndroidブラウザの音声認識機能が利用できる場合に動作します。HTTPSで開いて
「調理をはじめる」を押すと音声操作が自動的にONになり、調理画面右上のボタンでON/OFFを切り替えられます。
初回はブラウザのマイク権限を許可してください。利用できない端末でも、検索欄と大きな操作ボタンですべての
基本操作を行えます。読み上げにはブラウザの音声合成を使い、読み上げ中は誤認識防止のため音声認識を一時停止します。

対応する調理コマンドは「次へ」「戻って」「読み上げ」「5分タイマー」「停止」です。

工程操作の通信結果を確認できない場合、Appはその操作を`outcome_unknown`として記録し、自動再送を
行いません。画面を連打しても送信中の操作は重複送信されません。実環境ではHome Assistant側の工程状態を
確認してから次の操作へ進めます。

## テスト

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q kitchen_companion/app
```

テストは一時DBと架空データのみを使い、外部サイトへ接続しません。
