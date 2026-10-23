# ラベル運用ルール（trade-engine）

## 目的
Issueを見ただけで「今どの工程か」「次に誰が何をするか」を即判断できる状態を作る。

---

## ラベル一覧と意味

| ラベル | 状態 | 主担当 | 次アクション |
|---|---|---|---|
| `wf:plan-ready` | 要件整理・実装指示が完了 | Copilot(chat) → あなた | あなたがCopilot(VSCode)へ実装投入 |
| `wf:impl-running` | 実装中 | Copilot(VSCode) | 実装・テスト・結果報告 |
| `wf:impl-done` | 実装結果が揃った | Copilot(chat) | 必要ならClaudeレビュー依頼作成 |
| `wf:review-needed` | Claudeレビュー待ち/中 | Claude | レビュー結果を返却 |
| `wf:decision-needed` | 判断待ち | あなた | Go / Rework を決定 |
| `wf:done` | 完了 | あなた | Closeして終了 |

---

## 遷移ルール（基本フロー）

1. Issue作成時: `wf:plan-ready`
2. 実装開始時: `wf:plan-ready` → `wf:impl-running`
3. 実装結果提出後: `wf:impl-running` → `wf:impl-done`
4. レビュー依頼時: `wf:impl-done` → `wf:review-needed`
5. レビュー返却後: `wf:review-needed` → `wf:decision-needed`
6. 採用して完了: `wf:decision-needed` → `wf:done`

---

## 例外フロー

### A. レビュー不要な軽微修正
`wf:impl-done` → `wf:decision-needed` → `wf:done`

### B. 差し戻し（再実装）
`wf:decision-needed` → `wf:impl-running`
（同じIssue内で継続、Task IDは据え置き）

### C. 要件変更が発生
`wf:impl-done` or `wf:review-needed` → `wf:plan-ready`
（Copilot(chat)が指示書を更新して再スタート）

---

## 運用の厳守事項

1. **同時に付ける状態ラベルは1つだけ**
   - 進捗ラベルの多重付与は禁止（状態が曖昧になるため）

2. **コメントテンプレのTYPEを必ず入れる**
   - `IMPLEMENTATION_REQUEST`
   - `IMPLEMENTATION_RESULT`
   - `REVIEW_REQUEST`
   - `REVIEW_RESULT`

3. **Task ID（TRD-XXX）を全コメントに記載**
   - 検索性・追跡性を維持するため

4. **判断は必ず `wf:decision-needed` を経由**
   - 「いつ誰が採否を決めたか」を明確に残す

---

## あなた用ショートチェック（30秒）

- いまのラベルは？
- そのラベルの主担当は誰？
- 次アクションは何？
- Go/No-Goの判断が必要か？

これだけ確認すれば、迷わず再開できる。
