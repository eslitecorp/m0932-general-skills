---
name: seo-query
description: "查詢 Eslite 誠品線上 Astro 商品頁 SSR 效能與 SEO 數據，包含 render time 統計、cache hit rate、404 異常率、各 bot 流量分析，以及升階條件評估。觸發語句：「SSR 效能如何」、「查昨天 render time」、「升階條件通過了嗎」、「有達標嗎」、「/seo-query」。"
tags: ["report", "ai"]
---

# SEO Query — 商品頁 SSR 效能查詢

查詢 Eslite 誠品線上 Astro 商品頁的 SSR 效能與 SEO 數據，解讀 render time 統計、cache hit rate 與異常率。

---

## 背景知識

SSR 服務只有爬蟲（Googlebot）進入，不影響使用者體驗，但直接影響 SEO 品質（索引速度、爬取預算、排名）。
渲染架構使用 Cloudflare Worker，效能問題方向是優化 API 或 cache 策略，不適用擴容建議。
目前處於 {rollout.phase} 放量階段（GUID 尾兩位 {rollout.guidSuffix}，約 {rollout.trafficPercent}% 流量），cache hit rate 偏低是預期行為。
Astro 目前僅處理 ~{rollout.trafficPercent}% 的 Googlebot 流量，SSR 效能問題對整體 GSC 指標影響有限，分析時不應將 GSC 指標波動直接歸因於 Astro。
ssr_records = 實際打到 Worker 的請求數（cache miss）；cache_hit_ssr = Cloudflare edge 直接回應（未進 Worker）。
render_time_stats 只涵蓋 cache miss 的請求。
商品價格與庫存由 client-side 非同步載入，不在 SSR 範疇。

## 異常判斷規則

**Render Time**
- `render_time_stats.p95_ms` > {rules.p95WarnMs}ms → ⚠️ 警告
- `render_time_stats.p99_ms` > {rules.p99WarnMs}ms → ⚠️ 警告
- `render_time_stats.count_above_5000ms / render_time_stats.total_records` > {rules.above5sAbnormalPct}% → 🚨 異常（5秒以上）
- `render_time_stats.count_above_3000to5000ms / render_time_stats.total_records` > {rules.above3to5sWarnPct}% → ⚠️ 警告（3–5秒）

**Cache Hit Rate**（cache_hits / 總請求數）
- < {rules.cacheHitRateWarnPct}% → ⚠️ 警告（P1 放量階段基準約 7–9%）
- < {rules.cacheHitRateAbnormalPct}% → 🚨 異常

**404 Rate**（404 次數 / SSR miss 總數）
- 目前基準約 {rules.error404BaselinePct}%，SEO 健康目標為 < {rules.error404HealthyPct}%
- > {rules.error404WarnPct}% → ⚠️ 警告（比現況惡化）
- > {rules.error404AbnormalPct}% → 🚨 異常（批次下架或 bug 造成）
- 回答時一律附上「目前值 vs SEO 目標 {rules.error404HealthyPct}%」的落差說明

## 資料路徑

所有 ID 與設定值集中於 `seo-query/seo-query-config.json`，需要時直接修改該檔即可。

檔名格式：`ssr-product-log-YYYYMMDD_analysis.json` / `combined-YYYYMMDD_analysis.json`

---

## GSC Tracking Sheet 欄位說明

Sheet 每列代表一個指標，欄位結構如下：

| 欄 | 內容 |
|----|------|
| A | 指標名稱 |
| B | 月趨勢（vs 4週前） |
| C | 週變化（vs 上週） |
| D | 歷史最大值 |
| E | 歷史最小值 |
| F | （空欄，略過） |
| G | 最新週數據（最近一週） |
| H | 上週數據 |
| I 以後 | 更早各週（依序往前） |

第一列（Row 1）為表頭，G 欄起為日期（格式 M/DD，最新週在最左）。

**重點指標列（A 欄值）：**

*流量*
- `曝光`：全站總曝光次數
- `點擊`：全站總點擊次數
- `手機曝光`、`手機點擊`：行動裝置流量
- `/product曝光`、`/product點擊`：商品頁流量（與 Astro 最直接相關）

*索引覆蓋率*
- `有效 (Coverage)（涵蓋範圍）`：已建立索引的有效頁面數
- `排除 (網頁-未建立索引)`：被排除、未索引的頁面數
- `錯誤 (伺服器錯誤5XX)`：5XX 錯誤頁面數（直接影響索引）
- `重複頁面`：重複頁面數
- `檢索未索引`：已檢索但尚未建立索引（爬取預算耗損）

*Core Web Vitals*
- `手機 快/ 良好`、`手機 中/ 需要改善`、`手機 慢/ 不良`
- `桌機 快/ 良好`、`桌機 中/ 需要改善`、`桌機 慢/ 不良`

*Rich Results*
- `產品摘要`：商品結構化資料曝光數

**讀取方式：** `values[row][0]` 取得指標名稱，`values[row][1]` 月趨勢，`values[row][2]` 週變化，`values[row][6]` 最新週數值。

---

## 工作流程

### Step 0：讀取設定檔

