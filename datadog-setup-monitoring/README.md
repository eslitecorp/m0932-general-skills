# Datadog 監控自動設定指南

針對 `athena-api`（Rails + OpenTelemetry）的 APM 監控標準化流程。
執行腳本後，自動為指定 API 端點建立 **2 個 Monitor + 2 個 SLO**，並可選擇性更新 Dashboard。

---

## 建立項目一覽

| 項目 | 類型 | 內容 |
|------|------|------|
| Error Rate Monitor | Metric Alert | 錯誤率 > N%，5 分鐘滾動視窗 |
| P99 Latency Monitor | Metric Alert | P99 延遲 > Ns，5 分鐘滾動視窗 |
| Availability SLO | Monitor-based | 錯誤率 Monitor 可用性 99.5% / 30d |
| P99 Latency SLO | Time Slice | P99 < Ns，99% / 30d（使用真實 metric 歷史數據）|

---

## 前置作業

### 1. 建立 Datadog API Key 與 Application Key

1. 前往 **Organization Settings → API Keys**
   - 建立或複製現有的 API Key

2. 前往 **Organization Settings → Application Keys**
   - 點「New Key」，名稱建議：`automation-monitoring`
   - 勾選以下 Scopes：

   | Scope | 用途 |
   |-------|------|
   | `monitors_write` | 建立 / 修改 Monitor |
   | `slos_write` | 建立 / 修改 SLO |
   | `dashboards_write` | 修改 Dashboard |
   | `metrics_read` | 查詢 P99 歷史數據（--auto-threshold 需要）|

### 2. 設定環境變數

```bash
export DD_API_KEY="your_api_key"
export DD_APP_KEY="your_app_key"

# 非 US5 站點需額外設定（預設 us5.datadoghq.com）
# export DD_SITE="datadoghq.com"
```

**推薦：** 寫入 `~/.zshrc` 或 `~/.claude/settings.json`（Claude Code 專用）讓設定持久化。

---

## 使用方式

### 基本用法

```bash
python3 setup_monitoring.py \
  --service    <service_name> \
  --endpoint   "<METHOD /path>" \
  --resource-filter "<resource_name_tag_pattern>" \
  --p99-threshold <秒> \
  --dashboard-id  <dashboard_id>
```

### 自動計算 P99 閾值

加上 `--auto-threshold`，腳本會自動查詢過去 7 天 P99 數據並建議閾值（p95 of p99 無條件進位）：

```bash
python3 setup_monitoring.py \
  --service    athena-api \
  --endpoint   "GET /api/v3/orders" \
  --resource-filter "get_/api/v3/orders*" \
  --auto-threshold \
  --dashboard-id  uh3-7r2-uzx
```

---

## 參數說明

| 參數 | 必填 | 說明 |
|------|------|------|
| `--service` | ✅ | APM service tag 名稱（e.g., `athena-api`）|
| `--endpoint` | ✅ | 端點描述，用於命名（e.g., `GET /api/v4/products`）|
| `--resource-filter` | ✅ | APM `resource_name` tag 值，支援 `*` 萬用字元 |
| `--p99-threshold` | ※ | P99 延遲警報閾值（秒）|
| `--auto-threshold` | ※ | 自動查詢 P99 數據並計算閾值，與 `--p99-threshold` 擇一 |
| `--error-threshold` | | 錯誤率警報閾值（%，預設 `0.5`）|
| `--dashboard-id` | | 要新增 section 的 Dashboard ID（可選）|
| `--priority` | | Monitor priority 1~5（預設 `2`）|

---

## 如何找到 `--resource-filter` 的值

`resource_name` tag 是 APM 自動產生的路由名稱，格式為 HTTP Method 小寫 + 路徑，空格以 `_` 替代。

查詢方式（Datadog MCP 或 UI）：

```text
搜尋 spans: service:athena-api 過去 15 分鐘
觀察 resource_name tag 的實際值
```

常見範例：

| 實際路由 | resource_name tag | resource-filter 參數 |
|---------|-------------------|----------------------|
| `GET /api/v4/products/:id` | `get /api/v4/products/123/...` | `get_/api/v4/products*` |
| `GET /api/v2/cart/items` | `get /api/v2/cart/items?step=1` | `get_/api/v2/cart/items*` |
| `POST /api/v1/orders` | `post /api/v1/orders` | `post_/api/v1/orders*` |

> **注意：** Datadog tag 中空格以 `_` 表示，所以 `get /api/...` → filter 寫 `get_/api/...`

---

## 實際範例

### athena-api 現有設定

```bash
# GET /api/v4/products（P99 實測 avg 1.5s，閾值 4s）
python3 setup_monitoring.py \
  --service athena-api \
  --endpoint "GET /api/v4/products" \
  --resource-filter "get_/api/v4/products*" \
  --p99-threshold 4 \
  --error-threshold 0.5 \
  --dashboard-id uh3-7r2-uzx

# GET /api/v2/cart/items（P99 實測 avg 6.4s，閾值 12s）
python3 setup_monitoring.py \
  --service athena-api \
  --endpoint "GET /api/v2/cart/items" \
  --resource-filter "get_/api/v2/cart/items*" \
  --p99-threshold 12 \
  --error-threshold 0.1 \
  --priority 1 \
  --dashboard-id uh3-7r2-uzx
```

---

## 閾值設定建議

### P99 閾值

