# 第一層：行為診斷

**判斷依據**：任何「agent 跑起來很卡」「記憶體不夠」「要不要升規」的請求，一律先跑本層。
**套用方式**：本檔 + `scripts/scan_sessions.py`。本層調整完並複測後仍不足，才讀 `environment.md`。

> **這一層是整份 skill 的主體。** 「在資源有限的機器上擴大 agent 使用」的答案主要不在硬體，
> 在使用方法 —— 探勘走索引、探勘交給 subagent，context 就不會膨脹到需要靠多開 session 來閃避。

---

## 取數

```bash
python3 scripts/scan_sessions.py --preflight   # 先跑這個：這台量得到什麼、量不到什麼
python3 scripts/scan_sessions.py               # 人類可讀
python3 scripts/scan_sessions.py --json        # 給報告產生器
```

- 全量掃描 session 紀錄目錄，實測 207 session／87,765 行約 **4.5 秒**完成
- ⛔ **先跑 `--preflight`。** 不看它就直接讀指標，會把「量不到」讀成「量到 0」——
  這兩件事在本 skill 裡的處置完全相反。四個必看欄位見 `portability.md`
- ⛔ **腳本只輸出統計量**。要擴充輸出欄位時，先確認不會帶出任何對話內容、程式碼或業務資訊
- 「還開著／未封存」不在腳本範圍，須另查 `list_sessions` 的 `isRunning` / `isArchived` / `lastActivityAt`

### 語料不完整時怎麼辦

`--preflight` 或報告的 `corpus.corpus_truncated` 為 `true`，代表更早的紀錄已被保留期刪除。

- ✅ **聲明限制、限縮結論**：受影響的指標只是這個視窗內的**下界**，不是總量
- ✅ 把判斷重心移到當下現況（`scripts/probe_host.sh`）
- ⛔ **不得建議延長保留期**。要機器改設定來配合工具，方向是反的
  （`portability.md` 的 R2）。語料是什麼就是什麼

### 使用強度不同的機器怎麼比

絕對值（如「程式碼探勘 ≥ 20」）在 207 session 的機器與 7 session／40 天的機器上意義不同。
跨機器對照一律看 `metric_1_index_bypass.intensity_normalized`：

| 欄位 | 意義 |
|---|---|
| `explore_per_session` | 每個 session 的探勘次數 |
| `explore_per_active_hour` | 每小時實際使用的探勘次數（`active_hours` 為 0 時回 `UNKNOWN`） |

---

## 五個指標

### 🥇 指標 1：探勘繞道率

**問的是**：探勘程式碼時，是查索引，還是把檔案整個讀進 context？

```text
explore_cost = Read + Grep + Glob
             + Bash 中含唯讀探勘動詞者（grep|rg|ag|find|cat|bat|head|tail|sed|awk|ls|wc|tree）
index_use    = 工具名稱含任一索引樣式的 MCP 呼叫
bypass_ratio = 程式碼探勘次數 / max(index_use, 1)
```

**`index_use` 有三態，不是一個數字。** ⛔ 把「主機沒裝索引型 MCP」讀成 `0`
就是在偽造一個行為旗標 —— 兩者的處置完全相反：沒裝 → `UNKNOWN`（硬旗標不成立）／
有設定且 process 活著但 0 次呼叫 → `0` + 🔴（是行為問題，指名並給建議）／有呼叫 → 次數。
三態的判準正本是 `portability.md` 的 R1，⛔ 不要在這裡另立一份。

⛔ **索引樣式清單不可寫死成三個 server 名稱。**
〔事證：某機用的是另一套索引工具，白名單三個名稱一次都沒出現，
564 次工具呼叫命中 0 —— 照原樣判會得到一個假的 🔴 硬旗標〕
本機的索引工具不在預設清單裡時，把名稱片段加進 `scripts/index-mcp-patterns.json`。

- **只把「目標是程式碼」的探勘算進分子**：目標路徑落在某個 git repo 底下，或指向程式碼副檔名
- ⛔ **不要用 session 的 cwd 當閘門。** 索引型 MCP 可以從任何 cwd 對任何 repo 查詢，
  而在非 repo 目錄底下 grep 一個 repo 裡的檔案同樣是程式碼探勘
  〔事證：以 cwd 為閘門會得出「22 個 code repo session、索引使用 0」這種既真實又誤導的結論 ——
  實際上索引查詢全部發生在 cwd 不是 repo 的 session 裡〕

**門檻**

| 判定 | 條件 |
|---|---|
| 🔴 硬旗標 | `程式碼探勘 ≥ 20` 且 `index_use == 0` 且 **`index_state == CONFIGURED`** |
| 🔴 紅 | ratio > p90 — **僅在 `n ≥ 20` 時成立** |
| ⚠️ 黃 | ratio > p75 — **僅在 `n ≥ 20` 時成立** |

⛔ **樣本不足時不得使用 p75／p90。**
〔事證：某機的 ratio 分布只有 **n = 4**，卻印出 `p75 = 48.0`、`p90 = 53.4` ——
4 個點算 p90 統計上不成立，而且那 4 個點正是被評判的對象本身，是**門檻自引**〕

`n < 20` 時腳本不輸出 `threshold_*`，改輸出帶 `n` 與 `is_threshold_grade: false` 的
`per_session_ratio_sample_percentiles`。那組數字**只能當樣本描述，不得當判準**。
此時只用硬旗標與絕對值，並在報告寫明樣本量。

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

**指標的概念可攜，自動量測不可攜。** session 紀錄是 Claude Code 專有格式，
其他 harness 跑不出 `scan_sessions.py` 的數字，改走 `portability.md` 照五個指標寫好的自陳問卷。
自陳答案要**標為自陳**，不得與掃描數字混在同一張表裡比較 ——
**要明講這個落差，不要讓人以為跑得出數字。**
跨裝置的失效清單與兩條治理規則同樣在那一份，**每次換機器先讀它**。

---

## 狀態說明

| 段落 | 狀態 |
|---|---|
| 指標 1、2、3、5 的計算式與陷阱 | ✅ **已在兩台機器實地驗證**（裝置 A：207 session／87,765 行；裝置 B：7 session／2,384 行；兩台皆 0 解析失敗） |
| 指標 4 的兩個 ⛔ | ✅ 已實測否證（774h span、184.6h 中位拖尾），並有 fixture 測試覆蓋 |
| `index_use` 的三態 | ✅ 裝置 B 是「有設定、process 活著、0 次呼叫」的實例，這一態在 v0.1 被誤壓成硬旗標 |
| p75／p90 門檻的**跨人適用性** | ⚠️ **仍未驗證，且 v0.2 起在 `n < 20` 時不再輸出**。兩台**不構成分布** —— 正解不是校準更好的門檻，是不輸出 |
| 「過度切分」的 p95 門檻 | ⚠️ **未驗證**。僅觀察到單一極端案例（一個 session 派了 46 個 subagent），不足以定門檻 |
| 指標 3 的「斜率為正」判定 | ⚠️ **未實作**。目前只實作峰值；斜率沒有實作，也沒有排入 |
| 強度正規化（`per_session`／`per_active_hour`） | ⚠️ **已實作但未跨裝置驗證**。兩台的值還沒放在一起比較過 |
| 語料截斷偵測 | ✅ 裝置 B 實測命中（語料涵蓋 106 天 vs 保留期 30 天，`list_sessions` 另有 3 個月的落差） |

⛔ 沒驗證過的放待累積，**不要憑空杜撰**。每次套用到新的人／新的機器，
就回來更新這張表，並在 `portability.md` 的裝置 profile 加一列。
