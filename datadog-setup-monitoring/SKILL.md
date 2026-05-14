---
name: datadog-setup-monitoring
description: "為指定 API 端點建立 Datadog 完整監控：3 個 Anomaly Monitor（Error Rate + P99 Latency + Request Rate）+ 2 個 Anomaly SLO（Availability + P99 Latency，Monitor-based）+ Dashboard section（含 anomaly overlay + event overlay）。觸發語句範例：「幫我把 GET /api/v3/orders 建立基本監控和 SLO」、「/datadog-setup-monitoring」。"
argument-hint: "[service] [\"METHOD /path\"] [resource-filter] [p99-threshold or auto]"
allowed-tools: Bash
disable-model-invocation: false
---

# Datadog 監控自動設定

腳本路徑：`${CLAUDE_SKILL_DIR}/setup_monitoring.py`

## 執行流程

### Step 1：收集必要參數

若 `$ARGUMENTS` 不完整，向使用者詢問以下資訊：

1. **service**：APM service tag 名稱（例如 `my-api`）
2. **endpoint**：端點描述，例如 `GET /api/v3/orders`
3. **resource-filter**：APM `resource_name` tag 過濾值，例如 `get_/api/v3/orders*`
   - 規則：HTTP method 小寫 + `_` + path，支援 `*` 萬用字元
   - 若使用者不確定，使用 Metrics API 查詢（見下方「查詢 resource_name」）
4. **p99-threshold**：P99 延遲閾值（秒）。若使用者回答「不知道」或「自動」，使用 `--auto-threshold` 自動計算
5. **error-threshold**：錯誤率閾值（%，預設 `0.5`；登入 / 結帳類建議 `0.1`）
6. **dashboard-id**（可選）：要新增 section 的 Dashboard ID。若使用者提供 Dashboard 名稱但不知道 ID，使用「查詢 Dashboard ID」腳本列出所有 Dashboard 後確認

### Step 1.5：查詢 resource_name（若使用者不確定）

使用 Metrics API 查詢實際 resource_name，避免 zsh 特殊字元問題，**必須寫成 Python 腳本檔案再執行**：

```python
# 寫入暫存腳本 /tmp/query_rn.py
import json, urllib.request, urllib.parse, time

DD_API_KEY = "..."  # 從 ~/.claude/settings.json 讀取
DD_APP_KEY = "..."
DD_SITE = "us5.datadoghq.com"
APM_METRIC = "trace.OpenTelemetry_Instrumentation_Rack.server"

now = int(time.time())
frm = now - 86400 * 7  # 過去 7 天

def query(q):
    params = urllib.parse.urlencode({"from": frm, "to": now, "query": q})
    url = f"https://api.{DD_SITE}/api/v1/query?{params}"
    req = urllib.request.Request(url, headers={"DD-API-KEY": DD_API_KEY, "DD-APPLICATION-KEY": DD_APP_KEY})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

# 用關鍵字過濾，例如 *login*（替換為實際關鍵字）
result = query(f"sum:{APM_METRIC}.hits{{service:my-api,resource_name:*login*}} by {{resource_name}}")
for s in result.get("series", []):
    rn = s.get("scope", "").split(",")[0].replace("resource_name:", "")
    print(rn)
```

執行：`python3 /tmp/query_rn.py`

> ⚠️ **注意**：不要用 zsh heredoc（`<<'EOF'`）執行含 `@`、`*`、`{}` 的 Python 程式碼，會被 zsh 展開導致錯誤。請寫成 `.py` 檔案再執行。

### Step 2：確認參數

整理收集到的參數，向使用者確認後再執行：

```
即將建立監控：
  服務：{service}
  端點：{endpoint}
  resource-filter：{resource-filter}
  錯誤率閾值：{error-threshold}%（Anomaly Monitor 無固定 P99 閾值，動態偵測）
  Dashboard：{dashboard-id 或「不加入」}
```

### Step 3：執行腳本

確認後，使用 Bash 工具執行：

**有指定 p99-threshold 時：**
```bash
DD_API_KEY="..." DD_APP_KEY="..." python3 "${CLAUDE_SKILL_DIR}/setup_monitoring.py" \
  --service "{service}" \
  --endpoint "{endpoint}" \
  --resource-filter "{resource-filter}" \
  --p99-threshold {p99-threshold} \
  --error-threshold {error-threshold} \
  [--threshold-value 7] [--threshold-unit days] \
  [--dashboard-id {dashboard-id}]
```

**使用自動閾值時：**
```bash
DD_API_KEY="..." DD_APP_KEY="..." python3 "${CLAUDE_SKILL_DIR}/setup_monitoring.py" \
  --service "{service}" \
  --endpoint "{endpoint}" \
  --resource-filter "{resource-filter}" \
  --auto-threshold \
  --error-threshold {error-threshold} \
  [--threshold-value 7] [--threshold-unit days] \
  [--dashboard-id {dashboard-id}]
```

