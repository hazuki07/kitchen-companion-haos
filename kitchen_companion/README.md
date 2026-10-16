# Kitchen Companion

AndroidタブレットとHome Assistant OSで使う、個人向けの料理アシスタントです。

初回導入時はApp設定の `mode` を `stage` のままにしてください。このモードでは
Home AssistantとMealieをモックへ置き換え、実環境への接続と書き込みを行いません。

実Home Assistant・Mealieとの接続は、既存設定のバックアップと読み取り検証を終えた後にだけ
`home_assistant`へ切り替えます。Mealie tokenなどの秘密値をGitへ保存しないでください。

`home_assistant`モードでは、ローカル下書きの詳細画面から確認済みレシピをMealieへ手動同期できます。
同期はボタン操作時だけ実行し、削除はMealieへ伝播しません。結果不明の通信障害では自動再送しません。
ローカル下書きだけをアプリから削除できます。Mealieレシピの削除はMealie側で行います。

選択したレシピの詳細画面から、材料をHome Assistantの買い物リストへ、工程をタスクリストへ
追加できます。`home_assistant`モードではApp設定の`shopping_list_entity_id`と
`task_list_entity_id`へ対象の`todo.*` entity IDを指定してください。未設定の送信先は無効になり、
通信結果が不明な操作は自動再送されません。
