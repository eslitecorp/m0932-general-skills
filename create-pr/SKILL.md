---
name: create-pr
description: "分析 git diff 將變更分類成獨立的 atomic commits（gitmoji 格式，繁體中文訊息），並建立 PR 到預設主線分支。觸發語句：「幫我建立 PR」、「commit 並開 PR」、「create pr」、「/create-pr」。"
---

# Create PR — Atomic Commit & Pull Request

分析目前工作目錄的 git 差異，自動分類變更、執行 atomic commits，再建立 PR 到預設主線分支。

---

## 工作流程

### Step 1：確認工作狀態與分支

執行以下指令，取得完整變更清單與當前分支：

```bash
git status
git branch --show-current
git diff HEAD
git diff --cached
```

若工作目錄無任何變更，告知使用者並結束。

**分支檢查（必須在 commit 前處理）：**

若目前在 `main`（或 `master`）分支，**立即**建立功能分支，再繼續後續步驟：

```bash
git checkout -b <功能描述的分支名稱>
# 分支命名規則：kebab-case，全小寫，例如：add-login-feature、fix-cart-calculation
```

> 若使用者未指定分支名稱，從變更內容推斷一個合適的名稱，並告知使用者。

---

### Step 2：分析並分類變更

依照以下 gitmoji 分類，將所有變更檔案歸入對應類型。**每個分類將形成一個獨立的 atomic commit。**

| Emoji | 用途 |
|---|---|
| ✨ | 新增功能 |
| 🐛 | 修正 bug |
| ♻️ | 重構（不改變行為） |
| 📝 | 文件、README、註解 |
| 🔧 | 設定檔、環境變數、CI/CD |
| 📦️ | 新增或更新相依套件 |
| ✅ | 新增或更新測試 |
| 💄 | UI / 樣式調整 |
| 🔥 | 刪除程式碼或檔案 |
| 👷 | CI build system |

> 若一個檔案跨越多個類型，以**主要意圖**為準；難以判斷時預設用 ♻️。

---

### Step 3：向使用者確認分組

在執行 commit 前，列出分組結果供確認：

```
以下是預計的 commit 分組，請確認是否正確：

[1] ✨ 新增使用者登入功能
    - src/auth/login.ts
    - src/auth/types.ts

[2] 📦️ 更新套件依賴
    - package.json
    - package-lock.json

[3] 📝 更新 README 操作說明
    - README.md

是否繼續？(y/n)
```

---

### Step 4：執行 Atomic Commits

依照確認的分組，逐一 stage 並 commit。

**Commit 訊息格式（繁體中文）：**

```
<emoji> (<範圍>): <簡短說明>
```

**規則：**
- 第一行不超過 72 字元
- 以 gitmoji emoji 開頭（直接用 emoji 字元，非 `:code:`）
- 範圍（scope）為受影響的模組或資料夾名稱，可省略
- 說明使用**繁體中文**，簡潔描述「做了什麼」

**範例：**
```
✨ (auth): 新增使用者登入驗證功能
🐛 (cart): 修正購物車數量計算錯誤
🔧: 更新 Python 依賴套件至最新版本
```

**執行方式（每個分組依序）：**

先取得當前 git 使用者資訊，用於 `Co-Authored-By`：

```bash
git config user.name
git config user.email
```

```bash
git add <該分組的檔案列表>
git commit -m "$(cat <<'EOF'
<類型>(<範圍>): <繁體中文說明>

<詳細說明（若有）>

Co-Authored-By: <git config user.name> <<git config user.email>>
Co-Authored-By: <當前使用的 AI Agent 名稱，例如：Roo, Claude> <noreply@anthropic.com>
EOF
)"
```

---

### Step 5：建立 PR

所有 commit 完成後，推送並建立 PR。

**取得預設主線分支名稱：**

```bash
git remote show origin | grep 'HEAD branch' | awk '{print $NF}'
```

**推送到遠端：**

```bash
git push -u origin <branch-name>
```

**建立 PR：**

PR title 格式：`<emoji> (<scope>): <說明>`，例如 `✨ (auth): 新增登入驗證功能`

```bash
gh pr create \
  --base <預設主線分支，通常為 main 或 master> \
  --title "<emoji> (<scope>): <繁體中文說明>" \
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
  [1] ✨ (auth): 新增使用者登入驗證功能
  [2] 📦️: 更新套件依賴

Code Owner 審查已自動指派，PR 合併至 main 前需獲得審查核准。
```

---

## 注意事項

- **不要** 將 `.env`、credentials、secrets 等敏感檔案加入 commit
- **不要** 使用 `git add .` 或 `git add -A`，務必逐一指定檔案
- **不要** 跳過 pre-commit hooks（`--no-verify`）
- **不要** force push 到 main 分支
- 若 commit hook 失敗，修正問題後建立**新的** commit，不要使用 `--amend`