**`--threshold-value` / `--threshold-unit` 說明：**
- 控制查詢歷史數據的時間範圍，用於計算 P99 閾值、Sum(Requests) 警示線、Request Rate SLO 閾值
- 預設：`--threshold-value 7 --threshold-unit days`（過去 7 天）
- 範例：`--threshold-value 2 --threshold-unit weeks`（過去 2 週）
- 範例：`--threshold-value 1 --threshold-unit months`（過去 1 個月）
- 單位選項：`days`、`weeks`（×7天）、`months`（×30天）

> ⚠️ **重要**：若環境變數未在 shell 中設定，必須在指令前明確傳入 `DD_API_KEY="..." DD_APP_KEY="..."`，或從 `~/.claude/settings.json` 讀取後傳入。

### Step 4：顯示結果

執行完成後，整理輸出，以繁體中文顯示：
- 建立的 Monitor 名稱與 ID
- 建立的 SLO 名稱與 ID
  - Availability SLO：`{端點簡稱} Availability SLO (Anomaly, 99% / 30d)`
  - P99 Latency SLO：`{端點簡稱} P99 Latency SLO (Anomaly, 99% / 30d)`
  - Request Rate SLO：`{端點簡稱} Request Rate SLO (99% / 30d)`
- Dashboard 連結（若有更新）
- 若有錯誤，說明原因與解決方式

---

## 參數解析規則（若 $ARGUMENTS 有值）

`$ARGUMENTS` 格式：`[service] ["METHOD /path"] [resource-filter] [p99s 或 auto]`

範例：
- `/datadog-setup-monitoring` → 引導式對話收集全部參數
- `/datadog-setup-monitoring "GET /api/v3/orders"` → 詢問其餘參數
- `/datadog-setup-monitoring my-api "GET /api/v3/orders" "get_/api/v3/orders*" auto` → 自動計算閾值並執行

---

## 常見 resource-filter 範例

| endpoint | resource-filter |
|----------|----------------|
| `GET /api/v1/products/:id` | `get_/api/v1/products*` |
| `GET /api/v1/cart/items` | `get_/api/v1/cart/items*` |
| `POST /api/v1/orders` | `post_/api/v1/orders*` |
| `POST /api/v1/auth/sign_in` | `post_/api/v1/auth/sign_in*` |
| `GET /api/v1/orders/:id/details` | `get_/api/v1/orders*details*` |

---

## 前置條件確認

執行前確認以下環境變數已設定：

```bash
python3 - <<'PYEOF'
import json, os
s = json.load(open(os.path.expanduser("~/.claude/settings.json")))
env = s.get("env", {})
print("DD_API_KEY:", "✅ 已設定" if env.get("DD_API_KEY") else "❌ 未設定")
print("DD_APP_KEY:", "✅ 已設定" if env.get("DD_APP_KEY") else "❌ 未設定")
PYEOF
```

若未設定，提示：
> 請在 `~/.claude/settings.json` 的 `env` 區塊設定 `DD_API_KEY` 與 `DD_APP_KEY`

### APP Key 必要 Scopes

| Scope | 用途 |
|-------|------|
| `monitors_read` / `monitors_write` | 建立 Monitor |
| `slos_read` / `slos_write` | 建立 SLO |
| `dashboards_read` / `dashboards_write` | 更新 Dashboard |
| `metrics_read` | 查詢 P99 / Request Count 歷史數據 |

---

## Dashboard Template Variables

Dashboard 使用 `$env` template variable 過濾環境，所有 timeseries widget query 均包含此 filter：

```python
# query 格式
f"p99:{APM_METRIC}{{service:{service},$env,resource_name:{resource_filter}}}"
```

| 設定 | 值 |
|------|---|
| 變數名稱 | `env` |
| prefix | `env` |
| 可選值 | `prod`, `stg`, `uat` |
| 預設值 | `prod` |

**用途**：
- 切換環境查看不同 env 的數據
- 排除特定環境的流量（例如 prod 環境排除 VPN/proxy 流量）
- `add_dashboard_section()` 會自動保留現有 Dashboard 的 `template_variables` 設定

---

## Dashboard Section 結構

每個 API section 包含 **6 個 widget**，高度 7 行：

```
行 y+0: [標題 note ── 全寬 w=12]
行 y+1: [Avail SLO (Anomaly) w=3][P99 SLO (Anomaly) w=3][P99 Latency (s) 圖 w=6]
行 y+4: [Error Rate w=6]            [Sum(Requests) w=6]
```

### Widget 詳細規格

| Widget | 內容 | 說明 |
|--------|------|------|
| P99 Latency | request 1: `p99:` line（**orange**）<br>request 2: `anomalies(p99:..., 'robust', 2)` line（gray, solid, thin） | robust 演算法適合穩定週期性指標 |
| Error Rate | request 1: `errors/total*100` bars（**red**）<br>request 2: `anomalies(errors/total*100, 'agile', 2)` line（gray, solid, thin） | agile 演算法適合快速變化指標 |
| Sum(Requests) | request 1: `as_count()` line（**blue**）<br>request 2: `as_rate()` + formula `anomalies(query1, 'agile', 2)` line（gray, solid, thin）<br>request 3/4: overlay（marker） | 累計量 + 異常帶 + 警示線 |