讀取 `seo-query/seo-query-config.json`，取得以下變數供後續步驟使用：
- `SSR_FOLDER_ID` ← `drive.ssrFolderId`
- `COMBINED_FOLDER_ID` ← `drive.combinedFolderId`
- `GSC_SHEET_ID` ← `gscSheet.spreadsheetId`
- `GSC_SHEET_NAME` ← `gscSheet.sheetName`（需 URL encode，空格轉 `%20`；若含特殊字元建議整體 encode）
- `{rollout.phase}`、`{rollout.guidSuffix}`、`{rollout.trafficPercent}`、`{rollout.startDate}` ← 放量階段資訊，用於背景知識說明
- `{rules.p95WarnMs}`、`{rules.p99WarnMs}`、`{rules.above5sAbnormalPct}`、`{rules.above3to5sWarnPct}` ← 異常判斷門檻
- `{rules.p95BaselineMs}`、`{rules.p99BaselineMs}` ← 升階條件用 baseline（升階門檻 = baseline × 1.2）；與達標日判斷無關，達標日依 Worker 請求數與峰值 RPM 判定

### Step 1：決定查詢日期

預設查**昨天**的資料（log 記錄前一天），格式為 `YYYYMMDD`。若使用者明確指定日期則以指定日期為準。

### Step 2：取得 gcloud token 並同時搜尋所有資料來源

```bash
TOKEN=$(gcloud auth print-access-token)
# SSR 資料夾（使用 SSR_FOLDER_ID）
curl -s "https://www.googleapis.com/drive/v3/files?q='${SSR_FOLDER_ID}'+in+parents+and+name+contains+'YYYYMMDD'&fields=files(id,name)" \
  -H "Authorization: Bearer $TOKEN"
# Combined 資料夾（使用 COMBINED_FOLDER_ID）
curl -s "https://www.googleapis.com/drive/v3/files?q='${COMBINED_FOLDER_ID}'+in+parents+and+name+contains+'YYYYMMDD'&fields=files(id,name)" \
  -H "Authorization: Bearer $TOKEN"
# GSC Tracking Sheet（使用 GSC_SHEET_ID 與 GSC_SHEET_NAME）
# 工作表名稱含空格時需用單引號包裹並 URL encode（%27 = 單引號，%20 = 空格）
GSC_RANGE=$(python3 -c "import urllib.parse; print(urllib.parse.quote(\"'${GSC_SHEET_NAME}'\") + '!A1:AZ200')")
curl -s "https://sheets.googleapis.com/v4/spreadsheets/${GSC_SHEET_ID}/values/${GSC_RANGE}" \
  -H "Authorization: Bearer $TOKEN"
```

兩個 Drive 資料夾都需要查：SSR 檔取 render time，Combined 檔取 cache hit rate 和 404 數據。
GSC Tracking Sheet 取全站 GSC 週期指標（曝光、點擊、CTR、排名、覆蓋率等）。

**Combined 檔重要欄位對應：**
- Cache hit rate：`cloudflare_cache_hit.total_ssr_hits`（cache hits）／`data_source_stats.ssr_records`（Worker 請求數）
- 404 總數：`errors_404.total_404_count`
- Worker 請求數：`data_source_stats.ssr_records`

**無法從 JSON 取得的指標（需人工從 Cloudflare Dashboard 確認）：**
- HTTP 5xx rate：JSON 分析檔不含 HTTP status code breakdown
- Astro 200 率：同上；404 為下架商品正常回應，計算時應排除
GSC Tracking Sheet 取全站 GSC 週期指標（曝光、點擊、CTR、排名、覆蓋率等）。

### Step 3：下載 JSON 資料

取得 file ID 後下載內容（SSR 和 Combined 各下載第一筆）：

```bash
curl -s "https://www.googleapis.com/drive/v3/files/FILE_ID?alt=media" \
  -H "Authorization: Bearer $TOKEN"
```

GSC Tracking Sheet 已在 Step 2 直接取得，無需額外下載。

**檔案找不到時：** 若 Drive 搜尋結果為空（`files: []`），直接告知使用者「查無 YYYYMMDD 的資料，pipeline 可能未執行（例如假日）」，不可用其他日期的資料替代或自行推測。

### Step 4：解讀並回答問題

**語言：** 繁體中文，技術術語可保留英文。

**輸出格式：**

1. **SSR 效能** — 用表格呈現，欄位：指標 / 數值 / 狀態
   ```
   | 指標      | 數值     | 狀態 |
   |-----------|---------|------|
   | P95       | 2296ms  | ✅   |
   | P99       | 3024ms  | ⚠️   |
   | 5秒以上率  | 0.10%   | ✅   |
   | 3–5秒率   | 0.94%   | ✅   |
   ```

2. **Cache Hit Rate** — 單行數值 + 狀態符號

3. **404 Rate** — 單行數值 + 狀態符號 + 括號標示與 SEO 目標 {rules.error404HealthyPct}% 的落差

4. **GSC 指標**（若問題涉及）— 條列重點指標的週變化，標注 ✅ / ⚠️ / 🚨

5. **結尾固定格式：**
   ```
   ⚠️ 以上為 AI 建議，請工程師判斷後再行動。
   ```

**注意：** GSC 數據為全站指標，Astro 僅佔 ~{rollout.trafficPercent}% 流量，不應將 GSC 波動直接歸因於 Astro。

---

## 注意事項

- 使用者需以公司 Google 帳號登入 gcloud，否則 token 取得會失敗：`gcloud auth login`
- `gcloud auth print-access-token` 取得的 token 有效期約 1 小時，過期需重新執行
- render_time_stats 只涵蓋 cache miss（進入 Worker）的請求，非全部流量
- cache hit rate 偏低在放量階段為預期行為，不代表異常
- {rollout.startDate} 為本階段（{rollout.phase}）切換日，當天流量為前後兩個階段混合，數據不具代表性，**不計入觀測達標日**

---

## 使用者問題

$ARGUMENTS
