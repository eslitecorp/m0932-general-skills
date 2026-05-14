# Datadog 監控自動設定指南

針對使用 OpenTelemetry 的 APM 服務，提供監控標準化流程。
執行腳本後，自動為指定 API 端點建立 **Monitor + SLO**，並可選擇性更新 Dashboard。

---

## 建立項目一覽

| 項目 | 類型 | 內容 |
|------|------|------|
| Error Rate Monitor | Metric Alert | 錯誤率 > N%，5 分鐘滾動視窗 |
| P99 Latency Monitor | Metric Alert | P99 延遲 > Ns，5 分鐘滾動視窗 |
| Availability SLO | Monitor-based | 錯誤率 Monitor 可用性 99.5% / 30d |
| P99 Latency SLO | Time Slice | P99 < Ns，99% / 30d |
| Request Rate SLO | Time Slice | Request Rate >= X req/s，99% / 30d |

### Dashboard Section 結構（每個 API 6 個 widget，高度 7 行）

```
行 y+0: [標題 note ── 全寬 w=12]
行 y+1: [Avail SLO w=3][P99 SLO w=3][P99 Latency (s) 圖 w=6]
行 y+4: [Error Rate w=6]            [Sum(Requests) w=6]
```

每個 timeseries widget 均包含：
- Anomaly overlay（異常帶，灰色）
- Monitor event overlay（Monitor 觸發時間點）
- Change Tracking overlay（部署事件）

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
   | `monitors_read` / `monitors_write` | 建立 / 修改 Monitor |
   | `slos_read` / `slos_write` | 建立 / 修改 SLO |
   | `dashboards_read` / `dashboards_write` | 修改 Dashboard |
   | `metrics_read` | 查詢 P99 / Request Count 歷史數據 |

### 2. 設定環境變數

```bash
export DD_API_KEY="your_api_key"
export DD_APP_KEY="your_app_key"

# 非 US5 站點需額外設定（預設 us5.datadoghq.com）
# export DD_SITE="datadoghq.com"
```

**推薦：** 寫入 `~/.claude/settings.json`（Claude Code 專用）讓設定持久化：

```json
{
  "env": {
    "DD_API_KEY": "your_api_key",
    "DD_APP_KEY": "your_app_key"
  }
}
```

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

加上 `--auto-threshold`，腳本會自動查詢歷史 P99 數據並建議閾值（p95 of p95 無條件進位）：

```bash
python3 setup_monitoring.py \
  --service    my-api \
  --endpoint   "GET /api/v1/orders" \
  --resource-filter "get_/api/v1/orders*" \
  --auto-threshold \
  --dashboard-id  <dashboard_id>
```

### 動態調整查詢時間範圍

使用 `--threshold-value` 和 `--threshold-unit` 控制查詢歷史數據的範圍（影響 P99 閾值、Sum(Requests) 警示線、Request Rate SLO 閾值）：

```bash
# 過去 2 週（預設為 7 days）
python3 setup_monitoring.py \
  --service    my-api \
  --endpoint   "GET /api/v1/products" \
  --resource-filter "get_/api/v1/products*" \
  --auto-threshold \
  --threshold-value 2 --threshold-unit weeks \
  --dashboard-id  <dashboard_id>

# 過去 1 個月
python3 setup_monitoring.py \
  --service    my-api \
  --endpoint   "POST /api/v1/auth/sign_in" \
  --resource-filter "post_/api/v1/auth/sign_in*" \
  --auto-threshold \
  --threshold-value 1 --threshold-unit months \
  --error-threshold 0.1 \
  --dashboard-id  <dashboard_id>
```

---

## 參數說明

| 參數 | 必填 | 說明 |
|------|------|------|
| `--service` | ✅ | APM service tag 名稱（e.g., `my-api`）|
| `--endpoint` | ✅ | 端點描述，用於命名（e.g., `GET /api/v1/products`）|
| `--resource-filter` | ✅ | APM `resource_name` tag 值，支援 `*` 萬用字元 |
| `--p99-threshold` | ※ | P99 延遲警報閾值（秒）|
| `--auto-threshold` | ※ | 自動查詢歷史 P99 數據並計算閾值，與 `--p99-threshold` 擇一 |
| `--error-threshold` | | 錯誤率警報閾值（%，預設 `0.5`）|
| `--threshold-value` | | 查詢歷史數據的數量（預設 `7`）|
| `--threshold-unit` | | 查詢歷史數據的單位：`days` / `weeks` / `months`（預設 `days`）|
| `--dashboard-id` | | 要新增 section 的 Dashboard ID（可選）|
| `--priority` | | Monitor priority 1~5（預設 `2`）|

