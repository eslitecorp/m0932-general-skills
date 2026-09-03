#!/usr/bin/env python3
"""
從近期 session 語料挑出降載實驗的任務集。

    python3 select_task_set.py [--n 10] [--days 30] [--root <projects 目錄>] [--json]

為什麼要有這支腳本：`audit-standard.md` §5 要求**挑選規則與挑完的清單都要預先登記**，
而「偶發超大負載被平滑掉」與「任務集太簡單造成偽陰性」是 skill 邊界裡明列的兩種誤判。
人工挑會挑到自己記得的那幾個；規則化挑才擋得住那個偏差。

三條設計約束：

1. ⛔ **隱私**：只輸出 session id、工具用量、行數、時間。
   **不輸出任何對話內容、檔案路徑、專案名稱或 session 標題。**
2. ⛔ **決定性**：不用 random，排序鍵完全由資料決定。同一份語料重跑必得同一份清單 ——
   否則「挑選規則已登記」是假的。
3. ⛔ **覆蓋優先於量**：先保證每個主要工作型態都有代表，再補到 n。
   只取最重的 n 個會挑出一堆同型任務（skill 邊界的「任務集太簡單」的反面：太單一）。

工作型態由**工具組成**判定，不看內容 —— 這既是隱私要求，也比讀標題可靠
（標題是人寫的，工具組成是行為留下的）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".claude" / "projects"

# 工作型態 → 判定它的工具。順序有意義：由最具辨識力的排到最通用的。
# ⛔ 不要用 session 標題或檔案路徑判型態 —— 那會帶出業務資訊，而且沒有工具組成可靠。
WORK_TYPES = [
    ("程式碼探勘與修改", {"Edit", "Write", "Read", "Grep", "Glob"}),
    ("code review", {"mcp__gitlab__get_merge_request_diffs",
                     "mcp__gitlab__get_merge_request",
                     "mcp__code-review-graph__get_review_context_tool"}),
    ("票務與 GTD", {"mcp__youtrack__search_issues", "mcp__youtrack__get_issue",
                    "TaskUpdate"}),
    ("文件與報告產出", {"Write", "mcp__606dcce5-57b4-470a-9274-dfca397e9c00__content_read"}),
    ("網路研究", {"WebFetch", "WebSearch"}),
    ("環境與腳本操作", {"Bash"}),
]


def classify(tools: Counter) -> str:
    """回傳最能代表這個 session 的工作型態。取交集佔比最高者，平手時取表中較前者。"""
    total = sum(tools.values()) or 1
    best, best_share = "未分類", 0.0
    for name, keys in WORK_TYPES:
        share = sum(v for k, v in tools.items() if k in keys) / total
        if share > best_share:
            best, best_share = name, share
    return best if best_share > 0 else "未分類"


def scan(root: Path, days: int) -> list[dict]:
    """只取統計量。⛔ 不讀任何 message content。"""
    cutoff = None
    sessions = []
    for path in sorted(root.rglob("*.jsonl")):
        if "subagents" in path.parts:
            continue
        tools, lines, stamps = Counter(), 0, []
        sid = path.stem
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    lines += 1
                    if '"tool_use"' not in line and '"timestamp"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    ts = rec.get("timestamp")
                    if isinstance(ts, str):
                        try:
                            stamps.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
                        except ValueError:
                            pass
                    msg = rec.get("message")
                    if not isinstance(msg, dict):
                        continue
                    content = msg.get("content")
                    if not isinstance(content, list):
                        continue
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name"):
                            tools[b["name"]] += 1
        except OSError:
            continue
        if not stamps or not tools:
            continue
        last = max(stamps)
        if cutoff is None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days) \
                if days else None
        sessions.append({
            "session_id": sid,
            "last_activity": last.isoformat(),
            "lines": lines,
            "tool_calls": sum(tools.values()),
            "distinct_tools": len(tools),
            "work_type": classify(tools),
            "top_tools": [t for t, _ in tools.most_common(4)],
        })
    if cutoff:
        sessions = [s for s in sessions
                    if datetime.fromisoformat(s["last_activity"]) >= cutoff]
    return sessions


def select(sessions: list[dict], n: int) -> dict:
    """
    挑選規則（**這段就是要登記的規則本體，改動它必須是新一次預先登記**）：

    1. 依 `tool_calls` 遞減、`session_id` 遞增排序（決定性，無 random）
    2. **覆蓋階段**：每個出現過的工作型態，各取該型態內 `tool_calls` 最高的一個
    3. **補足階段**：**跨型態輪替**，每輪各型態取一個尚未選入且最重的，直到 n 個
    4. 不足 n 個就回報實際數量並標 `insufficient: true`
       —— ⛔ 不得放寬 days 或降低 n 來湊數（那會變成挑符合條件的樣本）

    📌 補足階段為什麼要輪替而不是「照 tool_calls 補」：
       照 tool_calls 補會把名額全給最重的那一型（實測第一版把 3 個名額全填成
       同一型，10 個裡有 4 個同型）。那正是 `SKILL.md` 邊界裡
       「降載實驗偽陰性（任務集太簡單）」的另一面 —— 不是太簡單，是**太單一**：
       型態專屬的失敗就看不到了。輪替同時保留了「每型態內取最重的」，
       所以敏感度沒有犧牲。
    """
    ordered = sorted(sessions, key=lambda s: (-s["tool_calls"], s["session_id"]))
    picked, seen_types = [], set()
    for s in ordered:                      # 覆蓋階段
        if s["work_type"] not in seen_types:
            picked.append(dict(s, picked_because=f"覆蓋工作型態「{s['work_type']}」"))
            seen_types.add(s["work_type"])

    # 補足階段：跨型態輪替，型態順序固定（依該型態最重者的 tool_calls 遞減）
    chosen = {p["session_id"] for p in picked}
    by_type: dict[str, list] = {}
    for s in ordered:
        if s["session_id"] not in chosen:
            by_type.setdefault(s["work_type"], []).append(s)
    type_order = sorted(by_type, key=lambda t: (-by_type[t][0]["tool_calls"], t))
    round_no = 0
    while len(picked) < n and any(by_type.values()):
        progressed = False
        for t in type_order:
            if len(picked) >= n:
                break
            if by_type.get(t):
                s = by_type[t].pop(0)
                picked.append(dict(s, picked_because=(
                    f"補足（跨型態輪替第 {round_no + 1} 輪，型態「{t}」內最重者）")))
                progressed = True
        if not progressed:
            break
        round_no += 1
    return {
        "n_requested": n,
        "n_selected": len(picked),
        "insufficient": len(picked) < n,
        "work_types_covered": sorted(seen_types),
        "pool_size": len(sessions),
        "tasks": picked[:max(n, len(seen_types))],
        "_rule": "①tool_calls 遞減 + session_id 遞增排序 "
                 "②覆蓋階段：每個工作型態各取該型態內最重的一個 "
                 "③補足階段：跨型態輪替，每輪各型態取尚未選入且最重的，直到 n。"
                 "決定性、無 random。⛔ 改動規則須新一次預先登記",
        "_privacy": "只含 session id／工具名／計數／時間。無對話內容、無路徑、無標題",
    }


def main():
    ap = argparse.ArgumentParser(description="挑降載實驗的任務集（規則化，決定性）")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--days", type=int, default=30,
                    help="只看最近幾天（0 = 不限）。⛔ 不得為了湊數放寬")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        sys.exit(f"[ERROR] 找不到 {root}")

    res = select(scan(root, args.days), args.n)

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    print("=" * 62)
    print(f"降載實驗任務集 — 取 {res['n_selected']}／{res['n_requested']} 個"
          f"（母體 {res['pool_size']} 個 session，最近 {args.days} 天）")
    print("=" * 62)
    if res["insufficient"]:
        print(f"⚠️ 不足 {res['n_requested']} 個。⛔ 不得放寬 --days 或降低 --n 來湊數 ——")
        print("   那會把「規則化挑選」變成「挑符合條件的樣本」。照實回報數量並限縮結論。")
    print(f"涵蓋工作型態：{', '.join(res['work_types_covered'])}")
    print()
    for i, t in enumerate(res["tasks"], 1):
        print(f"{i:2}. {t['session_id']}")
        print(f"    型態 {t['work_type']}｜工具呼叫 {t['tool_calls']}｜"
              f"行數 {t['lines']}｜最後活動 {t['last_activity'][:16]}")
        print(f"    主要工具 {', '.join(t['top_tools'])}")
        print(f"    選入理由 {t['picked_because']}")
    print("=" * 62)
    print("⛔ 這份清單要與挑選規則一起 commit（audit-standard.md §5）。")
    print("   事後換任務集必須是新一次預先登記，舊的留在歷史上。")
    print("=" * 62)


if __name__ == "__main__":
    main()
