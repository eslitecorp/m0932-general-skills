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
UNKNOWN_STR = "UNKNOWN"

# 工作型態 → 判定它的**工具名子字串**（不是完整工具名）。
#
# ⛔ **不要寫死 MCP server 名稱。** 第一版寫了 `mcp__gitlab__get_merge_request_diffs`、
#    `mcp__youtrack__search_issues`，甚至一個本機專屬的 connector UUID
#    （`mcp__606dcce5-…__content_read`）—— 換一台機器那些名字全都不同，
#    對應的工作型態會**靜默永不命中**，分類默默退化成「未分類」而不報錯。
#    這正是 `portability.md` 的 A1（索引 MCP 白名單寫死 server 名稱）**在新腳本裡重犯**。
#
# 修法與 A1 相同：比對**協議層的能力字串**（MCP 工具名把操作編進名字裡，
# 這一段跨機器穩定），內建工具名（Edit／Bash／WebFetch…）本身就是 Claude Code 固定的；
# 再加一個同目錄的外掛檔讓每台機器可以擴充。
WORK_TYPE_PATTERNS = [
    ("code review", ("merge_request", "pull_request", "review_context", "get_diff")),
    ("票務與 GTD", ("issue", "ticket", "TaskUpdate", "work_item")),
    ("文件與報告產出", ("document", "content_read", "content_modify", "page_",
                        "table_rows", "spreadsheet")),
    ("網路研究", ("WebFetch", "WebSearch")),
    ("程式碼探勘與修改", ("Edit", "Write", "Read", "Grep", "Glob")),
    ("環境與腳本操作", ("Bash",)),
]
PATTERNS_FILE = Path(__file__).resolve().parent / "work-type-patterns.json"


def load_work_types() -> list[tuple[str, tuple[str, ...]]]:
    """內建樣式 + 同目錄 work-type-patterns.json 的擴充（擴充不是取代）。"""
    types = [(n, tuple(p)) for n, p in WORK_TYPE_PATTERNS]
    try:
        extra = json.loads(PATTERNS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return types
    merged = {n: list(p) for n, p in types}
    for name, pats in (extra.get("work_types") or {}).items():
        merged.setdefault(name, [])
        merged[name].extend(str(x) for x in pats if x)
    # 保持內建順序在前，外掛新增的型態接在後面（決定性）
    order = [n for n, _ in types] + sorted(set(merged) - {n for n, _ in types})
    return [(n, tuple(dict.fromkeys(merged[n]))) for n in order]


def classify(tools: Counter, work_types=None) -> str:
    """
    回傳最能代表這個 session 的工作型態：**子字串**命中量佔比最高者，平手時取表中較前者。

    ⛔ 不用 session 標題或檔案路徑判型態 —— 那會帶出業務資訊，
       而且沒有工具組成可靠（標題是人寫的，工具組成是行為留下的）。
    """
    work_types = work_types or load_work_types()
    total = sum(tools.values()) or 1
    best, best_share = "未分類", 0.0
    for name, pats in work_types:
        share = sum(v for k, v in tools.items()
                    if any(p in k for p in pats)) / total
        if share > best_share:
            best, best_share = name, share
    return best if best_share > 0 else "未分類"


def scan(root: Path, days: int, exclude: set | None = None) -> list[dict]:
    """只取統計量。⛔ 不讀任何 message content。"""
    cutoff = None
    sessions = []
    exclude = exclude or set()
    work_types = load_work_types()
    for path in sorted(root.rglob("*.jsonl")):
        if "subagents" in path.parts:
            continue
        if path.stem in exclude:          # ⛔ D2：診斷自己的 session 不得被選為任務
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
            "work_type": classify(tools, work_types),
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
    # ⛔ C3：語料可能已被保留期截斷，--days 視窗內看起來完整不代表真的完整。
    #    回報語料的時間範圍讓人看得見視窗，不替他判斷。
    stamps = sorted(s["last_activity"] for s in sessions)
    return {
        "corpus_window": {"oldest": stamps[0] if stamps else UNKNOWN_STR,
                          "newest": stamps[-1] if stamps else UNKNOWN_STR,
                          "_note": "⛔ 若語料已被保留期截斷，這個視窗只是下界。"
                                   "與 list_sessions 的最舊 lastActivityAt 對照才知道落差"},
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
    ap.add_argument("--exclude-current", action="store_true",
                    help="排除診斷自己這個 session（portability.md D2 觀測者效應）")
    ap.add_argument("--session-id", default=None,
                    help="明確指定當前 session id；單獨給定即視同 --exclude-current")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        sys.exit(f"[ERROR] 找不到 {root}")

    # ⛔ D2：不重寫 session id 的解析與驗證 —— 沿用 scan_sessions.py 已修好的那一份
    #    （第 15 類失效：解得出來但解錯，要對著語料驗證）。重寫等於再犯一次。
    exclude = set()
    if args.exclude_current or args.session_id:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import scan_sessions as ss
        except ImportError:
            sys.exit("[ERROR] 找不到同目錄的 scan_sessions.py，"
                     "而 --exclude-current 沿用它的 session id 解析與語料驗證")
        sid, source = ss.resolve_session_id(args.session_id)
        if not sid:
            sys.exit("[ERROR] --exclude-current 需要當前 session id，但解不出來。"
                     "請改用 --session-id <id>。（不靜默略過 —— "
                     "靜默略過會讓你以為排除掉了）")
        if ss.session_id_in_corpus(root, sid) is False:
            sys.exit(f"[ERROR] 解出的 session id 對不上語料裡任何 session，排除不會生效。\n"
                     f"        解到：{sid}（來源 {source}）\n"
                     f"        這是「解得出來但解錯」，處置與解不出來相同。")
        exclude.add(sid)

    res = select(scan(root, args.days, exclude), args.n)
    res["excluded_current_session"] = sorted(exclude) or "未排除（⚠️ 診斷本身可能被選為任務）"

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
    cw = res["corpus_window"]
    print(f"語料視窗：{cw['oldest'][:16]} … {cw['newest'][:16]}"
          f"（⛔ 若已被保留期截斷，這只是下界）")
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
