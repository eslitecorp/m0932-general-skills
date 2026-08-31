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
INDEX_MCP_PREFIXES = (
    "mcp__code-review-graph__",
    "mcp__semble__",
)
INDEX_MCP_EXACT = {"mcp__gitlab__semantic_code_search"}

# work segment 切分門檻：事件間隔超過這個秒數就視為換了一段工作
SEGMENT_GAP_SEC = 30 * 60

# 程式碼副檔名：用來判斷一次探勘是不是「程式碼探勘」
CODE_EXT_RE = re.compile(
    r"\.(go|rb|py|ts|tsx|js|jsx|java|kt|swift|rs|c|h|cc|cpp|php|scala|ex|exs|sql|proto)\b"
)


def discover_repo_roots(extra_cwds=()) -> list[str]:
    """
    找出本機的 git repo 根目錄。

    ⚠️ 為什麼不用 session 的 cwd 當閘門：索引型 MCP 可以從任何 cwd 對任何 repo 查詢，
    而在非 repo 目錄底下 grep 一個 repo 裡的檔案，同樣是「程式碼探勘」。
    用 cwd 判定會把這類漏掉 —— 實測 code-review-graph 的 39 次呼叫全部發生在
    cwd 不是 repo 的 session 裡，若以 cwd 為閘門會得出「code repo session 索引使用 0」
    這種既真實又誤導的結論。
    """
    roots = set()
    for base in (Path.home() / "Work" / "Code", Path.home() / "Work", Path.home()):
        if not base.is_dir():
            continue
        try:
            for child in base.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    roots.add(str(child.resolve()))
        except OSError:
            continue
    for cwd in extra_cwds:
        if cwd and (Path(cwd) / ".git").exists():
            roots.add(str(Path(cwd).resolve()))
    return sorted(roots, key=len, reverse=True)


# --- 工具函式 --------------------------------------------------------------

def parse_ts(raw):
    """ISO8601 → aware datetime；失敗回 None（不拋，避免單筆壞資料中斷全量掃描）。"""
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_index_tool(name: str) -> bool:
    return name in INDEX_MCP_EXACT or name.startswith(INDEX_MCP_PREFIXES)


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


def scan_file(path: Path, is_subagent: bool, exclude_ids: set, repo_roots) -> SessionStat | None:
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

                if is_index_tool(name):
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

def collect(root: Path, exclude_ids: set, repo_roots):
    mains, subs = [], []
    for path in sorted(root.rglob("*.jsonl")):
        is_sub = "subagents" in path.parts
        st = scan_file(path, is_sub, exclude_ids, repo_roots)
        if st is None:
            continue
        (subs if is_sub else mains).append(st)
    return mains, subs


def build_report(mains, subs):
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
        }

    return {
        "corpus": {
            "main_sessions": len(mains),
            "subagent_files": len(subs),
            "total_lines": sum(s.lines for s in all_st),
            "parse_errors": sum(s.parse_errors for s in all_st),
            "code_repo_sessions": len(code_sessions),
        },
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
                "index_use": code_index,
                "bypass_ratio": round(code_explore / max(code_index, 1), 1),
                "sessions_with_code_explore": sum(1 for s in mains if s.explore_code > 0),
                "sessions_using_index": len(sessions_with_index),
                "per_session_ratio_dist": pct(ratios),
                "threshold_p75": round(percentile(ratios, 75), 2) if ratios else None,
                "threshold_p90": round(percentile(ratios, 90), 2) if ratios else None,
                "flagged_sessions": len(flag_no_index),
            },
            "non_code_exploration": {
                "explore_cost": noncode_explore,
                "note": "管理／文件類探勘，索引型 MCP 不適用；"
                        "這部分的 context 成本由指標 2（delegate 給 subagent）處理",
            },
            "index_use_total_all": index_total,
            "sessions_using_index_all": len(sessions_with_index),
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
            "cache_read_peak_dist": pct(peaks),
            "threshold_p90": int(percentile(peaks, 90)) if peaks else None,
            "compact_events": sum(len(s.compact_events) for s in mains),
            "sessions_with_compact": sum(1 for s in mains if s.compact_events),
        },
        "metric_4_session_usage": {
            "_note": "「還開著／未封存」的權威來源是 list_sessions 的 isRunning / isArchived，"
                     "不在本腳本範圍。以 mtime 推算拖尾已驗證不可靠（中位數 184.6h 無意義），已移除",
            "active_hours_dist": pct(actives),
            "segment_count_dist": pct(segs),
            "resumed_sessions": sum(1 for s in mains if s.segment_count > 1),
        },
        "metric_5_redundant_read": {
            "dup_read_rate_dist": pct(dups),
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
    a(f"其中 code repo session：{c['code_repo_sessions']}")
    a("")
    cr, nc = m1["code_exploration"], m1["non_code_exploration"]
    a("【指標 1】探勘繞道率")
    a(f"  全體探勘量 {m1['explore_cost_all']:,}"
      f"（直接工具 {m1['explore_direct']:,}／繞道 Bash {m1['explore_via_bash']:,}"
      f" = {m1['bash_share_of_explore']:.0%}）")
    a(f"  ── 程式碼探勘（索引指標適用）：{cr['explore_cost']:,} 次"
      f"／索引查詢 {cr['index_use']:,} 次  →  繞道率 {cr['bypass_ratio']}:1")
    a(f"     有做程式碼探勘的 session {cr['sessions_with_code_explore']}"
      f"／其中用過索引型 MCP {cr['sessions_using_index']}")
    a(f"     門檻 p75={cr['threshold_p75']} p90={cr['threshold_p90']}"
      f" ｜硬旗標命中 {cr['flagged_sessions']} 個 session")
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
    a(f"  單 session 峰值 p90={m3['threshold_p90']:,}" if m3["threshold_p90"] else "  （無資料）")
    a(f"  compaction 事件 {m3['compact_events']} 次，分布在 {m3['sessions_with_compact']} 個 session"
      f"（覆蓋率低時此項僅供輔助）")
    a("")
    a("【指標 4】session 使用型態")
    ah, sg = m4["active_hours_dist"], m4["segment_count_dist"]
    if ah:
        a(f"  實際使用時長(h，work-segment 加總) median={ah['median']} "
          f"p90={ah['p90']} max={ah['max']}")
    if sg:
        a(f"  工作段數 median={sg['median']} p90={sg['p90']} max={sg['max']}"
          f" ｜被 resume 過的 session {m4['resumed_sessions']} 個")
    a("  ⚠️ 「還開著／未封存」須另查 list_sessions（isRunning / isArchived），本腳本不推算")
    a("")
    a("【指標 5】重複操作率（輔助）")
    dd = m5["dup_read_rate_dist"]
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


def main():
    ap = argparse.ArgumentParser(description="掃描 Claude Code session 紀錄，產出五個行為指標")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="projects 目錄")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    ap.add_argument("--exclude-current", action="store_true",
                    help="排除當前 session（避免掃描行為自己汙染統計）")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        sys.exit(f"[ERROR] 找不到 {root}")

    exclude = set()
    if args.exclude_current:
        cur = os.environ.get("CLAUDE_SESSION_ID")
        if cur:
            exclude.add(cur)

    repo_roots = discover_repo_roots()
    mains, subs = collect(root, exclude, repo_roots)
    if not mains and not subs:
        sys.exit("[ERROR] 沒有掃到任何 session 紀錄")

    rep = build_report(mains, subs)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else render(rep))


if __name__ == "__main__":
    main()
