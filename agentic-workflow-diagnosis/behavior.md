# 第一層：行為診斷

**判斷依據**：任何「agent 跑起來很卡」「記憶體不夠」「要不要升規」的請求，一律先跑本層。
**套用方式**：本檔 + `scripts/scan_sessions.py`。本層調整完並複測後仍不足，才讀 `environment.md`。

> **這一層是整份 skill 的主體。** 「在資源有限的機器上擴大 agent 使用」的答案主要不在硬體，
> 在使用方法 —— 探勘走索引、探勘交給 subagent，context 就不會膨脹到需要靠多開 session 來閃避。

---

## 取數

```bash
python3 scripts/scan_sessions.py            # 人類可讀
python3 scripts/scan_sessions.py --json     # 給報告產生器
```

- 全量掃描 session 紀錄目錄，實測 207 session／87,765 行約 **4.5 秒**完成
- ⛔ **腳本只輸出統計量**。要擴充輸出欄位時，先確認不會帶出任何對話內容、程式碼或業務資訊
- 「還開著／未封存」不在腳本範圍，須另查 `list_sessions` 的 `isRunning` / `isArchived` / `lastActivityAt`

---

## 五個指標

### 🥇 指標 1：探勘繞道率

**問的是**：探勘程式碼時，是查索引，還是把檔案整個讀進 context？

```text
explore_cost = Read + Grep + Glob
             + Bash 中含唯讀探勘動詞者（grep|rg|ag|find|cat|bat|head|tail|sed|awk|ls|wc|tree）
index_use    = mcp__code-review-graph__* + mcp__semble__* + mcp__gitlab__semantic_code_search
bypass_ratio = 程式碼探勘次數 / max(index_use, 1)
```

- **只把「目標是程式碼」的探勘算進分子**：目標路徑落在某個 git repo 底下，或指向程式碼副檔名
- ⛔ **不要用 session 的 cwd 當閘門。** 索引型 MCP 可以從任何 cwd 對任何 repo 查詢，
  而在非 repo 目錄底下 grep 一個 repo 裡的檔案同樣是程式碼探勘
  〔事證：以 cwd 為閘門會得出「22 個 code repo session、索引使用 0」這種既真實又誤導的結論 ——
  實際上索引查詢全部發生在 cwd 不是 repo 的 session 裡〕

**門檻**（相對值，每次掃描重算，不要抄下面的數字）

| 判定 | 條件 |
|---|---|
| 🔴 硬旗標 | `程式碼探勘 ≥ 20` 且 `index_use == 0` |
| 🔴 紅 | ratio > 全體 p90 |
| ⚠️ 黃 | ratio > 全體 p75 |

**給提案者的建議**：指名「這個 session 做了 N 次程式碼探勘、0 次索引查詢」，
建議探勘型任務開頭先建圖再語意搜尋，把 `get_impact_radius` 從 review 場景推廣出去。

---

### 🥈 指標 2：主 context 探勘負載

**問的是**：探勘是在主 context 裡硬做，還是 delegate 給 subagent？

```text
main_context_load = explore_cost(isSidechain == false) / explore_cost(total)
```

| 判定 | 條件 | 建議 |
|---|---|---|
| 🔴 該派卻硬做 | `explore_cost ≥ 20` 且 `Agent 呼叫 == 0` 且 `main_context_load > 0.9` | 探勘階段一律 delegate，主 context 只收結論 |
| ⚠️ 過度切分 | subagent 數 > 全體 p95 | 反向極端也是病 |

**這個指標吃下指標 1 涵蓋不到的部分** —— 管理／文件類探勘用不上程式碼索引，但一樣會撐大 context，
解法是同一個：讓它發生在 subagent 裡。

---

### 🥉 指標 3：context 膨脹成本

**問的是**：session 是不是開太久沒清？

- 取 `cache_read_input_tokens` 的**單 session 峰值**與**逐 work-segment 的成長斜率**
- 🔴 峰值 > 全體 p90，或單一 work segment 內斜率為正且從未出現 `compact_boundary`

⚠️ **不要單獨用 compaction 次數判斷。** 欄位齊全但覆蓋率極低
〔事證：全 corpus 只有 5 次 compaction、分布在 3 個 session，1.4% 的覆蓋率統計上不成立〕。
`cache_read` 覆蓋 100% 的 assistant 訊息，且直接對應成本，是更敏感的代理指標。

**建議**：指出「第 N 段對話後 context 已達 X，建議 `/clear` 或開新 session」。

---

### 4️⃣ 指標 4：session 使用型態

- **實際使用時長** = 各 work segment 長度總和（gap > 30 分鐘切段）
- **工作段數** > 1 代表被 resume 過

⚠️ **不要用 `max(timestamp) - min(timestamp)` 當使用時長。**
〔事證：原始 span 最大達 774 小時（32 天），那是 resume 造成的假象，不是「開著不關」。
直接用會產生大量假陽性〕

⛔ **不要用檔案 mtime 推算「拖尾」。**
〔事證：以「最後一筆帶時戳事件 → mtime」計算，得到中位數 184.6 小時、最大 1172.6 小時，是無意義的值 ——
部分 record type 不帶時戳，且檔案在最後一筆時戳事件之後仍會被改寫〕
「還開著／未封存」的權威來源是 `list_sessions`，那是診斷當下的即時狀態。

---

### 5️⃣ 指標 5：重複操作率（輔助）

```text
dup_read_rate = 重複 Read 次數 / 不重複檔案數
```

- 只在 `unique_files > 5` 時計算；> 0.5 者標記
- **僅作輔助佐證** —— 重讀有時是 compaction 後的合理行為。
  配合指標 3 使用（「壓縮後重讀 N 個檔案」）比單看重讀率有說服力

---

## 可攜性的誠實邊界

**指標的概念可攜，自動量測不可攜。** session 紀錄是 Claude Code 專有格式；
其他 harness（Roo Code／Antigravity 等）沒有同構的紀錄，`scan_sessions.py` 在那裡跑不出東西。

→ **其他 harness 走人工自陳版**：用同一組問題（探勘怎麼做的？有沒有 delegate？session 開多久？），
答案靠自述而非掃描。**要明講這個落差，不要讓人以為跑得出數字。**

---

## 狀態說明

| 段落 | 狀態 |
|---|---|
| 指標 1、2、3、5 的計算式與陷阱 | ✅ **已在一台機器上實地驗證**（2026-08-28，207 session／87,765 行／0 解析失敗） |
| 指標 4 的兩個 ⛔ | ✅ 已實測否證（774h span、184.6h 中位拖尾） |
| p75／p90 門檻的**跨人適用性** | ⚠️ **未驗證**。目前分布只來自一台機器、一種角色。第二個人跑過之後才知道門檻要不要分角色 |
| 「過度切分」的 p95 門檻 | ⚠️ **未驗證**。僅觀察到單一極端案例（一個 session 派了 46 個 subagent），不足以定門檻 |
| 指標 3 的「斜率為正」判定 | ⚠️ **未實作**。目前只實作峰值，斜率待補 |

⛔ 沒驗證過的放待累積，**不要憑空杜撰**。每次套用到新的人／新的機器，就回來更新這張表。