---

## 如何找到 `--resource-filter` 的值

`resource_name` tag 是 APM 自動產生的路由名稱，格式為 HTTP Method 小寫 + 路徑，空格以 `_` 替代。

### 查詢方式（Python 腳本，避免 zsh 特殊字元問題）

```python
# 寫成 .py 檔案再執行，不要用 zsh heredoc
import json, urllib.request, urllib.parse, time, os

settings = json.load(open(os.path.expanduser("~/.claude/settings.json")))
DD_API_KEY = settings["env"]["DD_API_KEY"]
DD_APP_KEY = settings["env"]["DD_APP_KEY"]
DD_SITE = settings["env"].get("DD_SITE", "datadoghq.com")
APM_METRIC = "trace.OpenTelemetry_Instrumentation_Rack.server"

now = int(time.time())
frm = now - 86400 * 7

# 替換 my-api 和 *keyword* 為實際服務名稱和關鍵字
q = urllib.parse.quote(f"sum:{APM_METRIC}.hits{{service:my-api,resource_name:*keyword*}} by {{resource_name}}")
url = f"https://api.{DD_SITE}/api/v1/query?from={frm}&to={now}&query={q}"
req = urllib.request.Request(url, headers={"DD-API-KEY": DD_API_KEY, "DD-APPLICATION-KEY": DD_APP_KEY})
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read())
for s in data.get("series", []):
    print(s.get("scope", "").split(",")[0].replace("resource_name:", ""))
```

常見範例：

| 實際路由 | resource_name tag | resource-filter 參數 |
|---------|-------------------|----------------------|
| `GET /api/v1/products/:id` | `get /api/v1/products/123` | `get_/api/v1/products*` |
| `GET /api/v1/cart/items` | `get /api/v1/cart/items?step=1` | `get_/api/v1/cart/items*` |
| `POST /api/v1/orders` | `post /api/v1/orders` | `post_/api/v1/orders*` |
| `POST /api/v1/auth/sign_in` | `post /api/v1/auth/sign_in` | `post_/api/v1/auth/sign_in*` |

> **注意：** Datadog tag 中空格以 `_` 表示，所以 `get /api/...` → filter 寫 `get_/api/...`

---

## 閾值設定建議

### P99 閾值

建議先用 `--auto-threshold` 查看實際數據，再手動調整：

一般原則：
- 閾值 = p95 of p99（過去 N 天），無條件進位到整數
- Cart/checkout 類：允許較高延遲（通常 10~15s）
- 查詢類：要求較嚴格（通常 1~5s）

### 錯誤率閾值

| API 類型 | 建議 critical | 建議 warning |
|---------|--------------|-------------|
| 登入 / 結帳 | 0.1% | 0.05% |
| 商品查詢 | 0.5% | 0.2% |
| 一般 CRUD | 1.0% | 0.5% |

### Sum(Requests) 警示線

- 自動計算：p99 of request_count/min（過去 N 天），無條件進位
- 意義：超過此值表示流量異常偏高（超出歷史 99% 正常範圍）

### Request Rate SLO 閾值

- 自動計算：p5 of request_count/min ÷ 60（轉為 req/s），無條件捨去
- 意義：低於此值表示服務流量異常偏低（可能是服務中斷）

---

## APM Metric 說明

腳本使用的底層 metric：

```text
trace.OpenTelemetry_Instrumentation_Rack.server
```

由 `opentelemetry-instrumentation-rack` gem 產生，為 distribution 類型，unit: seconds。

可用 aggregator：`avg`, `p50`, `p75`, `p90`, `p95`, `p99`

---

## 每次執行後的輸出範例

腳本執行完成後會輸出建立的項目摘要：

```
📋 Step 5 / 5  完成！建立項目摘要：

==================================================
   Monitor (Error Rate) : 12345678
   Monitor (P99 Latency): 12345679
   Monitor (Req Count)  : 12345680
   Anomaly (Error Rate) : 12345681
   Anomaly (P99 Latency): 12345682
   Anomaly (Req Rate)   : 12345683
   SLO (Availability)   : abcd1234...
   SLO (P99 Latency)    : abcd5678...
   SLO (Request Rate)   : abcd9012...
   Dashboard            : https://us5.datadoghq.com/dashboard/xxx-yyy-zzz
```
