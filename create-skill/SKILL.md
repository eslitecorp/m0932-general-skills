---
name: create-skill
description: "協助建立或迭代更新 Claude Code skill。先掃描現有 skill 避免重複，再以標準格式產出一致的 SKILL.md，最後給出簡易報告。觸發語句：「幫我建立 skill」、「新增 skill」、「create skill」、「/create-skill」。"
tags: ["skill", "claude", "meta", "workflow"]
---

# Create Skill — Skill 建立助手

協助同仁建立或迭代更新 Claude Code skill，確保格式一致、避免重複，任何 model 皆可有效執行。

---

## 工作流程

### Step 1：收集需求

向使用者詢問：

> 請簡短描述你想建立的 skill：
> - **名稱**（英文 kebab-case，例如：`weekly-report`）
> - **用途**：這個 skill 要解決什麼問題？
> - **觸發情境**：使用者會在什麼時候用到它？
> - **輸入**（可選）：需要使用者提供哪些資訊？
> - **產出**：執行完後要產生什麼？

若使用者只給了簡短描述（例如：「幫我建立一個產週報的 skill」），從描述中推斷上述資訊，再向使用者確認。

---

### Step 2：掃描現有 Skill（去重檢查）

搜尋 skill 存放路徑，比對是否已有功能相似或名稱雷同的 skill：

**搜尋位置（依序檢查）：**
1. 目前 repo 的所有 `*/SKILL.md`
2. `~/.claude/skills/*/SKILL.md`

**相似度判斷標準：**

| 情況 | 處理方式 |
|---|---|
| 名稱完全相同 | 詢問使用者是否要**迭代更新**現有 skill |
| 功能高度重疊（> 70%） | 告知使用者，建議**更新現有 skill** 或合併 |
| 功能部分重疊（30-70%） | 提示使用者，讓其決定新建或更新 |
| 無重疊 | 直接進行新建 |

若決定**迭代更新**，跳至 [Step 4：撰寫 SKILL.md] 並說明是更新哪個檔案。

---

### Step 3：確認 Skill 規格

向使用者確認以下規格後再動筆：

```
即將建立的 skill 規格如下，請確認：

名稱：<skill-name>
操作：<新建 / 更新現有 skill>
說明：<一句話描述>
標籤：<tag1, tag2, ...>
觸發語句：<範例 1>、<範例 2>
工作流程概覽：
  1. <第一步>
  2. <第二步>
  ...
產出：<最終會產出什麼>

是否正確？(y/n)
```

---

### Step 4：撰寫 SKILL.md

依照以下標準格式撰寫，確保任何 model 皆能有效執行：

**檔案位置：** `<repo-root>/<skill-name>/SKILL.md`

**格式規範：**

```markdown
---
name: <skill-name>
description: "<一句話描述，需涵蓋觸發語句範例。長度建議 50-150 字>"
tags: ["<tag1>", "<tag2>"]
---

# <Skill 標題>

<2-3 句說明此 skill 的用途與產出>

---

## 工作流程

### Step N：<步驟標題>

<清楚說明這個步驟要做什麼，包含：>
- 需要執行的指令（以 code block 呈現）
- 需要詢問使用者的問題（以 blockquote > 呈現）
- 判斷邏輯（以表格或條件清單呈現）
- 預期的輸出格式

...（重複各 Step）

---

## 注意事項

- <限制事項 1>
- <限制事項 2>
```

**撰寫原則：**

1. **步驟要具體可執行**：每個 step 告訴 model「要做什麼」、「怎麼做」、「做完的樣子」
2. **指令要完整**：不要只寫「執行 git diff」，要寫出完整的 bash 指令
3. **判斷邏輯要明確**：用表格或 if/else 說明條件，避免模糊描述
4. **輸出格式要固定**：提供輸出範本，讓每次執行結果一致
5. **description 要包含觸發語句**：讓 Claude 能正確判斷何時執行此 skill
6. **避免過度設計**：不要加入「未來可能需要」的功能，聚焦當前需求

**安全性規範（建立 skill 時必須檢查）：**

若 skill 涉及外部服務、API 呼叫或任何需要認證的操作，**必須**執行以下步驟：

