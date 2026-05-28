---
name: seo-query
description: "查詢 Eslite 誠品線上 Astro 商品頁 SSR 效能與 SEO 數據，包含 render time 統計、cache hit rate、404 異常率，以及各 bot 流量分析。觸發語句：「SSR 效能如何」、「查昨天 render time」、「/seo-query」。"
tags: ["report", "ai"]
---

# SEO Query — 商品頁 SSR 效能查詢

查詢 Eslite 誠品線上 Astro 商品頁的 SSR 效能與 SEO 數據，解讀 render time 統計、cache hit rate 與異常率。

---

## 背景知識

SSR 服務只有爬蟲（Googlebot）進入，不影響使用者體驗，但直接影響 SEO 品質（索引速度、爬取預算、排名）。
渲染架構使用 Cloudflare Worker，效能問題方向是優化 API 或 cache 策略，不適用擴容建議。
目前處於放量階段，cache hit rate 偏低是預期行為。
ssr_records = 實際打到 Worker 的請求數（cache miss）；cache_hit_ssr = Cloudflare edge 直接回應（未進 Worker）。
render_time_stats 只涵蓋 cache miss 的請求。
商品價格與庫存由 client-side 非同步載入，不在 SSR 範疇。

## 異常判斷規則

- `render_time_stats.p95_ms` > 3000ms → ⚠️ 警告
- `render_time_stats.p99_ms` > 5000ms → ⚠️ 警告
- `render_time_stats.count_above_5000ms / render_time_stats.total_records` > 1% → 🚨 異常（5秒以上）
- `render_time_stats.count_above_3000to5000ms / render_time_stats.total_records` > 3% → ⚠️ 警告（3–5秒）

## 資料路徑

資料存放於 Google Drive 固定資料夾（不隨月份變動）：
- SSR folder ID：`1iXSr0Oc4lEJnSScPMSplI2z9bUNyGpVR`
- Combined folder ID：`1w089WQQpTFmkRtLN6jwPE7nFpUzhL2Pi`

檔名格式：`ssr-product-log-YYYYMMDD_analysis.json` / `combined-YYYYMMDD_analysis.json`

---

## 工作流程

### Step 1：決定查詢日期

預設查**昨天**的資料（log 記錄前一天），格式為 `YYYYMMDD`。若使用者明確指定日期則以指定日期為準。

### Step 2：取得 gcloud token 並同時搜尋兩個資料夾

```bash
TOKEN=$(gcloud auth print-access-token)
# SSR 資料夾
curl -s "https://www.googleapis.com/drive/v3/files?q='1iXSr0Oc4lEJnSScPMSplI2z9bUNyGpVR'+in+parents+and+name+contains+'YYYYMMDD'&fields=files(id,name)" \
  -H "Authorization: Bearer $TOKEN"
# Combined 資料夾
curl -s "https://www.googleapis.com/drive/v3/files?q='1w089WQQpTFmkRtLN6jwPE7nFpUzhL2Pi'+in+parents+and+name+contains+'YYYYMMDD'&fields=files(id,name)" \
  -H "Authorization: Bearer $TOKEN"
```

兩個資料夾都需要查：SSR 檔取 render time，Combined 檔取 cache hit rate 和 404 數據。

### Step 3：下載 JSON 資料

取得 file ID 後下載內容（SSR 和 Combined 各下載第一筆）：

```bash
curl -s "https://www.googleapis.com/drive/v3/files/FILE_ID?alt=media" \
  -H "Authorization: Bearer $TOKEN"
```

### Step 4：解讀並回答問題

根據數據與異常判斷規則回答使用者問題：
- 使用繁體中文，技術術語可保留英文
- 回答結尾加上：⚠️ 以上為 AI 建議，請工程師判斷後再行動。

---

## 注意事項

- 使用者需以公司 Google 帳號登入 gcloud，否則 token 取得會失敗：`gcloud auth login`
- `gcloud auth print-access-token` 取得的 token 有效期約 1 小時，過期需重新執行
- render_time_stats 只涵蓋 cache miss（進入 Worker）的請求，非全部流量
- cache hit rate 偏低在放量階段為預期行為，不代表異常

---

## 使用者問題

$ARGUMENTS
