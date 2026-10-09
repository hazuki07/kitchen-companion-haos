# cooking-assistant daily maintenance

このファイルは`automation/daily`で動く非対話Autopilot専用です。最初に`AGENTS.md`と
`TASKS.md`を読み、Readyから自己完結したタスクを最大1件だけ選びます。`TASKS.md`と
`HANDOFF.md`はVM専用作業メモリであり、Gitへ追加しません。

## 許可する作業

- 架空データとモックだけで検証できる小さなbug fix、単体テスト、error handling、文書更新。
- 変更対象は`TASKS.md`、`README.md`、`docs/PROJECT_STATUS.md`、
  `kitchen_companion/app`配下のPython、`tests`配下のPythonだけ。
- 既存のStageモック、Mealie読み取り専用境界、HA操作の再送防止を維持する。
- Readyが空なら推測で機能を追加せず、変更なしで終了する。

## 禁止事項

- 実Home Assistant、実Mealie、外部レシピサイト、その他のネットワークへ接続しない。
- integration／E2E、OAuth、Docker、HA OS App install、deployを実行しない。
- 実DB、token、`.env`、認証情報、個人情報、実レシピ、実環境固有IDを読まない。
- `AGENTS.md`、本ファイル、`.autopilot/`、`kitchen_companion/config.yaml`、
  `kitchen_companion/run.sh`、Dockerfile、依存設定、Git設定、Autopilot runtimeを変更しない。
- branch変更、reset、clean、restore、stash、push、PR、mergeを行わない。

## 完了手順

1. 選択理由を`TASKS.md`へ記録し、1タスクだけ実装する。
2. `.venv/bin/pytest -q`を実行する。
3. `.venv/bin/ruff check --config pyproject.toml kitchen_companion/app tests`を実行する。
4. `python3 -m compileall -q kitchen_companion/app`と`git diff --check`を実行する。
5. 秘密情報・要求外変更・未追跡ファイルを自己レビューする。
6. 成功時は結果と未検証範囲を`TASKS.md`へ記録し、対象をReview Pendingへ移す。

完全成功時だけ、conventional commit形式のASCII件名、`AUTOPILOT_REVIEW_OK`、Review Pendingへ
移した正確な`AP-<番号>`の3行を返します。失敗時は短い非機密の失敗分類だけを返し、変更を破棄しません。
