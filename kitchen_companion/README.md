# Kitchen Companion

AndroidタブレットとHome Assistant OSで使う、個人向けの料理アシスタントです。

初回導入時はApp設定の `mode` を `stage` のままにしてください。このモードでは
Home AssistantとMealieをモックへ置き換え、実環境への接続と書き込みを行いません。

実Home Assistant・Mealieとの接続は、既存設定のバックアップと読み取り検証を終えた後にだけ
`home_assistant`へ切り替えます。Mealie tokenなどの秘密値をGitへ保存しないでください。

`home_assistant`モードでは、ローカル下書きの詳細画面から確認済みレシピをMealieへ手動同期できます。
同期はボタン操作時だけ実行し、削除はMealieへ伝播しません。結果不明の通信障害では自動再送しません。
ローカル下書きだけをアプリから削除できます。Mealieレシピの削除はMealie側で行います。
