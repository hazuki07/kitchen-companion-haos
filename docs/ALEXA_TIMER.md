# Alexaタイマー連携契約

Kitchen CompanionはAlexaのentityや通知serviceを直接呼びません。実Home Assistantモードでは、
Git管理外の`integrations.json`に設定した`timer_script_entity_id`だけを呼び出します。

```json
{
  "home_assistant": {
    "script_entity_id": "script.replace_with_cooking_guide",
    "timer_script_entity_id": "script.replace_with_kitchen_timer"
  }
}
```

タイマースクリプトへ渡す変数は次の固定契約です。

| 変数 | 値 |
| --- | --- |
| `operation` | `start`または`stop` |
| `minutes` | 開始時は1から180、停止時は0 |

Home Assistant側では、`start`で専用timer helperを開始し、その`timer.finished`をトリガーに既存の
キッチンEcho通知先へ発報します。`stop`では同じhelperを取消します。通知先の実entity IDは
`docs/local/`などGit除外済みの実環境メモだけで管理してください。

結果不明時にAppは自動再送しません。Home Assistant側のtimer helperを確認してから再操作します。
