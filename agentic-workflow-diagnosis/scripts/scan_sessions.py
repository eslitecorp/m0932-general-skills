#!/usr/bin/env python3
"""
scan_sessions.py — 從 Claude Code 的 session 紀錄量出五個行為指標。

用途：agentic-workflow-diagnosis skill 第一層「行為診斷」的取數工具。

⛔ 隱私硬規則（跑在別人機器上時尤其重要）
   本腳本**只輸出統計量**。不得輸出任何對話內容、程式碼、prompt 文字或業務資訊。
   唯一會出現的字串是：工具名稱、MCP server 名稱、cwd 的最後一段目錄名、以及純數字。
   若要擴充功能，任何新增的輸出欄位都必須先通過這條檢查。

用法
    python3 scan_sessions.py                 # 人類可讀摘要
    python3 scan_sessions.py --json          # 機器可讀（給報告產生器）
    python3 scan_sessions.py --exclude-current   # 排除當前 session（避免自己汙染統計）
    python3 scan_sessions.py --root <path>   # 指定 projects 目錄

兩個必須處理的陷阱（見 SKILL.md，拿掉會讓指標失真）
  1. 探勘量必須從 Bash 命令裡撈 —— 實測 Grep 只被呼叫 3 次、Glob 0 次，
     但 Bash 有 74% 含唯讀探勘動詞。只數 Read/Grep/Glob 會低估約 8 倍。
  2. session 時長必須先做 work-segment 切分 —— 原始 max-min 最大達 774 小時，
     那是 resume 造成的假象，直接用會產生大量假陽性。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# --- 常數 ------------------------------------------------------------------

DEFAULT_ROOT = Path.home() / ".claude" / "projects"

# 直接的探勘工具
EXPLORE_TOOLS = {"Read", "Grep", "Glob"}

# Bash 裡的唯讀探勘動詞。位置限定在「命令開頭」或「管線/分隔/子殼之後」，
# 避免把 `git commit -m "cat"` 這種出現在參數裡的字誤計。
BASH_EXPLORE_VERBS = (
    "grep", "rg", "ag", "find", "cat", "bat", "head", "tail",
    "sed", "awk", "ls", "wc", "tree",
)
BASH_EXPLORE_RE = re.compile(
    r"(?:^|[|&;]|\$\(|`|\n)\s*(?:sudo\s+)?(" + "|".join(BASH_EXPLORE_VERBS) + r")\b"
)

# 索引型 MCP：探勘應該優先走這些，而不是把整個檔案讀進 context
#
# ⛔ 這份清單是「預設值」，不是「全部」。寫死 server 名稱在別台機器上會靜默失效 ——
#    白名單裡的 server 在該機不存在時，index_use 會是 0，而 0 會被讀成「有索引卻不用」
#    的行為問題，實際上是「這台根本沒裝索引型 MCP」。兩者的處置完全相反。
#    〔事證：某機用的是 codebase-memory，三個預設 prefix 一個都沒出現，
#      564 次工具呼叫裡白名單命中 0 —— 照原樣判會得到一個假的 🔴 硬旗標〕
#
# 因此本腳本改為：預設清單 + 同目錄的 index-mcp-patterns.json（使用者可擴充）
#                + 從主機 MCP 設定實際發現的 server 名稱。
DEFAULT_INDEX_MCP_PATTERNS = [
    "code-review-graph",
    "semble",
    "gitlab__semantic_code_search",
    "codebase-memory",
    "serena",
]

PATTERNS_FILE = Path(__file__).resolve().parent / "index-mcp-patterns.json"

# 主機 MCP 設定的候選位置（依 Claude Code 的實際佈局）
MCP_CONFIG_PATHS = (
    Path.home() / ".claude.json",
    Path.home() / ".claude" / ".mcp.json",
)

# 樣本數低於此值時，分布只能當「樣本百分位」看，不得當門檻用。
# 〔事證：某機 ratio 分布 n=4，卻印出 p75=48.0 / p90=53.4 —— 4 個點算 p90 統計上不成立〕
MIN_DIST_N = 20

# Claude Code 未設 cleanupPeriodDays 時的預設保留天數
DEFAULT_RETENTION_DAYS = 30

# 家目錄底下往下找 git repo 的層數。3 足以涵蓋 ~/Work/<x>/codebase/<y> 這種深度，
# 又不至於掃穿整個家目錄。掃不到的深層 repo 由 projects 目錄名反解與 --repo-root 補。
HOME_SCAN_DEPTH = 3

# 家目錄掃描的剪枝清單。不剪的話 depth 3 會踩進 Library 這種數十萬個目錄的樹，
# 實測會讓 --preflight 跑超過兩分鐘 —— 一個自檢工具卡在自檢是不能接受的。
HOME_SCAN_PRUNE = {
    "Library", "Applications", "Movies", "Music", "Pictures", "Photos",
    "node_modules", "venv", ".venv", "vendor", "target", "build", "dist",
    "Public", "Postman", "Parallels", "Creative Cloud Files",
}

# 掃描目錄數上限。超過即停 —— 寧可少找到幾個 repo（有 --repo-root 可補），
# 也不要讓自檢本身變成效能問題。
HOME_SCAN_MAX_DIRS = 4000

# work segment 切分門檻：事件間隔超過這個秒數就視為換了一段工作
SEGMENT_GAP_SEC = 30 * 60

# 程式碼副檔名：用來判斷一次探勘是不是「程式碼探勘」
CODE_EXT_RE = re.compile(
    r"\.(go|rb|py|ts|tsx|js|jsx|java|kt|swift|rs|c|h|cc|cpp|php|scala|ex|exs|sql|proto)\b"
)


def decode_project_dir(name: str) -> str:
    """
    把 projects 目錄名反解回 cwd。

    Claude Code 以 cwd 的路徑分隔符換成 "-" 當目錄名，例如
    "-Users-alice-Work-foo" → "/Users/alice/Work/foo"。

    這是完全可攜的來源 —— 不必猜使用者把 repo 放在哪一層，
    使用者實際在哪些目錄工作過，目錄名本身就記著了。
    """
    if not name.startswith("-"):
        return ""
    return "/" + name[1:].replace("-", "/")


def discover_repo_roots(root: Path, extra_roots=()) -> list[str]:
    """
    找出本機的 git repo 根目錄。

    ⚠️ 為什麼不用 session 的 cwd 當閘門：索引型 MCP 可以從任何 cwd 對任何 repo 查詢，
    而在非 repo 目錄底下 grep 一個 repo 裡的檔案，同樣是「程式碼探勘」。
    用 cwd 判定會把這類漏掉 —— 實測 code-review-graph 的 39 次呼叫全部發生在
    cwd 不是 repo 的 session 裡，若以 cwd 為閘門會得出「code repo session 索引使用 0」
    這種既真實又誤導的結論。

    ⛔ 不再寫死 ~/Work/Code 這類路徑，也不再只掃 depth 1。
       〔事證：某機的專案在 ~/Work/<x>/codebase/<y>（depth 3），寫死 depth 1 的版本掃不到，
         那些 repo 底下的探勘於是全部沒被算成程式碼探勘〕
       改為四個來源取**聯集**（刻意是聯集不是取代 —— 只留 projects 反解會漏掉
       「有 repo 但從未以它為 cwd 開過 session」的情況，實測會讓程式碼探勘量少一半）：
         1. 家目錄底下的 git repo，掃到 HOME_SCAN_DEPTH 層
         2. projects 目錄名反解出的 cwd（使用者真正工作過的地方，不限層數）
         3. 目前工作目錄所屬的 git repo
         4. --repo-root 明確指定（逃生門）
    """
    roots = set()

    # 1. 家目錄底下的 git repo（可攜：不假設任何子目錄命名）
    visited = [0]

    def scan_dir(base: Path, depth: int):
        if depth < 0 or visited[0] >= HOME_SCAN_MAX_DIRS:
            return
        try:
            children = list(base.iterdir())
        except OSError:
            return
        for child in children:
            if visited[0] >= HOME_SCAN_MAX_DIRS:
                return
            if child.name.startswith(".") or child.name in HOME_SCAN_PRUNE:
                continue
            try:
                if not child.is_dir() or child.is_symlink():
                    continue
            except OSError:
                continue
            visited[0] += 1
            if (child / ".git").exists():
                roots.add(str(child))
                continue          # repo 內不再往下找巢狀 repo
            scan_dir(child, depth - 1)

    scan_dir(Path.home(), HOME_SCAN_DEPTH)

    # 2. 從 projects 目錄名反解（不限層數，補上 1 掃不到的深層 repo）
    try:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            cwd = decode_project_dir(child.name)
            if not cwd:
                continue
            p = Path(cwd)
            # 從該 cwd 往上找 .git，涵蓋 cwd 是 repo 子目錄的情況
            for cand in (p, *p.parents):
                if (cand / ".git").exists():
                    roots.add(str(cand))
                    break
    except OSError:
        pass

    # 3. 目前工作目錄
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            roots.add(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass

    # 4. 明確指定
    for r in extra_roots:
        if r:
            roots.add(str(Path(r).expanduser().resolve()))

    return sorted(roots, key=len, reverse=True)


def load_index_patterns() -> list[str]:
    """預設索引 MCP 樣式 + 同目錄 index-mcp-patterns.json 的擴充。"""
    patterns = list(DEFAULT_INDEX_MCP_PATTERNS)
    try:
        extra = json.loads(PATTERNS_FILE.read_text(encoding="utf-8"))
        if isinstance(extra, dict):
            extra = extra.get("patterns", [])
        if isinstance(extra, list):
            patterns.extend(str(x) for x in extra if x)
    except (OSError, json.JSONDecodeError):
        pass
    return sorted(set(patterns))


def discover_host_mcp_servers() -> set[str]:
    """
    列出主機設定裡的 MCP server 名稱（含 root 層與各 project 層）。

    用途是分辨兩件在 v0.1 被壓成同一個 0 的事：
      - 主機根本沒裝索引型 MCP  → index_use 應為 UNKNOWN，不是行為問題
      - 裝了卻從沒呼叫過        → index_use 為 0，這才是行為問題
    """
    servers = set()

    def harvest(obj):
        if isinstance(obj, dict):
            ms = obj.get("mcpServers")
            if isinstance(ms, dict):
                servers.update(ms.keys())
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    harvest(v)
        elif isinstance(obj, list):
            for v in obj:
                harvest(v)

    for p in MCP_CONFIG_PATHS:
        try:
            harvest(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return servers


def read_retention_days() -> tuple[int, str]:
    """
    讀出 session 紀錄的保留天數，回傳 (天數, 來源)。

    未設定時 Claude Code 走預設值；本函式把「未設定」如實標成 default，
    不假裝主機明確設過。
    """
    for p in (Path.home() / ".claude" / "settings.json",
              Path.home() / ".claude" / "settings.local.json"):
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(cfg, dict) and isinstance(cfg.get("cleanupPeriodDays"), int):
            return cfg["cleanupPeriodDays"], str(p)
    return DEFAULT_RETENTION_DAYS, "default（未設定）"


def detect_corpus_truncation(mains, subs) -> dict:
    """
    偵測歷史語料是否已被保留期截斷。

    ⛔ 偵測到截斷時，**不得建議延長保留期**。要機器改設定來配合工具是本末倒置；
       正確處置是**聲明限制、限縮結論範圍**，並把判斷重心移到當下現況的 live probe。
       這條是 skill 的 R2 原則（當下現況優先，不改機器）在腳本層的實作。
    """
    days, source = read_retention_days()
    stamps = [t for s in (mains + subs) for t in s.timestamps]
    if not stamps:
        return {
            "corpus_truncated": "UNKNOWN",
            "reason": "語料沒有任何可解析的時戳",
            "retention_days": days,
            "retention_source": source,
        }

    oldest = min(stamps)
    span_days = (datetime.now(timezone.utc) - oldest).total_seconds() / 86400.0
    # 最舊紀錄貼齊保留期邊界（給 10% 緩衝）→ 更早的已被刪掉
    truncated = span_days >= days * 0.9

    return {
        "corpus_truncated": bool(truncated),
        "corpus_span_days": round(span_days, 1),
        "retention_days": days,
        "retention_source": source,
        "_note": (
            "語料涵蓋天數已貼齊保留期 → 更早的紀錄已被刪除，"
            "受影響指標只能視為該視窗內的下界。"
            "請與 list_sessions 的最舊 lastActivityAt 對照確認實際落差。"
            "⛔ 不要為了讓數字好看去延長保留期 —— 聲明限制即可，判斷重心走 live probe。"
            if truncated else
            "語料涵蓋天數未貼齊保留期，無截斷跡象。"
        ),
    }


def resolve_session_id(explicit: str | None) -> tuple[str | None, str]:
    """
    解出「當前 session」的 id，回傳 (id, 來源)。

    ⛔ 環境變數名稱不可攜。v0.1 只讀 CLAUDE_SESSION_ID，在某機該變數不存在
       （實際名稱是 CLAUDE_CODE_HOST_SESSION_ID），於是 --exclude-current
       **靜默無作用** —— 診斷用的 session 汙染了自己的統計，而且沒有任何提示。
       這是 environment.md「陷阱四：失敗時可能靜默回 0」的同型錯誤。
       本函式解不出來時回 (None, "")，由呼叫端**大聲失敗**，不得默默略過。
    """
    if explicit:
        return explicit, "--session-id"
    for key in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID"):
        val = os.environ.get(key)
        if val:
            return val, key
    return None, ""


# --- 工具函式 --------------------------------------------------------------

def parse_ts(raw):
    """ISO8601 → aware datetime；失敗回 None（不拋，避免單筆壞資料中斷全量掃描）。"""
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_index_tool(name: str, patterns) -> bool:
    """工具名稱含任一索引樣式即算索引查詢（不比對完整 server 名，換版換名才不會失效）。"""
    if not name.startswith("mcp__"):
        return False
    return any(p in name for p in patterns)


def iter_tool_uses(rec):
    """yield (tool_name, tool_input) —— 只取工具名與 input，不碰任何文字內容。"""
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block.get("name") or "", block.get("input") or {}


def is_code_target(target: str, repo_roots) -> bool:
    """
    這次探勘是不是針對程式碼？

    兩個判準取聯集：目標路徑落在某個 git repo 底下，或目標指向程式碼副檔名。
    刻意不看 session 的 cwd —— 理由見 discover_repo_roots 的註解。
    """
    if CODE_EXT_RE.search(target):
        return True
    return any(root in target for root in repo_roots)


def percentile(values, p):
    """線性插值百分位。values 需已排序且非空。"""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    k = (len(values) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    if lo == hi:
        return float(values[lo])
    return float(values[lo]) * (hi - k) + float(values[hi]) * (k - lo)


# --- 單一 session 的統計 ----------------------------------------------------

class SessionStat:
    __slots__ = (
        "path", "session_id", "cwd", "git_branch", "is_subagent",
        "explore_direct", "explore_bash", "index_use", "agent_calls",
        "explore_main", "explore_total", "explore_code", "tool_counter", "mcp_counter",
        "read_paths", "timestamps", "cache_read_series", "compact_events",
        "tokens", "models", "parse_errors", "lines",
    )

    def __init__(self, path: Path, is_subagent: bool):
        self.path = path
        self.session_id = ""
        self.cwd = ""
        self.git_branch = ""
        self.is_subagent = is_subagent
        self.explore_direct = 0
        self.explore_bash = 0
        self.index_use = 0
        self.agent_calls = 0
        self.explore_main = 0      # isSidechain == False 的探勘
        self.explore_total = 0
        self.explore_code = 0      # 目標落在 git repo 內或指向程式碼副檔名者
        self.tool_counter = Counter()
        self.mcp_counter = Counter()
        self.read_paths = Counter()
        self.timestamps = []
        self.cache_read_series = []
        self.compact_events = []
        self.tokens = Counter()
        self.models = Counter()
        self.parse_errors = 0
        self.lines = 0

    # -- 衍生指標 --

    @property
    def explore_cost(self) -> int:
        return self.explore_direct + self.explore_bash

    @property
    def bypass_ratio(self) -> float:
        return self.explore_cost / max(self.index_use, 1)

    @property
    def is_code_repo(self) -> bool:
        """
        是否在真正的 code repo 裡工作。

        ⚠️ 非 git 目錄的 `gitBranch` 會是字面值 "HEAD"，不是空字串。
        只判 `bool(gitBranch)` 會把管理／文件類 session 全部誤判成 code repo
        —— 實測會讓 207 個 session 有 207 個被判為 code repo，
        對它們套用索引指標即是大規模假陽性（管理類工作本來就不該用 code graph）。
        """
        return bool(self.git_branch) and self.git_branch != "HEAD"

    @property
    def main_context_load(self) -> float:
        if self.explore_total == 0:
            return 0.0
        return self.explore_main / self.explore_total

    @property
    def work_segments(self):
        """回傳 [(start, end), ...]，用 gap > SEGMENT_GAP_SEC 切分。"""
        ts = sorted(t for t in self.timestamps if t)
        if not ts:
            return []
        segs, seg_start, prev = [], ts[0], ts[0]
        for t in ts[1:]:
            if (t - prev).total_seconds() > SEGMENT_GAP_SEC:
                segs.append((seg_start, prev))
                seg_start = t
            prev = t
        segs.append((seg_start, prev))
        return segs

    @property
    def active_hours(self) -> float:
        """實際使用時長 = 各 work segment 長度總和（不是 max-min）。"""
        return sum((e - s).total_seconds() for s, e in self.work_segments) / 3600.0

    @property
    def span_hours(self) -> float:
        """生命週期（含 resume 的空窗）。僅供對照，不可當使用時長。"""
        segs = self.work_segments
        if not segs:
            return 0.0
        return (segs[-1][1] - segs[0][0]).total_seconds() / 3600.0

    @property
    def segment_count(self) -> int:
        """工作段數。>1 代表這個 session 被 resume 過。"""
        return len(self.work_segments)

    # ⛔ 刻意不提供「拖尾時長」。
    # 曾嘗試以「最後一筆帶時戳的事件 → 檔案 mtime」計算，實測中位數 184.6h、最大 1172.6h，
    # 是無意義的值：部分 record type（last-prompt / mode 等）不帶時戳，且檔案在最後一筆
    # 時戳事件之後仍會被改寫，mtime 因此不是「何時停止使用」的可靠代理。
    # 「還開著／未封存」的權威來源是 list_sessions 的 isRunning / isArchived / lastActivityAt，
    # 那是診斷當下才取得的即時狀態，不在本腳本的職責範圍。

    @property
    def dup_read_rate(self) -> float:
        uniq = len(self.read_paths)
        if uniq == 0:
            return 0.0
        dups = sum(c - 1 for c in self.read_paths.values() if c > 1)
        return dups / uniq

    @property
    def cache_read_peak(self) -> int:
        return max(self.cache_read_series) if self.cache_read_series else 0


def scan_file(path: Path, is_subagent: bool, exclude_ids: set, repo_roots,
              index_patterns) -> SessionStat | None:
    st = SessionStat(path, is_subagent)
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            st.lines += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                st.parse_errors += 1
                continue
            if not isinstance(rec, dict):
                continue

            sid = rec.get("sessionId")
            if sid:
                if sid in exclude_ids:
                    return None          # 排除當前 session，避免自己汙染統計
                st.session_id = sid
            st.cwd = rec.get("cwd") or st.cwd
            st.git_branch = rec.get("gitBranch") or st.git_branch

            ts = parse_ts(rec.get("timestamp"))
            if ts:
                st.timestamps.append(ts)

            # compaction
            if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
                meta = rec.get("compactMetadata") or {}
                st.compact_events.append({
                    "pre": meta.get("preTokens"),
                    "post": meta.get("postTokens"),
                    "trigger": meta.get("trigger"),
                    "duration_ms": meta.get("durationMs"),
                })

            # token usage
            msg = rec.get("message")
            if isinstance(msg, dict):
                if msg.get("model"):
                    st.models[msg["model"]] += 1
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    for k in ("input_tokens", "output_tokens",
                              "cache_read_input_tokens", "cache_creation_input_tokens"):
                        v = usage.get(k)
                        if isinstance(v, int):
                            st.tokens[k] += v
                    cr = usage.get("cache_read_input_tokens")
                    if isinstance(cr, int):
                        st.cache_read_series.append(cr)

            # MCP / skill 歸因（現成欄位，不用自己 parse 前綴）
            if rec.get("attributionMcpServer"):
                st.mcp_counter[rec["attributionMcpServer"]] += 1

            sidechain = bool(rec.get("isSidechain"))

            for name, tin in iter_tool_uses(rec):
                if not name:
                    continue
                st.tool_counter[name] += 1

                if name == "Agent":
                    st.agent_calls += 1

                if is_index_tool(name, index_patterns):
                    st.index_use += 1

                hit = 0
                target = ""       # 用來判斷這次探勘是不是針對程式碼
                if name in EXPLORE_TOOLS:
                    st.explore_direct += 1
                    hit = 1
                    fp = tin.get("file_path") or tin.get("path") or tin.get("pattern") or ""
                    if isinstance(fp, str):
                        target = fp
                    if name == "Read" and isinstance(tin.get("file_path"), str) and tin["file_path"]:
                        st.read_paths[tin["file_path"]] += 1
                elif name == "Bash":
                    cmd = tin.get("command")
                    if isinstance(cmd, str) and BASH_EXPLORE_RE.search(cmd):
                        st.explore_bash += 1
                        hit = 1
                        target = cmd

                if hit:
                    st.explore_total += 1
                    if not sidechain:
                        st.explore_main += 1
                    if target and is_code_target(target, repo_roots):
                        st.explore_code += 1

    return st if st.lines else None


# --- 全量掃描與彙整 ---------------------------------------------------------

def collect(root: Path, exclude_ids: set, repo_roots, index_patterns):
    mains, subs = [], []
    for path in sorted(root.rglob("*.jsonl")):
        is_sub = "subagents" in path.parts
        st = scan_file(path, is_sub, exclude_ids, repo_roots, index_patterns)
        if st is None:
            continue
        (subs if is_sub else mains).append(st)
    return mains, subs


def build_report(mains, subs, host=None):
    all_st = mains + subs
    code_sessions = [s for s in mains if s.is_code_repo]

    tools = Counter()
    mcps = Counter()
    tokens = Counter()
    for s in all_st:
        tools.update(s.tool_counter)
        mcps.update(s.mcp_counter)
        tokens.update(s.tokens)

    explore_cost_total = sum(s.explore_cost for s in all_st)
    index_total = sum(s.index_use for s in all_st)
    explore_direct_total = sum(s.explore_direct for s in all_st)
    explore_bash_total = sum(s.explore_bash for s in all_st)

    sessions_with_index = [s for s in mains if s.index_use > 0]
    sessions_with_agent = [s for s in mains if s.agent_calls > 0]

    ratios = sorted(s.explore_code / max(s.index_use, 1)
                    for s in mains if s.explore_code > 0)
    peaks = sorted(s.cache_read_peak for s in mains if s.cache_read_peak > 0)
    actives = sorted(s.active_hours for s in mains)
    segs = sorted(s.segment_count for s in mains)
    dups = sorted(s.dup_read_rate for s in mains if len(s.read_paths) > 5)

    total_tok = sum(tokens.values())

    # code repo 與非 code repo 分開統計 —— 索引指標只對前者成立
    # 以「探勘目標是不是程式碼」分類，不以 session 的 cwd 分類
    code_explore = sum(s.explore_code for s in all_st)
    noncode_explore = explore_cost_total - code_explore
    code_index = index_total

    # 旗標：命中即應在報告中被指名
    flag_no_index = [s for s in mains if s.explore_code >= 20 and s.index_use == 0]
    flag_main_load = [
        s for s in mains
        if s.explore_cost >= 20 and s.agent_calls == 0 and s.main_context_load > 0.9
    ]

    def pct(vals):
        if not vals:
            return {}
        return {
            "n": len(vals),
            "min": round(vals[0], 4),
            "p25": round(percentile(vals, 25), 4),
            "median": round(percentile(vals, 50), 4),
            "p75": round(percentile(vals, 75), 4),
            "p90": round(percentile(vals, 90), 4),
            "max": round(vals[-1], 4),
            # n 不足時，這組數字是「這幾個樣本的百分位」，不是可拿來判定的門檻。
            "is_threshold_grade": len(vals) >= MIN_DIST_N,
        }

    def threshold(vals, p, cast=float):
        """樣本不足就不給門檻 —— 回 None，讓呼叫端印 UNKNOWN 而不是印一個假數字。"""
        if len(vals) < MIN_DIST_N:
            return None
        return cast(round(percentile(vals, p), 2))

    host = host or {}

    # 索引使用的三態判定 —— v0.1 把後兩者壓成同一個 0，處置卻完全相反
    index_servers = host.get("index_servers_configured")
    if index_servers is None:
        index_state = "UNKNOWN"
        index_reason = "未取得主機 MCP 設定，無法分辨『沒裝』與『裝了沒用』"
    elif not index_servers:
        index_state = "UNKNOWN"
        index_reason = "主機沒有任何索引型 MCP —— 這不是行為問題，不得命中硬旗標"
    else:
        index_state = "CONFIGURED"
        index_reason = f"主機已設定索引型 MCP：{', '.join(sorted(index_servers))}"

    # 硬旗標只在 CONFIGURED 時成立
    flag_no_index_effective = flag_no_index if index_state == "CONFIGURED" else []

    # 使用強度正規化：絕對值門檻在 207 session 與 7 session 的機器上意義不同
    total_active_hours = sum(s.active_hours for s in mains)

    return {
        "corpus": {
            "main_sessions": len(mains),
            "subagent_files": len(subs),
            "total_lines": sum(s.lines for s in all_st),
            "parse_errors": sum(s.parse_errors for s in all_st),
            "code_repo_sessions": len(code_sessions),
            "total_active_hours": round(total_active_hours, 2),
            **detect_corpus_truncation(mains, subs),
        },
        "host": host,
        "metric_1_index_bypass": {
            "_scope": "索引指標只對 code repo session 成立；非 code repo 的探勘量另計，"
                      "它是 context 成本訊號，不是索引違反",
            "explore_cost_all": explore_cost_total,
            "explore_direct": explore_direct_total,
            "explore_via_bash": explore_bash_total,
            "bash_share_of_explore": (
                round(explore_bash_total / explore_cost_total, 4) if explore_cost_total else 0.0
            ),
            "code_exploration": {
                "explore_cost": code_explore,
                "index_state": index_state,
                "index_state_reason": index_reason,
                "index_use": code_index if index_state == "CONFIGURED" else "UNKNOWN",
                "bypass_ratio": (
                    round(code_explore / max(code_index, 1), 1)
                    if index_state == "CONFIGURED" else "UNKNOWN"
                ),
                "sessions_with_code_explore": sum(1 for s in mains if s.explore_code > 0),
                "sessions_using_index": len(sessions_with_index),
                "per_session_ratio_sample_percentiles": pct(ratios),
                "threshold_p75": threshold(ratios, 75),
                "threshold_p90": threshold(ratios, 90),
                "flagged_sessions": len(flag_no_index_effective),
            },
            "non_code_exploration": {
                "explore_cost": noncode_explore,
                "note": "管理／文件類探勘，索引型 MCP 不適用；"
                        "這部分的 context 成本由指標 2（delegate 給 subagent）處理",
            },
            "index_use_total_all": index_total,
            "sessions_using_index_all": len(sessions_with_index),
            "intensity_normalized": {
                "_why": "絕對值（如「程式碼探勘 ≥ 20」）在 207 session 的機器與 "
                        "7 session／40 天的機器上意義不同；跨裝置比較必須用分母",
                "explore_per_session": (
                    round(explore_cost_total / len(mains), 2) if mains else 0.0
                ),
                "explore_per_active_hour": (
                    round(explore_cost_total / total_active_hours, 2)
                    if total_active_hours > 0 else "UNKNOWN"
                ),
            },
        },
        "metric_2_main_context_load": {
            "sessions_using_subagent": len(sessions_with_agent),
            "pct_sessions_using_subagent": (
                round(100 * len(sessions_with_agent) / len(mains), 1) if mains else 0.0
            ),
            "agent_calls_total": tools.get("Agent", 0),
            "flagged_sessions": len(flag_main_load),
        },
        "metric_3_context_bloat": {
            "cache_read_total": tokens.get("cache_read_input_tokens", 0),
            "pct_of_all_tokens": (
                round(100 * tokens.get("cache_read_input_tokens", 0) / total_tok, 1)
                if total_tok else 0.0
            ),
            "token_breakdown": dict(tokens),
            "cache_read_peak_sample_percentiles": pct(peaks),
            "threshold_p90": threshold(peaks, 90, int),
            "compact_events": sum(len(s.compact_events) for s in mains),
            "sessions_with_compact": sum(1 for s in mains if s.compact_events),
        },
        "metric_4_session_usage": {
            "_note": "「還開著／未封存」的權威來源是 list_sessions 的 isRunning / isArchived，"
                     "不在本腳本範圍。以 mtime 推算拖尾已驗證不可靠（中位數 184.6h 無意義），已移除",
            "active_hours_sample_percentiles": pct(actives),
            "segment_count_sample_percentiles": pct(segs),
            "resumed_sessions": sum(1 for s in mains if s.segment_count > 1),
        },
        "metric_5_redundant_read": {
            "dup_read_rate_sample_percentiles": pct(dups),
            "flagged_sessions": sum(1 for v in dups if v > 0.5),
        },
        "tool_top": tools.most_common(15),
        "mcp_top": mcps.most_common(12),
    }


def render(rep) -> str:
    c = rep["corpus"]
    m1, m2, m3, m4, m5 = (
        rep["metric_1_index_bypass"], rep["metric_2_main_context_load"],
        rep["metric_3_context_bloat"], rep["metric_4_session_usage"],
        rep["metric_5_redundant_read"],
    )
    L = []
    a = L.append
    a("=" * 62)
    a("Agentic 工作流程行為診斷 — 掃描結果")
    a("=" * 62)
    a(f"主 session {c['main_sessions']}／subagent 檔 {c['subagent_files']}／"
      f"{c['total_lines']:,} 行／解析失敗 {c['parse_errors']}")
    a(f"其中 code repo session：{c['code_repo_sessions']}"
      f"｜實際使用時長合計 {c['total_active_hours']} h")
    if c.get("corpus_truncated") is True:
        a(f"⚠️ 語料已被保留期截斷：涵蓋 {c['corpus_span_days']} 天 vs 保留期 "
          f"{c['retention_days']} 天（{c['retention_source']}）")
        a("   → 受影響指標只是這個視窗內的下界。判斷重心請走當下現況的 live probe。")
    a("")
    cr, nc = m1["code_exploration"], m1["non_code_exploration"]
    a("【指標 1】探勘繞道率")
    a(f"  全體探勘量 {m1['explore_cost_all']:,}"
      f"（直接工具 {m1['explore_direct']:,}／繞道 Bash {m1['explore_via_bash']:,}"
      f" = {m1['bash_share_of_explore']:.0%}）")
    a(f"  ── 程式碼探勘（索引指標適用）：{cr['explore_cost']:,} 次"
      f"／索引查詢 {cr['index_use']} 次  →  繞道率 {cr['bypass_ratio']}:1")
    a(f"     索引型 MCP 狀態：{cr['index_state']} —— {cr['index_state_reason']}")
    a(f"     有做程式碼探勘的 session {cr['sessions_with_code_explore']}"
      f"／其中用過索引型 MCP {cr['sessions_using_index']}")
    if cr["threshold_p75"] is None:
        n = cr["per_session_ratio_sample_percentiles"].get("n", 0)
        a(f"     門檻 UNKNOWN（樣本 n={n} < {MIN_DIST_N}，不足以構成分布）"
          f" ｜硬旗標命中 {cr['flagged_sessions']} 個 session")
    else:
        a(f"     門檻 p75={cr['threshold_p75']} p90={cr['threshold_p90']}"
          f" ｜硬旗標命中 {cr['flagged_sessions']} 個 session")
    inten = m1["intensity_normalized"]
    a(f"     強度正規化：每 session {inten['explore_per_session']} 次"
      f"／每小時 {inten['explore_per_active_hour']} 次")
    a(f"  ── 非程式碼探勘（索引不適用）：{nc['explore_cost']:,} 次")
    a(f"     → 交給指標 2 處理（delegate 給 subagent），不算索引違反")
    a("")
    a("【指標 2】主 context 探勘負載")
    a(f"  用過 subagent 的 session：{m2['sessions_using_subagent']}/{c['main_sessions']}"
      f"（{m2['pct_sessions_using_subagent']}%）｜Agent 呼叫 {m2['agent_calls_total']}")
    a(f"  「該派卻硬做」命中 {m2['flagged_sessions']} 個 session")
    a("")
    a("【指標 3】context 膨脹成本")
    a(f"  cache_read {m3['cache_read_total']:,} = 全部 token 的 {m3['pct_of_all_tokens']}%")
    pk = m3["cache_read_peak_sample_percentiles"]
    if m3["threshold_p90"]:
        a(f"  單 session 峰值 p90={m3['threshold_p90']:,}")
    elif pk:
        a(f"  單 session 峰值 median={pk['median']:,.0f}"
          f"（樣本 n={pk['n']} < {MIN_DIST_N}，不給門檻）")
    else:
        a("  （無資料）")
    a(f"  compaction 事件 {m3['compact_events']} 次，分布在 {m3['sessions_with_compact']} 個 session"
      f"（覆蓋率低時此項僅供輔助）")
    a("")
    a("【指標 4】session 使用型態")
    ah = m4["active_hours_sample_percentiles"]
    sg = m4["segment_count_sample_percentiles"]
    if ah:
        a(f"  實際使用時長(h，work-segment 加總) median={ah['median']} "
          f"p90={ah['p90']} max={ah['max']}")
    if sg:
        a(f"  工作段數 median={sg['median']} p90={sg['p90']} max={sg['max']}"
          f" ｜被 resume 過的 session {m4['resumed_sessions']} 個")
    a("  ⚠️ 「還開著／未封存」須另查 list_sessions（isRunning / isArchived），本腳本不推算")
    a("")
    a("【指標 5】重複操作率（輔助）")
    dd = m5["dup_read_rate_sample_percentiles"]
    if dd:
        a(f"  重讀率 median={dd['median']} p75={dd['p75']} p90={dd['p90']}"
          f" ｜>0.5 者 {m5['flagged_sessions']} 個")
    else:
        a("  樣本不足")
    a("")
    a("工具使用 Top：" + "、".join(f"{n}={v:,}" for n, v in rep["tool_top"][:8]))
    if rep["mcp_top"]:
        a("MCP 使用 Top：" + "、".join(f"{n}={v}" for n, v in rep["mcp_top"][:8]))
    a("=" * 62)
    return "\n".join(L)


def build_host_context(root: Path, extra_roots, explicit_sid) -> dict:
    """
    主機能力自檢。**跑任何指標之前先跑這個。**

    目的是讓「量不到」與「量到 0」分開 —— 這兩件事在 v0.1 長得一模一樣，
    處置卻完全相反。
    """
    patterns = load_index_patterns()
    servers = discover_host_mcp_servers()
    index_servers = sorted(s for s in servers if any(p in s for p in patterns))
    sid, sid_source = resolve_session_id(explicit_sid)
    repo_roots = discover_repo_roots(root, extra_roots)

    return {
        "projects_root": str(root),
        "index_patterns": patterns,
        "mcp_servers_seen": len(servers),
        "index_servers_configured": index_servers,
        "session_id_resolved": bool(sid),
        "session_id_source": sid_source or "UNRESOLVED",
        "repo_roots_found": repo_roots,
        "platform": sys.platform,
    }


def render_preflight(host: dict) -> str:
    L = ["=" * 62, "Preflight — 這台主機量得到什麼、量不到什麼", "=" * 62]
    a = L.append
    a(f"平台            {host['platform']}")
    a(f"projects 目錄   {host['projects_root']}")
    a(f"MCP server 總數 {host['mcp_servers_seen']}")
    idx = host["index_servers_configured"]
    if idx:
        a(f"索引型 MCP      CONFIGURED：{', '.join(idx)}")
        a("                → index_use = 0 時，那是真的沒在用（行為問題）")
    else:
        a("索引型 MCP      UNKNOWN：主機沒有任何索引型 MCP")
        a("                → 指標 1 的硬旗標不成立，不得據此判行為問題")
        a(f"                → 若這台其實有，但名稱不在樣式清單裡，請擴充 {PATTERNS_FILE.name}")
    a(f"當前 session id {host['session_id_source']}")
    if not host["session_id_resolved"]:
        a("                ⚠️ 解不出來 → --exclude-current 無法生效，")
        a("                   掃描行為會汙染自己的統計。請改用 --session-id。")
    roots = host["repo_roots_found"]
    a(f"repo roots      {len(roots)} 個")
    for r in roots[:10]:
        a(f"                  {r}")
    if len(roots) > 10:
        a(f"                  …其餘 {len(roots) - 10} 個")
    a("=" * 62)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="掃描 Claude Code session 紀錄，產出五個行為指標")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="projects 目錄")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    ap.add_argument("--preflight", action="store_true",
                    help="只做主機能力自檢（量得到什麼／量不到什麼），不跑指標")
    ap.add_argument("--exclude-current", action="store_true",
                    help="排除當前 session（避免掃描行為自己汙染統計）")
    ap.add_argument("--session-id", default=None,
                    help="明確指定當前 session id（環境變數解不出來時的逃生門）")
    ap.add_argument("--repo-root", action="append", default=[],
                    help="額外的 git repo 根目錄，可重複指定")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        sys.exit(f"[ERROR] 找不到 {root}")

    host = build_host_context(root, args.repo_root, args.session_id)

    if args.preflight:
        print(json.dumps(host, ensure_ascii=False, indent=2)
              if args.json else render_preflight(host))
        return

    exclude = set()
    if args.exclude_current:
        # ⛔ 大聲失敗。v0.1 在這裡解不出 id 就默默跳過，結果是旗標看似生效、
        #    實際沒作用，掃描用的 session 汙染了自己的統計而沒有任何提示。
        sid, source = resolve_session_id(args.session_id)
        if not sid:
            sys.exit(
                "[ERROR] --exclude-current 需要當前 session id，但解不出來。\n"
                "        已嘗試：--session-id、$CLAUDE_SESSION_ID、$CLAUDE_CODE_HOST_SESSION_ID\n"
                "        請改用 --session-id <id>，或拿掉 --exclude-current 並接受統計含本次掃描。\n"
                "        （不會靜默略過 —— 靜默略過會讓你以為排除掉了。）"
            )
        exclude.add(sid)
        host["excluded_session_source"] = source

    mains, subs = collect(root, exclude, host["repo_roots_found"], host["index_patterns"])
    if not mains and not subs:
        sys.exit("[ERROR] 沒有掃到任何 session 紀錄")

    rep = build_report(mains, subs, host)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else render(rep))


if __name__ == "__main__":
    main()
