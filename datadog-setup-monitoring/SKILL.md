---
name: datadog-setup-monitoring
description: "為指定 API 端點建立 Datadog 基本監控：2 個 Monitor（錯誤率 + P99 延遲）+ 2 個 SLO（可用性 + P99 延遲），並可選擇性更新指定 Dashboard。觸發語句範例：「幫我把 GET /api/v3/orders 建立基本監控和 SLO」、「/datadog-setup-monitoring」。"
argument-hint: "[service] [\"METHOD /path\"] [resource-filter] [p99-threshold or auto]"
allowed-tools: Bash
disable-model-invocation: false
---

# Datadog 監控自動設定

腳本路徑：`${CLAUDE_SKILL_DIR}/setup_monitoring.py`

## 執行流程

### Step 1：收集必要參數

若 `$ARGUMENTS` 不完整，向使用者詢問以下資訊：

1. **service**：APM service tag 名稱（預設：`athena-api`）
2. **endpoint**：端點描述，例如 `GET /api/v3/orders`
3. **resource-filter**：APM `resource_name` tag 過濾值，例如 `get_/api/v3/orders*`
   - 規則：HTTP method 小寫 + `_` + path，支援 `*` 萬用字元
   - 若使用者不確定，說明：「在 Datadog APM → Traces 搜尋此服務，查看 Resource 欄位的實際值」
4. **p99-threshold**：P99 延遲閾值（秒）。若使用者回答「不知道」或「自動」，使用 `--auto-threshold` 自動計算
5. **error-threshold**：錯誤率閾值（%，預設 `0.5`；購物車 / 結帳類建議 `0.1`）
6. **dashboard-id**（可選）：要新增 section 的 Dashboard ID，`athena-api` 預設為 `uh3-7r2-uzx`

### Step 2：確認參數

整理收集到的參數，向使用者確認後再執行：

```
即將建立監控：
  服務：{service}
  端點：{endpoint}
  resource-filter：{resource-filter}
  P99 閾值：{p99-threshold}s（或自動計算）
  錯誤率閾值：{error-threshold}%
  Dashboard：{dashboard-id 或「不加入」}
```

### Step 3：執行腳本

確認後，使用 Bash 工具執行：

**有指定 p99-threshold 時：**
```bash
python3 "${CLAUDE_SKILL_DIR}/setup_monitoring.py" \
  --service "{service}" \
  --endpoint "{endpoint}" \
  --resource-filter "{resource-filter}" \
  --p99-threshold {p99-threshold} \
  --error-threshold {error-threshold} \
  [--dashboard-id {dashboard-id}]
```

**使用自動閾值時：**
```bash
python3 "${CLAUDE_SKILL_DIR}/setup_monitoring.py" \
  --service "{service}" \
  --endpoint "{endpoint}" \
  --resource-filter "{resource-filter}" \
  --auto-threshold \
  --error-threshold {error-threshold} \
  [--dashboard-id {dashboard-id}]
```

### Step 4：顯示結果

執行完成後，整理輸出，以繁體中文顯示：
- 建立的 Monitor 名稱與 ID
- 建立的 SLO 名稱與 ID
- Dashboard 連結（若有更新）
- 若有錯誤，說明原因與解決方式

---

## 參數解析規則（若 $ARGUMENTS 有值）

`$ARGUMENTS` 格式：`[service] ["METHOD /path"] [resource-filter] [p99s 或 auto]`

範例：
- `/datadog-setup-monitoring` → 引導式對話收集全部參數
- `/datadog-setup-monitoring "GET /api/v3/orders"` → 詢問其餘參數（service 預設 athena-api）
- `/datadog-setup-monitoring athena-api "GET /api/v3/orders" "get_/api/v3/orders*" auto` → 自動計算閾值並執行

---

## 常見 resource-filter 範例

| endpoint | resource-filter |
|----------|----------------|
| `GET /api/v4/products/:id` | `get_/api/v4/products*` |
| `GET /api/v2/cart/items` | `get_/api/v2/cart/items*` |
| `POST /api/v1/orders` | `post_/api/v1/orders*` |
| `PUT /api/v1/users/:id` | `put_/api/v1/users*` |

---

## 前置條件確認

執行前確認以下環境變數已設定，否則提示使用者：

```bash
echo "DD_API_KEY: $([ -n "$DD_API_KEY" ] && echo '✅ 已設定' || echo '❌ 未設定')"
echo "DD_APP_KEY: $([ -n "$DD_APP_KEY" ] && echo '✅ 已設定' || echo '❌ 未設定')"
```

若未設定，提示：
> 請先在 `~/.claude/settings.json` 的 `env` 區塊設定 `DD_API_KEY` 與 `DD_APP_KEY`，或執行 `export DD_API_KEY=xxx`
