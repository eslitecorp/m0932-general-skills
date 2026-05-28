# m0932-general-skills

放置日常工作通用技能。每個 skill 皆為獨立資料夾，內含 `SKILL.md` 供 Claude Code 呼叫。

---

## Skill 索引

### Git 工作流程

| Skill | 說明 |
| --- | --- |
| [create-pr](create-pr/) | 分析 git diff 將變更分類成 atomic commits（繁體中文訊息），並建立 PR 到預設主線分支 |

### Meta / Skill 管理

| Skill | 說明 |
| --- | --- |
| [audit-skill](audit-skill/) | 稽核 repo 中所有 skill 的安全性、格式合規與可追溯性，產出風險報告並同步 README 索引 |

### 報告 / 資料整合

| Skill | 說明 |
| --- | --- |
| [youtrack-report](youtrack-report/) | 連線 YouTrack 自動產生包含上週完成與未完成事項的 Markdown 週報 |

### 監控 / Observability

| Skill | 說明 |
| --- | --- |
| [datadog-setup-monitoring](datadog-setup-monitoring/) | 設定 Datadog 監控，建立 Dashboard、SLA/SLO 指標與告警規則 |
| [seo-query](seo-query/) | 查詢誠品線上 Astro 商品頁 SSR 效能與 SEO 數據，解讀 render time 與異常指標 |