建議先用 `--auto-threshold` 查看實際數據，再手動調整：

```bash
# 先查數據（不設定，只看建議值）
python3 setup_monitoring.py \
  --service athena-api \
  --endpoint "GET /api/v3/orders" \
  --resource-filter "get_/api/v3/orders*" \
  --auto-threshold
# 不加 --dashboard-id，乾跑查閾值，再用具體值正式建立
```

一般原則：

- 閾值 = max(avg × 2, p95_of_p99) 無條件進位
- Cart/checkout 類：允許較高延遲（通常 10~15s）
- 查詢類：要求較嚴格（通常 1~5s）

### 錯誤率閾值

| API 類型 | 建議 critical | 建議 warning |
|---------|--------------|-------------|
| 購物車 / 結帳 | 0.1% | 0.05% |
| 商品查詢 | 0.5% | 0.2% |
| 一般 CRUD | 1.0% | 0.5% |

---

## APM Metric 說明

腳本使用的底層 metric：

```text
trace.OpenTelemetry_Instrumentation_Rack.server
```

由 `opentelemetry-instrumentation-rack` gem 產生，為 distribution 類型，unit: seconds。

可用 aggregator：`avg`, `p50`, `p75`, `p90`, `p95`, `p99`

---

## Datadog MCP 設定（AI 工具整合）

透過 MCP 讓 AI 工具直接查詢 Datadog 數據（dashboards、metrics、spans、monitors 等）。

### Claude Code

**Step 1：建立 `.mcp.json`** 於專案根目錄（e.g., `~/Work/.mcp.json`）：

```json
{
  "mcpServers": {
    "datadog": {
      "type": "http",
      "url": "https://mcp.us5.datadoghq.com/api/unstable/mcp-server/mcp?toolsets=core,apm"
    }
  }
}
```

**Step 2：重啟 Claude Code**（關閉後重新開啟）。

**Step 3：在 Claude Code 中執行 `/mcp`**，找到 `datadog` server，點擊「Approve」允許連線。

**Step 4：OAuth 登入**。核准後會自動觸發 Datadog OAuth，瀏覽器開啟登入視窗，以 Datadog 帳號授權後即完成連線。

> 注意：`toolsets` 參數決定可用工具範圍。`core,apm` 為唯讀，
> 可選值：`core`、`apm`、`alerting`、`cases`、`dbm`。

### Roo Code

**Step 1：建立 `.roo/mcp.json`** 於專案根目錄：

```json
{
  "mcpServers": {
    "datadog": {
      "type": "streamable-http",
      "url": "https://mcp.us5.datadoghq.com/api/unstable/mcp-server/mcp?toolsets=core,apm"
    }
  }
}
```

**Step 2：重啟 VS Code** 或在 Roo Code 側邊欄點擊右上角 Server icon → Refresh。

**Step 3：OAuth 登入**。Roo Code 偵測到設定後自動觸發瀏覽器 OAuth 視窗，以 Datadog 帳號授權後即完成連線。

> 也可透過 UI 設定全域：Roo Code 側邊欄 → Server icon → **Edit Global MCP**，
> 貼入相同 JSON 內容（適用於所有專案）。

### GitHub Copilot（Agent Mode）

**Step 1：建立 `.vscode/mcp.json`** 於專案根目錄：

```json
{
  "servers": {
    "datadog": {
      "type": "http",
      "url": "https://mcp.us5.datadoghq.com/api/unstable/mcp-server/mcp?toolsets=core,apm"
    }
  }
}
```

**Step 2：重啟 VS Code**，Copilot 會自動偵測並載入 MCP server。

**Step 3：開啟 Copilot Chat**，切換到 **Agent mode**（對話框左下角下拉選單）。

**Step 4：OAuth 登入**。首次使用 datadog 工具時自動觸發瀏覽器 OAuth 視窗，授權後即完成連線。

> 也可透過 Command Palette（`Cmd+Shift+P`）執行 **MCP: Open User Configuration**
> 設定全域版本（適用於所有專案）。

### MCP Endpoint 說明

| 欄位 | 值 |
|------|-----|
| Site | US5 (`us5.datadoghq.com`) |
| Endpoint | `https://mcp.us5.datadoghq.com/api/unstable/mcp-server/mcp` |
| Toolsets | `core,apm`（可加 `alerting`, `cases`, `dbm`）|
| 認證方式 | OAuth 2.1（瀏覽器登入，自動取得 token）|

---

## 現有監控（athena-api）

| 名稱 | 類型 | ID |
|------|------|-----|
| GET /api/v4/products Error Rate > 0.5% | Monitor | `18798372` |
| GET /api/v4/products P99 Latency > 4s | Monitor | `18798386` |
| GET /api/v2/cart/items Error Rate > 0.1% | Monitor | `18798389` |
| GET /api/v2/cart/items P99 Latency > 12s | Monitor | `18798392` |
| GET /api/v4/products Availability SLO | SLO (monitor) | `573864bc...` |
| GET /api/v4/products P99 Latency SLO | SLO (time_slice) | `7c44bacf...` |
| GET /api/v2/cart/items Availability SLO | SLO (monitor) | `9d4c5d23...` |
| GET /api/v2/cart/items P99 Latency SLO | SLO (time_slice) | `69fc15c1...` |
| Dashboard: M0932-BE-Board | Dashboard | `uh3-7r2-uzx` |
