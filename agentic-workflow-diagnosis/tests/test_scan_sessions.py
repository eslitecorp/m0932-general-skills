#!/usr/bin/env python3
"""
scan_sessions.py 的回歸測試。

每個 case 對應 portability.md 裡的一類跨裝置失效。**新增失效類型時要同時新增 case**，
否則下一台機器會再踩一次同樣的坑。

只用 stdlib unittest，不引入任何依賴 —— 加了 requirements.txt 會觸發
validate_skill.py 的 dependabot 覆蓋檢查，而這裡不需要第三方套件。

跑法：
    python3 -m unittest discover agentic-workflow-diagnosis/tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import scan_sessions as ss  # noqa: E402


# --- fixture 產生器 --------------------------------------------------------

def rec(**kw):
    """一筆 assistant 紀錄。只放腳本會讀的欄位。"""
    base = {
        "type": "assistant",
        "sessionId": kw.pop("sid", "s-fixture"),
        "cwd": kw.pop("cwd", "/tmp/fixture"),
        "gitBranch": kw.pop("branch", "main"),
        "timestamp": kw.pop("ts", "2026-09-01T00:00:00.000Z"),
        "isSidechain": kw.pop("sidechain", False),
    }
    base.update(kw)
    return base


def tool_use(name, **tin):
    return {"message": {"content": [{"type": "tool_use", "name": name, "input": tin}]}}


def write_corpus(root: Path, project: str, session: str, records) -> Path:
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session}.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def scan(root: Path, host=None, patterns=None):
    """跑完整流程，回傳 report。"""
    patterns = patterns if patterns is not None else ss.DEFAULT_INDEX_MCP_PATTERNS
    host = host if host is not None else {"index_servers_configured": ["codebase-memory"]}
    mains, subs = ss.collect(root, set(), [], patterns)
    return ss.build_report(mains, subs, host)


class TmpCorpus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


# --- A1：索引白名單寫死 ----------------------------------------------------

class TestIndexDiscovery(TmpCorpus):
    """
    A1：v0.1 把「主機沒裝索引型 MCP」與「裝了從沒呼叫」壓成同一個 0，
    但兩者的處置完全相反 —— 前者不是行為問題，後者是。
    """

    def _corpus_with(self, tool_name):
        recs = [rec(**tool_use("Read", file_path="/repo/a.py")) for _ in range(25)]
        if tool_name:
            recs.append(rec(**tool_use(tool_name, query="x")))
        write_corpus(self.root, "-repo", "s1", recs)

    def test_default_whitelist_server_counted(self):
        self._corpus_with("mcp__code-review-graph__get_impact_radius")
        r = scan(self.root)
        self.assertEqual(r["metric_1_index_bypass"]["code_exploration"]["index_use"], 1)

    def test_other_host_index_server_also_counted(self):
        """換一台機器換一套索引工具，不該因為名字不同就變成 0。"""
        self._corpus_with("mcp__codebase-memory__search_graph")
        r = scan(self.root)
        self.assertEqual(r["metric_1_index_bypass"]["code_exploration"]["index_use"], 1)

    def test_user_extended_pattern_counted(self):
        """使用者在 index-mcp-patterns.json 加的樣式要生效。"""
        self._corpus_with("mcp__my-private-indexer__lookup")
        r = scan(self.root, patterns=ss.DEFAULT_INDEX_MCP_PATTERNS + ["my-private-indexer"])
        self.assertEqual(r["metric_1_index_bypass"]["code_exploration"]["index_use"], 1)

    def test_no_index_mcp_on_host_is_unknown_not_zero(self):
        """主機沒裝 → UNKNOWN，且**不得**命中硬旗標。"""
        self._corpus_with(None)
        r = scan(self.root, host={"index_servers_configured": []})
        code = r["metric_1_index_bypass"]["code_exploration"]
        self.assertEqual(code["index_state"], "UNKNOWN")
        self.assertEqual(code["index_use"], "UNKNOWN")
        self.assertEqual(code["bypass_ratio"], "UNKNOWN")
        self.assertEqual(code["flagged_sessions"], 0)

    def test_configured_but_never_called_is_zero_and_flagged(self):
        """裝了卻沒用 → 0 且命中旗標。這才是行為問題。"""
        self._corpus_with(None)
        r = scan(self.root, host={"index_servers_configured": ["codebase-memory"]})
        code = r["metric_1_index_bypass"]["code_exploration"]
        self.assertEqual(code["index_state"], "CONFIGURED")
        self.assertEqual(code["index_use"], 0)
        self.assertEqual(code["flagged_sessions"], 1)

    def test_host_context_unavailable_is_unknown(self):
        """拿不到主機設定時也不能假裝是 0。"""
        self._corpus_with(None)
        r = scan(self.root, host={})
        self.assertEqual(
            r["metric_1_index_bypass"]["code_exploration"]["index_state"], "UNKNOWN"
        )

    def test_non_mcp_tool_never_counts_as_index(self):
        write_corpus(self.root, "-repo", "s1",
                     [rec(**tool_use("Read", file_path="/repo/codebase-memory.py"))])
        r = scan(self.root)
        self.assertEqual(r["metric_1_index_bypass"]["code_exploration"]["index_use"], 0)


# --- A2：session id 環境變數不可攜 ------------------------------------------

class TestSessionIdResolution(unittest.TestCase):
    """A2：v0.1 只認 CLAUDE_SESSION_ID，別台機器上該變數不存在 → 靜默無作用。"""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_explicit_arg_wins(self):
        os.environ["CLAUDE_SESSION_ID"] = "from-env"
        self.assertEqual(ss.resolve_session_id("explicit"), ("explicit", "--session-id"))

    def test_primary_env_var(self):
        os.environ["CLAUDE_SESSION_ID"] = "abc"
        self.assertEqual(ss.resolve_session_id(None), ("abc", "CLAUDE_SESSION_ID"))

    def test_fallback_env_var(self):
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = "def"
        self.assertEqual(
            ss.resolve_session_id(None), ("def", "CLAUDE_CODE_HOST_SESSION_ID")
        )

    def test_unresolvable_returns_none(self):
        self.assertEqual(ss.resolve_session_id(None), (None, ""))

    def test_exclude_current_fails_loudly_when_unresolvable(self):
        """⛔ 解不出來必須非 0 退出。靜默略過會讓人以為排除掉了。"""
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID")}
        with tempfile.TemporaryDirectory() as tmp:
            write_corpus(Path(tmp), "-repo", "s1", [rec(**tool_use("Read", file_path="/a.py"))])
            out = subprocess.run(
                [sys.executable, str(SCRIPTS / "scan_sessions.py"),
                 "--root", tmp, "--exclude-current"],
                capture_output=True, text=True, env=env,
            )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("--session-id", out.stdout + out.stderr)

    def test_exclude_current_actually_excludes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_corpus(root, "-repo", "keep", [rec(sid="keep", **tool_use("Read", file_path="/a.py"))])
            write_corpus(root, "-repo", "drop", [rec(sid="drop", **tool_use("Read", file_path="/b.py"))])
            mains, _ = ss.collect(root, {"drop"}, [], ss.DEFAULT_INDEX_MCP_PATTERNS)
        self.assertEqual([s.session_id for s in mains], ["keep"])


# --- B1：repo root 路徑寫死 ------------------------------------------------

class TestRepoRootDiscovery(unittest.TestCase):
    """B1：v0.1 寫死 ~/Work/Code 等三個 base 且只掃 depth 1。"""

    def test_decode_project_dir(self):
        self.assertEqual(
            ss.decode_project_dir("-Users-alice-Work-foo"), "/Users/alice/Work/foo"
        )

    def test_decode_rejects_non_encoded(self):
        self.assertEqual(ss.decode_project_dir("plain"), "")

    def test_deep_repo_target_is_code(self):
        """深層 repo（depth 3）底下的探勘仍要算成程式碼探勘。"""
        deep = "/Users/alice/Work/proj/codebase/svc"
        self.assertTrue(ss.is_code_target(f"grep -r x {deep}/main.go", [deep]))

    def test_code_extension_without_repo_root(self):
        self.assertTrue(ss.is_code_target("/anywhere/x.ts", []))

    def test_plain_text_target_is_not_code(self):
        self.assertFalse(ss.is_code_target("/anywhere/notes.md", []))

    def test_home_scan_finds_nested_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            deep = home / "Work" / "proj" / "codebase" / "svc"
            (deep / ".git").mkdir(parents=True)
            projects = home / ".claude" / "projects"
            projects.mkdir(parents=True)
            saved = os.environ.get("HOME")
            os.environ["HOME"] = str(home)
            try:
                roots = ss.discover_repo_roots(projects)
            finally:
                if saved is not None:
                    os.environ["HOME"] = saved
        self.assertIn(str(deep), roots)


# --- C1：門檻自引 ----------------------------------------------------------

class TestThresholdGuard(TmpCorpus):
    """C1：p75/p90 由正在被評判的同一份小樣本算出，卻印成「門檻」。"""

    def _n_sessions(self, n):
        for i in range(n):
            write_corpus(
                self.root, "-repo", f"s{i}",
                [rec(sid=f"s{i}", **tool_use("Read", file_path=f"/repo/f{i}.py"))
                 for _ in range(3)],
            )

    def test_small_sample_yields_no_threshold(self):
        self._n_sessions(4)
        code = scan(self.root)["metric_1_index_bypass"]["code_exploration"]
        self.assertIsNone(code["threshold_p75"])
        self.assertIsNone(code["threshold_p90"])

    def test_small_sample_still_reports_percentiles_with_n(self):
        self._n_sessions(4)
        dist = (scan(self.root)["metric_1_index_bypass"]["code_exploration"]
                ["per_session_ratio_sample_percentiles"])
        self.assertEqual(dist["n"], 4)
        self.assertFalse(dist["is_threshold_grade"])

    def test_large_sample_yields_threshold(self):
        self._n_sessions(ss.MIN_DIST_N + 2)
        code = scan(self.root)["metric_1_index_bypass"]["code_exploration"]
        self.assertIsNotNone(code["threshold_p75"])
        self.assertTrue(
            code["per_session_ratio_sample_percentiles"]["is_threshold_grade"]
        )

    def test_empty_corpus_percentiles_are_empty(self):
        write_corpus(self.root, "-repo", "s1", [rec(**tool_use("Write", file_path="/a.py"))])
        code = scan(self.root)["metric_1_index_bypass"]["code_exploration"]
        self.assertEqual(code["per_session_ratio_sample_percentiles"], {})
        self.assertIsNone(code["threshold_p90"])


# --- C2：強度正規化 --------------------------------------------------------

class TestIntensityNormalization(TmpCorpus):
    """C2：絕對值門檻在 207 session 與 7 session 的機器上意義不同。"""

    def test_per_session_denominator(self):
        for i in range(2):
            write_corpus(
                self.root, "-repo", f"s{i}",
                [rec(sid=f"s{i}", **tool_use("Read", file_path="/repo/a.py"))
                 for _ in range(10)],
            )
        inten = scan(self.root)["metric_1_index_bypass"]["intensity_normalized"]
        self.assertEqual(inten["explore_per_session"], 10.0)

    def test_zero_active_hours_is_unknown_not_division_error(self):
        write_corpus(self.root, "-repo", "s1",
                     [rec(**tool_use("Read", file_path="/repo/a.py"))])
        inten = scan(self.root)["metric_1_index_bypass"]["intensity_normalized"]
        self.assertEqual(inten["explore_per_active_hour"], "UNKNOWN")


# --- C3：語料截斷 ----------------------------------------------------------

class TestCorpusTruncation(TmpCorpus):
    """C3：保留期會靜默截斷歷史；偵測到要聲明，⛔ 不得建議延長保留期。"""

    def _corpus_spanning(self, days_ago):
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        write_corpus(self.root, "-repo", "s1",
                     [rec(ts=ts, **tool_use("Read", file_path="/repo/a.py"))])

    def test_old_corpus_flagged_truncated(self):
        self._corpus_spanning(200)
        self.assertIs(scan(self.root)["corpus"]["corpus_truncated"], True)

    def test_recent_corpus_not_flagged(self):
        self._corpus_spanning(1)
        self.assertIs(scan(self.root)["corpus"]["corpus_truncated"], False)

    def test_truncation_note_never_suggests_changing_retention(self):
        """R2：不得要求機器改設定來配合工具。"""
        self._corpus_spanning(200)
        note = scan(self.root)["corpus"]["_note"]
        self.assertIn("不要", note)
        self.assertIn("延長保留期", note)
        self.assertIn("live probe", note)

    def test_no_timestamps_is_unknown(self):
        write_corpus(self.root, "-repo", "s1",
                     [{"type": "assistant", "sessionId": "s1",
                       "message": {"content": [
                           {"type": "tool_use", "name": "Read", "input": {"file_path": "/a.py"}}]}}])
        self.assertEqual(scan(self.root)["corpus"]["corpus_truncated"], "UNKNOWN")


# --- 既有陷阱：不可回歸 ----------------------------------------------------

class TestExistingTrapsStillHold(TmpCorpus):
    """v0.1 已經踩過並修好的坑。可攜性修正不得把它們弄回來。"""

    def test_head_branch_is_not_code_repo(self):
        """非 git 目錄的 gitBranch 是字面值 "HEAD"，不是空字串。"""
        write_corpus(self.root, "-x", "s1",
                     [rec(branch="HEAD", **tool_use("Read", file_path="/a.md"))])
        self.assertEqual(scan(self.root)["corpus"]["code_repo_sessions"], 0)

    def test_real_branch_is_code_repo(self):
        write_corpus(self.root, "-x", "s1",
                     [rec(branch="main", **tool_use("Read", file_path="/a.md"))])
        self.assertEqual(scan(self.root)["corpus"]["code_repo_sessions"], 1)

    def test_resume_gap_does_not_inflate_active_hours(self):
        """原始 max-min 最大達 774 小時，那是 resume 造成的假象。"""
        t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
        stamps = [t0, t0 + timedelta(minutes=10),
                  t0 + timedelta(days=32), t0 + timedelta(days=32, minutes=10)]
        write_corpus(self.root, "-x", "s1",
                     [rec(ts=t.isoformat(), **tool_use("Read", file_path="/a.py"))
                      for t in stamps])
        dist = scan(self.root)["metric_4_session_usage"]["active_hours_sample_percentiles"]
        self.assertLess(dist["max"], 1.0)          # 20 分鐘，不是 768 小時

    def test_bash_exploration_is_counted(self):
        """只數 Read/Grep/Glob 會低估約 8 倍 —— 探勘量必須從 Bash 命令裡撈。"""
        write_corpus(self.root, "-x", "s1",
                     [rec(**tool_use("Bash", command="grep -r foo /repo/main.go"))])
        self.assertEqual(scan(self.root)["metric_1_index_bypass"]["explore_via_bash"], 1)

    def test_bash_verb_inside_argument_not_counted(self):
        """`git commit -m "cat"` 不是探勘。"""
        write_corpus(self.root, "-x", "s1",
                     [rec(**tool_use("Bash", command='git commit -m "cat"'))])
        self.assertEqual(scan(self.root)["metric_1_index_bypass"]["explore_via_bash"], 0)


# --- 隱私：硬要求 ----------------------------------------------------------

class TestPrivacy(TmpCorpus):
    """腳本只輸出統計量。跑在別人機器上時這是硬要求。"""

    def test_no_prompt_text_or_paths_in_output(self):
        secret_path = "/Users/someone/private/salary.py"
        write_corpus(self.root, "-x", "s1", [
            rec(**tool_use("Read", file_path=secret_path)),
            rec(**tool_use("Bash", command=f"cat {secret_path}")),
            {"type": "user", "sessionId": "s1",
             "message": {"content": "機密業務內容不得外流"}},
        ])
        blob = json.dumps(scan(self.root), ensure_ascii=False)
        self.assertNotIn(secret_path, blob)
        self.assertNotIn("機密業務內容", blob)


if __name__ == "__main__":
    unittest.main()
