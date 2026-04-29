---
name: audit-skill
description: "稽核 repo 中所有 skill 的安全性、格式合規與可追溯性，產出風險報告並同步 README 索引。觸發語句：「稽核 skill」、「audit skill」、「檢查 skill 品質」、「/audit-skill」。"
tags: ["skill", "audit", "meta", "security"]
---

# Audit Skill — Skill 品質稽核

對 repo 中所有 `*/SKILL.md` 執行系統性稽核，檢查安全性、格式合規與可追溯性，產出風險報告，並同步更新 `README.md` 索引。

---

## 工作流程

### Step 1：掃描所有 Skill

列出 repo 中所有 skill：

```bash
find . -maxdepth 2 -name "SKILL.md" -not -path "./.git/*" | sort
```

逐一讀取每個 `SKILL.md` 的完整內容，準備進行稽核。

---

### Step 2：執行稽核 Checklist

讀取 `audit-skill/checklist.md`，依照其中所有規則逐一檢查每個 skill：

```bash
cat audit-skill/checklist.md
```

Checklist 分為四個維度（安全性、可追溯性、格式合規、維護性），每條規則包含：
- **判斷方式**：如何判斷該項目是否通過
- **風險等級**：🔴 高 / 🟡 中 / 🟠 中 / 🟢 低
- **參考來源**：對應的業界標準（OWASP、CWE 等），可用於判斷規則是否仍為當前最佳實踐

> **注意**：若 checklist.md 中的參考來源連結指向的標準已有更新版本，應在稽核報告中標注，並建議開 Issue 更新 checklist。

---

### Step 3：產出稽核報告

依照以下格式輸出每個 skill 的稽核結果：

```
Skill 稽核報告
─────────────────────────────────────────
稽核時間：<ISO 8601 時間>
稽核範圍：<N> 個 skill

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[1] <skill-name>
    路徑：<skill-name>/SKILL.md
    狀態：<✅ 通過 / ⚠️ 有警告 / 🔴 有高風險項目>

    安全性：
      ✅ 無 hardcode token/secret
      ✅ 無 hardcode 內部 URL
      ⚠️ 缺少 config.template 範本

    可追溯性：
      🔴 缺少 issue: 欄位
      ✅ description 包含觸發語句

    格式合規：
      ✅ frontmatter 完整
      ✅ 有工作流程區塊
      ⚠️ 缺少注意事項區塊

    維護性：
      ✅ 命名符合 kebab-case

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[2] ...

─────────────────────────────────────────
總結：
  🔴 高風險：<N> 項（需立即處理）
  🟡 中風險：<N> 項（建議處理）
  🟢 低風險：<N> 項（可選處理）

需要處理的 skill：
  - <skill-name>：<最高風險項目描述>
─────────────────────────────────────────
```

若所有 skill 均通過，輸出：

```
✅ 所有 <N> 個 skill 稽核通過，無風險項目。
```

---

### Step 4：同步 README 索引

稽核完成後，**必須**更新 repo 根目錄的 `README.md` 索引。

**4-1. 讀取目前 README.md**

```bash
cat README.md
```

**4-2. 彙整所有 skill 資訊**

讀取每個 `*/SKILL.md` 的 `name`、`description`、`tags` 欄位。

**4-3. 依分類更新索引表**

依照以下分類標準歸類所有 skill：

| 分類 | 適用類型 | 判斷關鍵字 |
|---|---|---|
| **Git 工作流程** | commit、PR、branch、版本控制 | git, commit, pr, branch, merge |
| **Meta / Skill 管理** | skill 建立、管理、稽核、Claude 工具 | skill, claude, meta, audit |
| **報告 / 資料整合** | 報告產生、資料抓取、外部系統整合 | report, data, export, sync, api |
| **監控 / Observability** | Dashboard、SLA、告警、指標 | monitoring, datadog, sla, alert |
| **AI / 自動化** | AI 呼叫、自動化腳本、排程 | ai, automation, schedule |
| **其他** | 不屬於上述類型 | — |

README 索引格式：

```markdown
## Skill 索引

### <分類名稱>

| Skill | 說明 |
|---|---|
| [skill-name](skill-name/) | <從 description 提取的一句話說明，30 字以內> |
```

完整替換 README 的索引區塊，不要只追加。

---

## 注意事項

- 稽核對象為 repo 內所有 `*/SKILL.md`，不包含 `~/.claude/skills/`（避免跨 repo 副作用）
- 高風險項目（🔴）需在報告中明確列出，並建議使用者開 Issue 追蹤修正
- 稽核本身不自動修改任何 SKILL.md，僅產出報告；修正需依 CONTRIBUTING.md 流程進行
- 若 README 索引與實際 skill 清單不一致，Step 4 會自動修正，此為例外允許的直接修改