### Event Overlay（所有 timeseries widget 均有）

```python
EVENT_OVERLAY = [
    {"q": "sources:monitor status:error,warning", "tags_execution": "and"},
    {"q": "sources:change_tracking service:{service} env:prod", "tags_execution": "and"},
]
```

- **Monitor overlay**：在時間軸上標記 Monitor 觸發（error/warning）的時間點，方便對照異常
- **Change Tracking overlay**：標記部署事件，方便判斷異常是否由部署引起

### 閾值計算邏輯

| 項目 | 計算方式 |
|------|---------|
| P99 Latency 閾值 | p95 of p95（查詢 `p95:` metric，過去 N 天），無條件進位到整數 |
| Sum(Requests) 警示線 | p99 of (as_count ÷ rollup_interval_min)，換算為 req/min，無條件進位 |
| Request Rate SLO 閾值 | p5 of req/min ÷ 60（轉為 req/s），無條件捨去 |

> **注意**：Datadog metrics API 的 `as_count()` 每個點是 rollup 區間的累計值（查詢 7 天時 interval≈3600s=60min）。
> 必須除以 `interval_min` 才能換算成 req/min，否則 marker 會偏高 60 倍。

## 錯誤率計算方式

**Error Rate = (HTTP status code ≠ 200 的 hits) / (所有 hits) × 100**

> ⚠️ **重要**：此服務的 APM trace tag 名稱為 `http.status_code`（有點號），
> 不是 `http_status_code`（底線）。使用錯誤的 tag 名稱會導致過濾無效，Error Rate 顯示 100%。

```
分子: sum:trace...hits{service:X, resource_name:Y, !http.status_code:200}.as_rate()
分母: sum:trace...hits{service:X, resource_name:Y}.as_rate()
```

Monitor query 格式：
```
sum(last_5m):(分子 / 分母) * 100 > threshold
```

Dashboard widget 使用 `formulas` 計算 `errors / total * 100`，顯示為百分比。

確認 tag 名稱的方法：
```python
# 查詢 by {http.status_code} 確認 tag 是否存在
sum:trace...hits{service:X} by {http.status_code}.as_rate()
```

## Anomaly Detection

### 演算法比較

| 演算法 | 原理 | 特性 |
|--------|------|------|
| `basic` | 簡單滾動百分位數 | 反應快、無季節性感知 |
| `agile` | Robust SARIMA | 快速適應 level shift、有季節性感知、對數值尺度敏感 |
| `robust` | 季節性趨勢分解（STL） | 穩定、對短暫異常有抵抗力、對數值尺度不敏感 |

### 各指標演算法選擇

| 指標 | 演算法 | 方向 | 選擇理由 |
|------|--------|------|---------|
| P99 Latency | `robust` | `above` | P99 延遲天然存在短暫尖峰（GC、cold start），robust 對短暫異常有抵抗力，避免誤報 |
| Error Rate | `agile` | `above` | 錯誤率在部署後可能發生 level shift，agile 能快速適應新基準，只在異常偏高時告警 |
| Request Rate | `agile` | `both` | 流量異常偏高（DDoS/bot）和異常偏低（服務中斷）都需要告警，agile 適應 level shift |

### Dashboard Anomaly Overlay

每個 timeseries widget 均包含 anomaly overlay，讓異常時間區間一眼可見。Anomaly band 顯示「預期正常範圍」，超出範圍的區間會以灰色帶標示。

### Anomaly Monitor（獨立 Monitor）

除了 Dashboard 視覺化，還建立了 3 個 Anomaly Monitor：

| Monitor | 演算法 | 觸發方向 |
|---------|--------|---------|
| Error Rate Anomaly | `agile` | `above`（只在異常偏高時告警） |
| P99 Latency Anomaly | `robust` | `above`（只在延遲異常偏高時告警） |
| Request Rate Anomaly | `agile` | `both`（流量異常偏高或偏低都告警） |

Monitor query 格式：

```
avg(last_30m):anomalies(metric{...}, 'algorithm', 2, direction='above', alert_window='last_30m', interval=60, count_default_zero='true') >= 1
```

## 查詢 Dashboard ID

若使用者提供 Dashboard 名稱（如 `M0932-BE-Board`）但不知道 ID，可用以下腳本查詢：

```python
# 寫入 /tmp/list_dashboards.py
import json, urllib.request
DD_API_KEY = "..."
DD_APP_KEY = "..."
DD_SITE = "us5.datadoghq.com"

url = f"https://api.{DD_SITE}/api/v1/dashboard"
req = urllib.request.Request(url, method="GET")
req.add_header("DD-API-KEY", DD_API_KEY)
req.add_header("DD-APPLICATION-KEY", DD_APP_KEY)
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read())
for d in data.get("dashboards", []):
    print(f'{d["id"]:30s} {d["title"]}')
```

執行後找到對應名稱的 ID，傳入 `--dashboard-id`。
