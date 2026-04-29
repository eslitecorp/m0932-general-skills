# Audit Skill — Checklist

每條規則標注風險等級、加入日期與外部參考來源。
執行 `audit-skill` 時，model 讀取此檔案作為稽核依據。

若需新增或修改規則，請依 [CONTRIBUTING.md](../CONTRIBUTING.md) 流程開 Issue。

---

## 安全性（Security）

| 檢查項目 | 判斷方式 | 風險等級 | 加入日期 | 參考來源 |
|---|---|---|---|---|
| 是否有 hardcode 的 token / secret / password | 搜尋 `token`、`secret`、`password`、`api_key` 等關鍵字，確認是否為真實值而非佔位符（如 `YOUR_API_TOKEN`） | 🔴 高 | 2026-04-29 | [OWASP: Sensitive Data Exposure](https://owasp.org/www-project-top-ten/2017/A3_2017-Sensitive_Data_Exposure) |
| 是否有 hardcode 的內部 URL / endpoint | 搜尋 `http`、`https` 開頭的字串，確認是否含組織內部網域 | 🔴 高 | 2026-04-29 | [CWE-312: Cleartext Storage of Sensitive Information](https://cwe.mitre.org/data/definitions/312.html) |
| 涉及外部服務的 skill 是否有 `.template` 範本 | 檢查同資料夾是否有 `*.template` 檔案 | 🟡 中 | 2026-04-29 | — |
| 對應的真實設定檔是否已加入 `.gitignore` | 讀取 repo 根目錄 `.gitignore`，確認 `config.ini`、`.env` 等已列入 | 🟡 中 | 2026-04-29 | [GitHub: Ignoring files](https://docs.github.com/en/get-started/getting-started-with-git/ignoring-files) |
| bash 指令是否有 injection 風險 | 確認 skill 中的 bash 範例未使用未經驗證的使用者輸入直接拼接指令 | 🔴 高 | 2026-04-29 | [CWE-78: OS Command Injection](https://cwe.mitre.org/data/definitions/78.html) |

---

## 可追溯性（Traceability）

| 檢查項目 | 判斷方式 | 風險等級 | 加入日期 | 參考來源 |
|---|---|---|---|---|
| frontmatter 是否有 `issue:` 欄位 | 檢查 YAML frontmatter 是否存在 `issue:` 且值為有效 URL | 🟡 中 | 2026-04-29 | — |
| `description` 是否包含觸發語句 | 確認 description 中有「觸發語句：」或明確的觸發範例 | 🟡 中 | 2026-04-29 | — |

---

## 格式合規（Format Compliance）

| 檢查項目 | 判斷方式 | 風險等級 | 加入日期 | 參考來源 |
|---|---|---|---|---|
| frontmatter 是否完整（`name`、`description`、`tags`） | 檢查 YAML frontmatter 必填欄位是否存在且非空 | 🟠 中 | 2026-04-29 | — |
| 是否有 `## 工作流程` 區塊 | 搜尋 `## 工作流程` 標題 | 🟠 中 | 2026-04-29 | — |
| 是否有 `## 注意事項` 區塊 | 搜尋 `## 注意事項` 標題 | 🟢 低 | 2026-04-29 | — |
| 步驟是否有具體的 bash 指令或判斷邏輯 | 確認每個 Step 至少有一個 code block 或表格 | 🟠 中 | 2026-04-29 | — |

---

## 維護性（Maintainability）

| 檢查項目 | 判斷方式 | 風險等級 | 加入日期 | 參考來源 |
|---|---|---|---|---|
| 是否有過度設計跡象 | 搜尋「未來」、「TODO」、「待補」、「暫時」等關鍵字 | 🟢 低 | 2026-04-29 | — |
| `name` 是否符合 kebab-case 命名規則 | 確認全小寫、無空格、無底線 | 🟢 低 | 2026-04-29 | — |
| skill 資料夾名稱是否與 `name` 欄位一致 | 比對資料夾名稱與 frontmatter `name` 值 | 🟠 中 | 2026-04-29 | — |
