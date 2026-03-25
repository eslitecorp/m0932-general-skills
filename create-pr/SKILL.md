---
name: create-pr
description: "分析 git diff 將變更分類成獨立的 atomic commits（繁體中文訊息），並建立 PR 到 main 分支。觸發語句：「幫我建立 PR」、「commit 並開 PR」、「create pr」、「/create-pr」。"
tags: ["git", "pr", "commit", "atomic", "github"]
---

# Create PR — Atomic Commit & Pull Request

分析目前工作目錄的 git 差異，自動分類變更、執行 atomic commits，再建立 PR 到 main 分支。

---

## 工作流程

### Step 1：確認工作狀態

執行以下指令，取得完整變更清單：

```bash
git status
git diff HEAD
git diff --cached
```

若工作目錄無任何變更，告知使用者並結束。

---

### Step 2：分析並分類變更

依照以下分類邏輯，將所有變更檔案歸入對應類型。**每個分類將形成一個獨立的 atomic commit。**

| 類型 | 判斷條件 | Commit 前綴 |
|---|---|---|
| **新功能** | 新增功能邏輯、新增 API endpoint、新增模組 | `feat` |
| **修正錯誤** | 修正 bug、行為不符預期 | `fix` |
| **重構** | 不改變行為的程式碼整理、改名、搬移 | `refactor` |
| **文件** | README、SKILL.md、註解、文件更新 | `docs` |
| **設定** | 設定檔、環境變數、CI/CD、依賴套件 | `chore` |
| **測試** | 新增或修改測試 | `test` |
| **樣式** | 格式化、縮排、不影響邏輯的排版 | `style` |

> 若一個檔案跨越多個類型，以**主要意圖**為準；如難以判斷，以 `refactor` 為預設。

---

### Step 3：向使用者確認分組

在執行 commit 前，列出分組結果供確認：

```
以下是預計的 commit 分組，請確認是否正確：

[1] feat：新增使用者登入功能
    - src/auth/login.ts
    - src/auth/types.ts

[2] chore：更新套件依賴
    - package.json
    - package-lock.json

[3] docs：更新 README 操作說明
    - README.md

是否繼續？(y/n)
```

---

### Step 4：執行 Atomic Commits

依照確認的分組，逐一 stage 並 commit。

**Commit 訊息格式（繁體中文）：**

```
<類型>(<範圍>): <簡短說明>

<詳細說明（選填）>
```

**規則：**
- 第一行不超過 72 字元
- 類型使用英文小寫前綴（`feat`, `fix`, `refactor`, `docs`, `chore`, `test`, `style`）
- 範圍（scope）為受影響的模組或資料夾名稱，可省略
- 簡短說明使用**繁體中文**，動詞開頭，描述「做了什麼」
- 詳細說明（選填）解釋「為什麼這樣做」

**範例：**
```
feat(auth): 新增使用者登入驗證功能

實作 JWT token 驗證邏輯，支援 email/password 登入，
並於登入失敗時回傳標準化錯誤訊息。
```

```
fix(cart): 修正購物車數量計算錯誤

當商品數量為 0 時，原先邏輯未正確清除購物車項目，
導致結帳時出現 NaN 金額。
```

```
chore: 更新 Python 依賴套件至最新版本
```

**執行方式（每個分組依序）：**

```bash
git add <該分組的檔案列表>
git commit -m "$(cat <<'EOF'
<類型>(<範圍>): <繁體中文說明>

<詳細說明（若有）>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Step 5：建立 PR

所有 commit 完成後，推送並建立 PR。

**推送到遠端：**

```bash
git push -u origin <branch-name>
```

若目前在 main 分支，先建立功能分支：

```bash
git checkout -b <功能描述的分支名稱>
# 分支命名規則：kebab-case，全小寫，例如：add-login-feature、fix-cart-calculation
```

**建立 PR：**

```bash
gh pr create \
  --base main \
  --title "<本次變更的簡短說明（繁體中文）>" \
  --body "$(cat <<'EOF'
## 變更摘要

<1-3 條重點說明本次 PR 的目的>

## Commits

<列出本次 PR 包含的所有 atomic commits>

## 測試確認

- [ ] 功能已在本機驗證
- [ ] 未破壞現有功能
- [ ] 相關文件已更新（如適用）

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

### Step 6：輸出結果摘要

PR 建立完成後，輸出摘要報告：

```
PR 建立完成！

分支：<branch-name>
PR 連結：<PR URL>

本次包含 <N> 個 atomic commits：
  [1] feat(auth): 新增使用者登入驗證功能
  [2] chore: 更新套件依賴

Code Owner 審查已自動指派，PR 合併至 main 前需獲得審查核准。
```

---

## 注意事項

- **不要** 將 `.env`、credentials、secrets 等敏感檔案加入 commit
- **不要** 使用 `git add .` 或 `git add -A`，務必逐一指定檔案
- **不要** 跳過 pre-commit hooks（`--no-verify`）
- **不要** force push 到 main 分支
- 若 commit hook 失敗，修正問題後建立**新的** commit，不要使用 `--amend`
