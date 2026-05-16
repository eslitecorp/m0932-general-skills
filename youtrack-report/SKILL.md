---
name: youtrack-report
tags: ["report", "project-management"]
description: "連線 YouTrack 自動產生包含上週完成與未完成事項的 Markdown 週報。觸發語句：「幫我產週報」、「產生 YouTrack 週報」、「youtrack report」、「/youtrack-report」。"
---

# YouTrack 週報產生器

連線 YouTrack API，自動查詢個人議題並產生 Markdown 格式週報，
包含「上週完成事項」與「尚未完成事項」兩個區塊。

---

## 工作流程

### Step 1：確認設定檔

檢查 `youtrack-report/.env` 是否存在且已填入正確值：

```bash
cat youtrack-report/.env
```

若檔案不存在，提示使用者複製範本並填入：

```bash
cp youtrack-report/.env.template youtrack-report/.env
```

需填入：
- `YOUTRACK_URL`：YouTrack 實例網址（例如 `https://your-org.youtrack.cloud`）
- `YOUTRACK_TOKEN`：個人 API Token（YouTrack → Profile → Authentication → New Permanent Token）

---

### Step 2：執行週報腳本

```bash
bash youtrack-report/run.sh
```

> `run.sh` 會自動建立 venv、安裝相依套件，並以 venv 絕對路徑執行，
> 繞過 GVM 等 shell hook 對 `python` 指令的攔截。

腳本會查詢以下兩組議題：
- **上週完成**：`for: me State: Done resolved date: {last week}`
- **尚未完成**：`for: me State: -Done -{Won't fix}`

---

### Step 3：輸出週報

腳本執行完成後，將 stdout 的 Markdown 內容輸出給使用者。

格式範例：

```
### 週報 (2025-03-25)

### 上週完成事項

優先序 預估時間(實際時間) 事項名稱
Medium 2h (1h 30m) [PROJ-123 修正登入頁面錯誤](https://your-org.youtrack.cloud/issue/PROJ-123)

### 尚未完成事項

優先序 預估時間(實際時間) 事項名稱
High 4h [PROJ-124 新增使用者管理功能](https://your-org.youtrack.cloud/issue/PROJ-124)
```

---

## 注意事項

- `.env` 含敏感資訊，已加入 `.gitignore`，請勿提交至版控
- API Token 請使用 YouTrack Permanent Token，不要使用帳號密碼
- 若 API 回傳 401，表示 token 已過期或權限不足
