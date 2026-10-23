# Copilot Instructions

## Issue 状態ラベル運用（wf:*）

ラベル運用は必ず `docs/label-operation-rule.md` に従うこと。ユーザーが毎回指示しなくても、以下を自動で守る。

- Issue 作成時は `wf:plan-ready` を付与する。
- 実装開始時に `wf:plan-ready` → `wf:impl-running`、実装結果の提出後に `wf:impl-running` → `wf:impl-done` へ、エージェントが遷移させる。
- `wf:decision-needed` → `wf:done`（および Go / Rework の判断）は人間が決定する。エージェントは勝手に変更せず、必要なら提案のみ行う。
- レビュー依頼・返却に関する遷移（`wf:review-needed` 等）は、依頼を受けた場合のみルールどおり実施する。
- 状態ラベルは同時に1つだけ。付け替え時は旧ラベルを外す。
- 実施したラベル遷移（例: `wf:impl-running` → `wf:impl-done`）は完了報告に記載する。

## Issue と PR の紐付けルール

1 つの Issue に複数の PR が紐づき得るため、自動クローズは使わない。

- Issue に関わる PR の本文には `Refs #番号` を入れる。
- `Closes` / `Fixes` / `Resolves` は使わない（複数 PR のうち最初のマージで Issue が閉じてしまうため）。
- Issue の Close と `wf:done` への遷移は人間が決定する。エージェントは自動で行わない（上記ラベル運用の `wf:decision-needed` → `wf:done` と同じ）。