| 檢查項目 | 要求 |
| --- | --- |
| API Token / Secret | 不得寫入任何檔案，改用環境變數或 config 檔 |
| 設定檔（config.ini / .env） | 加入 `.gitignore`，並提供 `*.template` 範本 |
| URL / Endpoint | 若含組織內部資訊，移至 config 檔而非寫死在 SKILL.md |
| SKILL.md 範例 | 範例中的 token/key 一律用佔位符，例如 `YOUR_API_TOKEN` |

**template 範本格式（以 config.ini 為例）：**

```ini
[service]
; 服務網址
url = "https://your-instance.example.com"
; API Token 取得方式：Service → Profile → API Token
token = "YOUR_API_TOKEN"
```

建立完 template 後，確認 `.gitignore` 已加入對應的真實設定檔路徑（例如 `config.ini`、`.env`）。

---

### Step 5：更新 README 索引

SKILL.md 建立（或更新）完成後，**必須**更新 repo 根目錄的 `README.md`。

**5-1. 讀取目前 README.md**

```bash
cat README.md
```

**5-2. 掃描所有現有 skill 資訊**

讀取每個 `*/SKILL.md` 的 `name`、`description`、`tags` 欄位，彙整出完整的 skill 清單。

**5-3. 重新審視分類（每次必做）**

依照以下分類標準，將**所有 skill（含本次新建/更新的）**重新歸類，確認分類是否合理：

| 分類 | 適用類型 | 判斷關鍵字 |
|---|---|---|
| **Git 工作流程** | commit、PR、branch、版本控制相關 | git, commit, pr, branch, merge |
| **Meta / Skill 管理** | skill 建立、管理、規範、Claude 工具 | skill, claude, meta, workflow |
| **報告 / 資料整合** | 報告產生、資料抓取、外部系統整合 | report, data, export, sync, api |
| **監控 / Observability** | Dashboard、SLA、告警、指標 | monitoring, datadog, sla, slo, alert |
| **AI / 自動化** | AI 呼叫、自動化腳本、排程 | ai, automation, schedule, claude-api |
| **其他** | 不屬於上述類型 | — |

**若某分類下只有 1 個 skill，考慮是否併入相近分類或保留等待成長。**
**若新增 skill 後某分類超過 5 個，考慮是否需要細分子類。**

重新歸類後，若發現現有 skill 應調整分類，一併在 README 中更新。

**5-4. 更新 README 索引表**

README 索引採用**分類區塊 + 表格**格式，每個分類一個表格：

```markdown
## Skill 索引

### <分類名稱>

| Skill | 說明 |
|---|---|
| [skill-name](skill-name/) | <從 description 提取的一句話說明，30 字以內> |
```

規則：
- Skill 名稱以**超連結**形式指向對應資料夾（相對路徑）
- 說明從該 skill 的 `description` 欄位提取，保持簡潔
- 分類按使用頻率或重要性排序（常用的放前面）
- 完整替換 README 的索引區塊，不要只追加

---

### Step 6：給出簡易報告

README 更新完成後，輸出以下報告：

```
Skill 建立報告
─────────────────────────────
操作：<新建 / 更新>
名稱：<skill-name>
檔案：<檔案路徑>

重複性掃描結果：
  - 掃描數量：<N> 個現有 skill
  - 相似 skill：<無 / skill-name（相似度 ~X%）>
  - 處理方式：<新建 / 迭代更新>

分類審視結果：
  - 本次歸類：<分類名稱>
  - 分類調整：<無變動 / 將 skill-name 從 A 移至 B>
  - 目前共 <N> 個分類，<M> 個 skill

Skill 重點：
  - 觸發語句：<範例>
  - 工作步驟：<N> 步
  - 主要產出：<說明>

下一步建議：
  1. 用 /create-pr 將此 skill 提交到 main 分支
  2. 若需要安裝到本機，將資料夾複製至 ~/.claude/skills/<skill-name>/
─────────────────────────────
```

---

## 注意事項

- Skill 名稱使用英文 **kebab-case**，全小寫，不含空格（例如：`weekly-report`）
- `description` 欄位是 Claude 判斷何時呼叫此 skill 的依據，**必須包含觸發語句範例**
- 不要在 SKILL.md 裡面 hardcode 使用者名稱、token 等敏感資訊
- 若更新現有 skill，保留原有功能，以**追加或修改**方式迭代，不要直接覆蓋整個檔案
