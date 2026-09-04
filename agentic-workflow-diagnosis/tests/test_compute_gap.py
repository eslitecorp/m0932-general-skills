"""
compute_gap.py 的 fixture 回歸測試。

這一組測試的重點不是「會不會判過」，而是**會不會判不過**：
一個永遠說 yes 的標準沒有公信力。所以每一道閘門都有一組必須判 FAIL／BLOCKED 的輸入。

⛔ 加新條文時一定要一起加它的否證測試。沒有測試的條文，下一台機器會直接繞過它。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import compute_gap as cg  # noqa: E402


# --- fixture 產生器 ---------------------------------------------------------

def decl(**over):
    d = {
        "declared_by": "role-x",
        "declared_at": "2026-09-03",
        "machine": {"model": "TestMac", "ram_gb": 16, "ram_upgradeable": False},
        "concurrency_declared": 5,
        "degradation_definition": "A_task_success_rate",
        "task_set_rule": "從近期語料自動挑，涵蓋主要工作型態",
        "incompressible": [
            {"name": "svc-a", "scales_with_concurrency": True, "per_unit_mb": 80,
             "observation": "關掉後有 3 次工具呼叫失敗紀錄",
             "shared_alternative_reason": "資料落地限制",
             "non_resident_alternative": "http transport"},
        ],
    }
    d.update(over)
    return d


def g0_sample(**states):
    """
    預設：有記憶體證據、無運算證據（→ PASS）。
    ⛔ absent 不是反面證據，只有 present 才算證據。
    """
    base = {k: "present" for k in cg.MEMORY_EVIDENCE}
    base.update({k: "absent" for k in cg.COMPUTE_EVIDENCE})
    base.update(states)
    return {k: {"state": v, "value": "<實測值>"} for k, v in base.items()}


def mem_only(**states):
    return g0_sample(**states)


def cpu_only(**states):
    """有運算證據、無記憶體證據（→ 不受理）。"""
    base = {k: "absent" for k in cg.MEMORY_EVIDENCE}
    base.update({"C1_single_process_saturating": "present",
                 "C2_load_high_without_memory": "absent"})
    base.update(states)
    return {k: {"state": v, "value": "<實測值>"} for k, v in base.items()}


def no_signal():
    """兩種證據都沒有（閒置）。"""
    base = {k: "absent" for k in list(cg.MEMORY_EVIDENCE) + list(cg.COMPUTE_EVIDENCE)}
    return {k: {"state": v, "value": "<實測值>"} for k, v in base.items()}


def meas(**over):
    m = {
        "machine": {"model": "TestMac", "ram_gb": 16},
        "vm": {"swap_total_mb": 8192, "swap_used_mb": 6831,
               "swapouts_pages": 1993782,
               "compressor_logical_pages": 1701250,
               "compressor_physical_pages": 448382,
               "pages_free": 4004, "page_size_bytes": 16384,
               "memory_pressure_free_pct": 40},
        "load": {"load1": 3.79, "ncpu": 8},
        "attribution": [
            {"name": "svc-a", "layer": "L4", "footprint_mb": 400,
             "scales_with_concurrency": True, "per_unit_mb": 80},
        ],
        "g0_samples": [g0_sample(), g0_sample()],
        "g1_evidence": [{"item": "索引可及性", "remeasure_cmd": "rerun scan",
                         "before": "6/152", "after": "7/153"}],
        "g1_l3_ab": [{"item": "svc-a", "candidate": "svc-a 常駐 vs http 共享",
                      "success_rate": 1.0, "e2e_time": 12.0,
                      "e2e_bill": 0.4, "resident_footprint_mb": 400,
                      "alternative_disproved": True}],
        "l1_root_cause_fixed": True,
        "downclock_experiment": {"ran": True, "degraded": True,
                                 "tasks": [f"t{i}" for i in range(10)]},
        "spec_ladder": {"next_cheaper_insufficient": True,
                        "evidence": "下一階 24GB 在降載實驗中仍出現 2 次失敗"},
    }
    for k, v in over.items():
        m[k] = v
    return m


class TestRatios(unittest.TestCase):
    def test_sigma_undefined_when_swap_unconfigured(self):
        """⛔ swap 未配置時 σ 是 UNDEFINED 不是 0（portability.md B3）。"""
        m = meas()
        m["vm"] = dict(m["vm"], swap_total_mb=0, swap_used_mb=0)
        r = cg.ratios(decl(), m, 400)
        self.assertEqual(r["sigma"], cg.UNDEFINED)

    def test_missing_input_is_unknown_not_zero(self):
        """⛔ 量不到寫 UNKNOWN，不寫 0。"""
        m = meas()
        m["vm"] = {}
        r = cg.ratios(decl(), m, 400)
        for k in ("rho", "sigma", "phi"):
            self.assertEqual(r[k], cg.UNKNOWN, k)

    def test_ratios_are_deterministic(self):
        """同一份輸入重跑兩次必須完全一樣 —— 可審查的前提。"""
        d, m = decl(), meas()
        self.assertEqual(cg.evaluate(d, m), cg.evaluate(d, m))


class TestBaselineIntersection(unittest.TestCase):
    def test_declared_and_l4_counted_at_declared_concurrency(self):
        """per-session fork 以宣告並行度為上限，不用實測並行度。"""
        bl = cg.baseline(decl(), meas())
        self.assertEqual(bl["baseline_mb"], 400.0)      # 80 × 5
        self.assertEqual([c["name"] for c in bl["counted"]], ["svc-a"])

    def test_declared_without_observation_is_excluded(self):
        """⛔ 說不出觀察證據的宣告不計入 —— 這是 L4 可否證的關鍵。"""
        d = decl()
        d["incompressible"][0]["observation"] = "   "
        bl = cg.baseline(d, meas())
        self.assertTrue(bl["intersection_empty"])
        self.assertIn("說不出觀察證據", bl["excluded"][0]["reason"])

    def test_declared_but_measured_not_l4_is_excluded(self):
        """宣告了但實測落在 L2 → 不計入，且要指名。"""
        m = meas()
        m["attribution"][0]["layer"] = "L2"
        bl = cg.baseline(decl(), m)
        self.assertTrue(bl["intersection_empty"])
        self.assertIn("L2", bl["excluded"][0]["reason"])

    def test_measured_l4_without_declaration_is_excluded(self):
        """實測是 L4 但沒宣告 → 意外常駐，回 L1／L2，不計入。"""
        m = meas()
        m["attribution"].append({"name": "surprise", "layer": "L4",
                                 "footprint_mb": 9999})
        bl = cg.baseline(decl(), m)
        self.assertEqual(bl["baseline_mb"], 400.0)
        self.assertTrue(any(e["name"] == "surprise" for e in bl["excluded"]))


class TestG0Axis(unittest.TestCase):
    def test_single_snapshot_is_blocked(self):
        """⛔ 不得用單一快照。"""
        self.assertEqual(cg.gate_g0(meas(g0_samples=[mem_only()]))["status"], cg.BLOCKED)

    def test_compute_evidence_only_is_rejected(self):
        """裝置 B 那一類：有運算的正面證據、無記憶體證據 → 不受理。"""
        g = cg.gate_g0(meas(g0_samples=[cpu_only(), cpu_only()]))
        self.assertEqual(g["status"], cg.FAIL)
        self.assertEqual(g["verdict"], "不受理")

    def test_memory_evidence_only_passes(self):
        g = cg.gate_g0(meas(g0_samples=[mem_only(), mem_only()]))
        self.assertEqual(g["status"], cg.PASS)

    def test_both_kinds_of_evidence_is_blocked(self):
        """⛔ 兩種正面證據同時存在時不硬判。"""
        g = cg.gate_g0(meas(g0_samples=[mem_only(), cpu_only(
            **{k: "present" for k in cg.MEMORY_EVIDENCE})]))
        self.assertEqual(g["status"], cg.BLOCKED)
        self.assertIn("同時存在", g["reason"])

    def test_no_evidence_at_all_is_blocked(self):
        """閒置機器：兩種證據都沒有 → 停住，⛔ 不得讀成任一方的證據。"""
        g = cg.gate_g0(meas(g0_samples=[no_signal(), no_signal()]))
        self.assertEqual(g["status"], cg.BLOCKED)
        self.assertIn("沒有成形的瓶頸", g["reason"])

    def test_unlabelled_state_is_blocked(self):
        """每一項都要標狀態，缺標就停住 —— 不替分析者猜。"""
        m = meas()
        m["g0_samples"][0]["M1_swapouts_rising"] = {"value": "1993782"}
        self.assertEqual(cg.gate_g0(m)["status"], cg.BLOCKED)


class TestG0RevisionThree(unittest.TestCase):
    """
    第三次預先登記：把「四項判據投票」改成「兩組正面證據」。

    理由：原判據表的兩欄不對稱。`ps` 被截斷是記憶體壓力的正面證據，
    `ps` 沒被截斷卻跟每一種狀態都相容（閒置／CPU 打滿／記憶體吃緊但未達截斷門檻），
    鑑別力為零。把它標成「指向運算」是製造訊號。

    ⚠️ 這次修訂的方向**有利於申請方**，所以不能拿方向當「不是裁剪」的證據。
    以下測試守的是它的**論證**：absent 不得產生任何一方的證據。
    """

    def test_absence_of_memory_evidence_is_not_compute_evidence(self):
        """核心論證：M 全 absent、C 也全 absent → 停住，不是不受理。"""
        g = cg.gate_g0(meas(g0_samples=[no_signal(), no_signal()]))
        self.assertEqual(g["status"], cg.BLOCKED)
        self.assertEqual(g["compute_evidence_present"], [])

    def test_absence_of_compute_evidence_is_not_memory_evidence(self):
        """反向也要成立：C 全 absent 不會讓 M 憑空出現。"""
        s = no_signal()
        g = cg.gate_g0(meas(g0_samples=[s, s]))
        self.assertEqual(g["memory_evidence_present"], [])

    def test_ps_consistent_no_longer_manufactures_compute_evidence(self):
        """
        裝置 A 的實地形態：M1／M3／M4 present、M2 absent（ps 未被截斷）、
        C1／C2 absent（最高 42%）。舊結構判矛盾，新結構判過。
        """
        s = mem_only(M2_ps_truncated="absent")
        g = cg.gate_g0(meas(g0_samples=[s, s, s]))
        self.assertEqual(g["status"], cg.PASS)
        self.assertNotIn("M2_ps_truncated", g["memory_evidence_present"])

    def test_device_b_verdict_is_preserved_by_the_revision(self):
        """
        ⛔ 驗收條件：修訂**不得改變裝置 B 的結論**。
        B 的形態是 C1 present（mediaanalysisd 230%）、記憶體證據全 absent，
        且其中一個取樣點連 C1 都 absent（突發結束）→ 仍須判不受理。
        """
        busy = cpu_only()
        idle = cpu_only(C1_single_process_saturating="absent")
        g = cg.gate_g0(meas(g0_samples=[busy, busy, idle]))
        self.assertEqual(g["status"], cg.FAIL)
        self.assertEqual(g["verdict"], "不受理")

    def test_unknown_never_becomes_evidence(self):
        """量不到寫 unknown，而 unknown 不得被當成任一方的證據。"""
        s = {k: {"state": "unknown", "value": "UNKNOWN"} for k in
             list(cg.MEMORY_EVIDENCE) + list(cg.COMPUTE_EVIDENCE)}
        g = cg.gate_g0(meas(g0_samples=[s, s]))
        self.assertEqual(g["status"], cg.BLOCKED)
        self.assertEqual(g["memory_evidence_present"], [])
        self.assertEqual(g["compute_evidence_present"], [])


class TestWorkingSetDeclaration(unittest.TestCase):
    """
    修訂 3：宣告工作集 —— 整項落 L2 但其中一段是工作真的需要的。

    ⛔ 這不是把 L2 洗成 L4。護欄有三層：要有 observation、只計宣告的那一段、
       而且是不是真的不可壓縮仍由 G2 的降載實驗決定。
    """

    def _l2_case(self, ws):
        d = decl()
        d["incompressible"] = [{"name": "browser", "scales_with_concurrency": False,
                                "working_set_mb": ws,
                                "observation": "當下使用證據：45 個 process，取樣期間有 CPU 活動",
                                "shared_alternative_reason": "延遲不可容忍",
                                "non_resident_alternative": "無"}]
        m = meas(attribution=[{"name": "browser", "layer": "L2", "footprint_mb": 9571}])
        return cg.baseline(d, m)

    def test_declared_working_set_counts_only_the_declared_part(self):
        bl = self._l2_case(4000)
        self.assertEqual(bl["baseline_mb"], 4000.0)
        self.assertTrue(any("超出工作集" in e["name"] for e in bl["excluded"]))
        self.assertTrue(any(e.get("mb") == 5571.0 for e in bl["excluded"]))

    def test_working_set_cannot_exceed_measured(self):
        """宣告比實測大時以實測為準 —— ⛔ 宣告不能創造不存在的量。"""
        self.assertEqual(self._l2_case(99999)["baseline_mb"], 9571.0)

    def test_l2_without_working_set_is_still_excluded(self):
        """沒宣告工作集的 L2 照樣整項排除，並指名原因。"""
        d = decl()
        d["incompressible"] = [{"name": "browser", "observation": "有在用",
                                "shared_alternative_reason": "延遲不可容忍",
                                "non_resident_alternative": "無"}]
        m = meas(attribution=[{"name": "browser", "layer": "L2", "footprint_mb": 9571}])
        bl = cg.baseline(d, m)
        self.assertTrue(bl["intersection_empty"])
        self.assertIn("未宣告工作集", bl["excluded"][0]["reason"])

    def test_working_set_still_needs_observation(self):
        """⛔ 工作集不能繞過 observation 這道門。"""
        d = decl()
        d["incompressible"] = [{"name": "browser", "working_set_mb": 4000,
                                "observation": "", "shared_alternative_reason": "延遲不可容忍",
                                "non_resident_alternative": "無"}]
        m = meas(attribution=[{"name": "browser", "layer": "L2", "footprint_mb": 9571}])
        self.assertTrue(cg.baseline(d, m)["intersection_empty"])


class TestSpecLadderPolicies(unittest.TestCase):
    """修訂 3：規格階梯的兩種政策。宣告式比證否式寬鬆，錨點必須是應然基線。"""

    LADDER = [16, 24, 36, 48, 64, 128]

    def _headroom(self, anchor_mb, rec_gb, baseline_mb=None):
        m = meas(spec_ladder={"policy": "declared_headroom", "tiers_gb": self.LADDER,
                              "anchor_mb": anchor_mb, "recommended_gb": rec_gb,
                              "tiers_above": 1})
        return cg.gate_g3(decl(), m,
                          baseline_mb if baseline_mb is not None else anchor_mb)

    def test_headroom_policy_accepts_cover_plus_one(self):
        """錨點 19.8 GB → 覆蓋階 24 → +1 → 36。"""
        g = self._headroom(20275, 36)
        self.assertEqual(g["status"], cg.PASS)
        self.assertTrue(any("36 GB" in n for n in g["notes"]))

    def test_headroom_policy_rejects_wrong_tier(self):
        """錨點 19.8 GB 卻寫 48 → 不受理（+1 階是 36，不是 +2）。"""
        self.assertEqual(self._headroom(20275, 48)["status"], cg.FAIL)

    def test_headroom_anchor_must_be_the_baseline_not_current_usage(self):
        """
        ⛔ 核心護欄：錨點不得用現值。
        現值 27.8 GB 會推到 48 GB，但應然基線是 19.8 GB —— 標準明訂
        「不得以現值作為需求基準，現值可能已經包含該被刪掉的浪費」。
        """
        g = self._headroom(28467, 48, baseline_mb=20275)
        self.assertEqual(g["status"], cg.FAIL)
        self.assertTrue(any("不得以現值當需求基準" in p for p in g["problems"]))

    def test_disproof_policy_still_works_and_is_the_default(self):
        g = cg.gate_g3(decl(), meas(), 400)
        self.assertEqual(g["status"], cg.PASS)

    def test_unknown_policy_is_rejected(self):
        m = meas(spec_ladder={"policy": "我覺得應該買大一點", "tiers_gb": self.LADDER})
        self.assertEqual(cg.gate_g3(decl(), m, 400)["status"], cg.FAIL)


class TestG1Exhaustion(unittest.TestCase):
    def test_claim_without_before_after_fails(self):
        """⛔ 只有宣稱沒有前→後 → 不受理，退回該層。"""
        m = meas(g1_evidence=[{"item": "某建議", "remeasure_cmd": "cmd",
                               "before": None, "after": "好了"}])
        g = cg.gate_g1(decl(), m)
        self.assertEqual(g["status"], cg.FAIL)
        self.assertTrue(any("沒有前→後" in p for p in g["problems"]))

    def test_l3_ab_missing_column_fails(self):
        """⛔ A/B 缺任一欄即不受理，不得用三欄推論。"""
        m = meas(g1_l3_ab=[{"candidate": "svc-a", "success_rate": 1.0,
                            "e2e_time": 12.0, "resident_footprint_mb": 400}])
        g = cg.gate_g1(decl(), m)
        self.assertEqual(g["status"], cg.FAIL)
        self.assertTrue(any("缺欄" in p for p in g["problems"]))

    def test_l3_ab_never_run_fails(self):
        """A/B 從未實測 → 不過。這是本標準目前真實的狀態。"""
        g = cg.gate_g1(decl(), meas(g1_l3_ab=[]))
        self.assertEqual(g["status"], cg.FAIL)

    def test_l1_manual_cleanup_without_root_cause_fails(self):
        g = cg.gate_g1(decl(), meas(l1_root_cause_fixed=False))
        self.assertEqual(g["status"], cg.FAIL)
        self.assertTrue(any("成因" in p for p in g["problems"]))


class TestG1CandidateCompleteness(unittest.TestCase):
    """
    第五次預先登記：G1 的候選集完整性。

    原本只檢查「列出來的每一列四欄齊不齊」，沒檢查「該列的候選有沒有全部列出來」。
    於是只測最好測的那一個就能過關 —— 而且把項目歸成 L4 就能讓它完全退出候選集。
    ⚠️ 這次修訂的方向**不利於申請方**（它讓 G1 更難過）。
    """

    def _two_items(self, ab_rows):
        d = decl()
        d["incompressible"] = [
            {"name": "svc-a", "observation": "有在用",
             "shared_alternative_reason": "資料落地限制",
             "non_resident_alternative": "冷載入"},
            {"name": "svc-b", "observation": "有在用",
             "shared_alternative_reason": "資料落地限制",
             "non_resident_alternative": "http transport"},
        ]
        m = meas(attribution=[{"name": "svc-a", "layer": "L4", "footprint_mb": 100},
                              {"name": "svc-b", "layer": "L4", "footprint_mb": 200}],
                 g1_l3_ab=ab_rows)
        return d, m

    def _row(self, item, disproved=True):
        return {"item": item, "success_rate": 1.0, "e2e_time": 1.0, "e2e_bill": 0.0,
                "resident_footprint_mb": 100, "alternative_disproved": disproved}

    def test_testing_only_the_easy_candidate_fails(self):
        """⛔ 只測一個候選就想過關 → 不過，且要指名漏了誰。"""
        d, m = self._two_items([self._row("svc-a")])
        g = cg.gate_g1(d, m)
        self.assertEqual(g["status"], cg.FAIL)
        self.assertTrue(any("svc-b" in p for p in g["problems"]))

    def test_covering_all_candidates_passes(self):
        d, m = self._two_items([self._row("svc-a"), self._row("svc-b")])
        self.assertEqual(cg.gate_g1(d, m)["status"], cg.PASS)

    def test_omitting_the_alternative_question_fails(self):
        """⛔ 「有沒有不需常駐的替代實作」這一問不得省略。"""
        d, m = self._two_items([self._row("svc-a"), self._row("svc-b")])
        del d["incompressible"][0]["non_resident_alternative"]
        g = cg.gate_g1(d, m)
        self.assertEqual(g["status"], cg.FAIL)
        self.assertTrue(any("不得省略" in p for p in g["problems"]))

    def test_measured_l3_is_always_a_candidate(self):
        """實測層別是 L3 的項目一律進候選集，就算沒被宣告。"""
        d = decl()
        m = meas(attribution=[{"name": "svc-a", "layer": "L4", "footprint_mb": 100},
                              {"name": "orphan-l3", "layer": "L3", "footprint_mb": 50}])
        g = cg.gate_g1(d, m)
        self.assertEqual(g["status"], cg.FAIL)
        self.assertTrue(any("orphan-l3" in p for p in g["problems"]))

    def test_ab_without_disproved_verdict_fails(self):
        """A/B 跑了但沒說「該替代有沒有被否證」→ 不算跑完。"""
        row = self._row("svc-a"); del row["alternative_disproved"]
        d, m = self._two_items([row, self._row("svc-b")])
        g = cg.gate_g1(d, m)
        self.assertEqual(g["status"], cg.FAIL)
        self.assertTrue(any("alternative_disproved" in p for p in g["problems"]))

    def test_untested_alternative_keeps_item_out_of_the_baseline(self):
        """
        ⛔ 這一條才是真正的牙齒：G1 不過的同時，基線也不能把那些項目算進去。
        否則 R 會虛報 —— 「我把它歸成 L4」在數字上仍然有效。
        """
        d, m = self._two_items([self._row("svc-a")])
        bl = cg.baseline(d, m)
        self.assertEqual([c["name"] for c in bl["counted"]], ["svc-a"])
        self.assertTrue(any(e["name"] == "svc-b" and "L3 待驗" in e["reason"]
                            for e in bl["excluded"]))

    def test_alternative_not_disproved_also_excluded(self):
        """A/B 顯示替代可行（未被否證）→ 該項是 L3，同樣不計入基線。"""
        d, m = self._two_items([self._row("svc-a", disproved=False),
                                self._row("svc-b")])
        bl = cg.baseline(d, m)
        self.assertNotIn("svc-a", [c["name"] for c in bl["counted"]])
        self.assertTrue(any(e["name"] == "svc-a" and "未被否證" in e["reason"]
                            for e in bl["excluded"]))


class TestG2Gap(unittest.TestCase):
    def test_downclock_not_run_is_blocked(self):
        m = meas(downclock_experiment={"ran": False, "tasks": [], "degraded": None})
        g = cg.gate_g2(cg.baseline(decl(), m), cg.ratios(decl(), m, 400), m)
        self.assertEqual(g["status"], cg.BLOCKED)

    def test_no_degradation_sends_back_to_layer_one(self):
        """⛔ 降載無劣化 → L4 分類錯誤，退回第一層。不得重挑任務集。"""
        m = meas(downclock_experiment={"ran": True, "degraded": False,
                                       "tasks": [f"t{i}" for i in range(10)]})
        g = cg.gate_g2(cg.baseline(decl(), m), cg.ratios(decl(), m, 400), m)
        self.assertEqual(g["status"], cg.FAIL)
        self.assertIn("退回第一層", g["verdict"])

    def test_fewer_than_ten_tasks_fails(self):
        m = meas(downclock_experiment={"ran": True, "degraded": True,
                                       "tasks": ["t1", "t2"]})
        g = cg.gate_g2(cg.baseline(decl(), m), cg.ratios(decl(), m, 400), m)
        self.assertEqual(g["status"], cg.FAIL)

    def test_sigma_undefined_produces_a_note_not_silence(self):
        """swap 未配置時要出聲：不得以 σ 支撐缺口。"""
        m = meas()
        m["vm"] = dict(m["vm"], swap_total_mb=0, swap_used_mb=0)
        g = cg.gate_g2(cg.baseline(decl(), m), cg.ratios(decl(), m, 400), m)
        self.assertEqual(g["status"], cg.PASS)
        self.assertTrue(any("UNDEFINED" in n for n in g["notes"]))


class TestG2Unvalidatable(unittest.TestCase):
    """
    修訂 6：自陳「G2 不可驗」的項目。

    撞到的形態：使用者宣告「分頁即待辦佇列」——那一項既無可縮減量
    （工作集＝當下現況），而「關掉分頁」的代價是**工作佇列遺失**，
    不是任務成功率下降，已登記的劣化定義 A 量不到那種代價。
    ⛔ 不可驗**不等於**已驗證。
    """

    def _case(self, ran=True, degraded=True):
        d = decl()
        d["incompressible"] = [{
            "name": "svc-a", "scales_with_concurrency": True, "per_unit_mb": 80,
            "observation": "有在用", "shared_alternative_reason": "資料落地限制",
            "non_resident_alternative": "無",
            "g2_unvalidatable": {"reason": "關掉它的代價是佇列遺失，劣化定義 A 量不到"}}]
        m = meas(downclock_experiment={"ran": ran, "degraded": degraded,
                                       "tasks": [f"t{i}" for i in range(10)]},
                 g1_l3_ab=[])
        return d, m

    def test_unvalidatable_item_blocks_g2_even_after_downclock(self):
        """⛔ 降載實驗跑完且有劣化，仍不得讓不可驗項目默默算成已驗證。"""
        d, m = self._case()
        g = cg.gate_g2(cg.baseline(d, m), cg.ratios(d, m, 400), m, d)
        self.assertEqual(g["status"], cg.BLOCKED)
        self.assertEqual(len(g["unvalidatable"]), 1)
        self.assertIn("不可驗不等於已驗證", g["reason"])

    def test_unvalidatable_reports_share_of_baseline(self):
        """審查者要看得到它佔基線多少 —— 那決定這個缺口有多少是未驗證的。"""
        d, m = self._case()
        g = cg.gate_g2(cg.baseline(d, m), cg.ratios(d, m, 400), m, d)
        self.assertEqual(g["unvalidatable"][0]["share_of_baseline"], 100.0)

    def test_unvalidatable_surfaced_even_before_downclock_runs(self):
        """降載還沒跑時就要先講：跑完那幾項也不會被驗證。"""
        d, m = self._case(ran=False, degraded=None)
        g = cg.gate_g2(cg.baseline(d, m), cg.ratios(d, m, 400), m, d)
        self.assertEqual(g["status"], cg.BLOCKED)
        self.assertIn("仍不會被驗證", g["reason"])

    def test_no_unvalidatable_still_passes(self):
        """沒有不可驗項目時行為不變 —— 修訂不得改變既有案子的結論。"""
        m = meas()
        g = cg.gate_g2(cg.baseline(decl(), m), cg.ratios(decl(), m, 400), m, decl())
        self.assertEqual(g["status"], cg.PASS)


class TestG2ValidationTypology(unittest.TestCase):
    """
    修訂 7：G2 的驗證型別三分（downclock／mechanism／structural）。
    ⚠️ 方向有利於申請方 —— 護欄是每列都要指名證據，且由 counted 驅動。
    """

    def _decl_with(self, validation):
        d = decl()
        d["incompressible"][0]["g2_validation"] = validation
        return d

    def _no_downclock(self):
        return meas(downclock_experiment={"ran": False, "tasks": [], "degraded": None})

    def _g2(self, d, m):
        return cg.gate_g2(cg.baseline(d, m), cg.ratios(d, m, 400), m, d)

    def test_mechanism_validation_passes_without_downclock(self):
        d = self._decl_with({"type": "mechanism",
                             "mechanism": "Chrome form data 保護 —— 平台拒絕回收以保護未存狀態",
                             "levers_exhausted": ["Memory Saver 已 Maximum", "手動 Urgent Discard 被擋"]})
        g = self._g2(d, self._no_downclock())
        self.assertEqual(g["status"], cg.PASS)
        self.assertTrue(any("寬" in n for n in g["notes"]))   # 方向揭露必須出現

    def test_mechanism_without_levers_fails(self):
        """⛔ 只說有機制、不列試過的桿子 → 不過。"""
        d = self._decl_with({"type": "mechanism", "mechanism": "某保護"})
        g = self._g2(d, self._no_downclock())
        self.assertEqual(g["status"], cg.FAIL)

    def test_structural_needs_a_legal_basis(self):
        """⛔ 「我覺得很重要」不是結構必要。"""
        d = self._decl_with({"type": "structural", "basis": "我覺得很重要"})
        g = self._g2(d, self._no_downclock())
        self.assertEqual(g["status"], cg.FAIL)
        d2 = self._decl_with({"type": "structural", "basis": "作業系統"})
        self.assertEqual(self._g2(d2, self._no_downclock())["status"], cg.PASS)

    def test_counted_item_missing_from_decl_still_needs_downclock(self):
        """⛔ 語意漏洞回歸測試：decl 缺漏不得讓閘門變鬆。"""
        d = decl()
        g = cg.gate_g2(cg.baseline(d, self._no_downclock()),
                       cg.ratios(d, self._no_downclock(), 400),
                       self._no_downclock(), None)     # 呼叫端漏傳 decl
        self.assertEqual(g["status"], cg.BLOCKED)
        self.assertIn("svc-a", str(g.get("pending_downclock") or g["reason"]))

    def test_mixed_pending_still_requires_downclock(self):
        """一項有驗證、另一項沒有 → 沒有的那項仍擋住 G2，並被指名。"""
        d = decl()
        d["incompressible"] = [
            dict(d["incompressible"][0],
                 g2_validation={"type": "structural", "basis": "作業系統"}),
            {"name": "svc-b", "observation": "有在用",
             "shared_alternative_reason": "資料落地限制",
             "non_resident_alternative": "無"}]
        m = self._no_downclock()
        m["attribution"].append({"name": "svc-b", "layer": "L4", "footprint_mb": 100})
        m["g1_l3_ab"] = []
        g = self._g2(d, m)
        self.assertEqual(g["status"], cg.BLOCKED)
        self.assertIn("svc-b", str(g["pending_downclock"]))


class TestG3Alternatives(unittest.TestCase):
    def test_upgradeable_machine_is_rejected(self):
        """可事後升級 → 不走第三層，走加購。"""
        d = decl()
        d["machine"]["ram_upgradeable"] = True
        g = cg.gate_g3(d, meas())
        self.assertEqual(g["status"], cg.FAIL)

    def test_missing_upgradeable_statement_fails(self):
        """這一問不得省略。"""
        d = decl()
        del d["machine"]["ram_upgradeable"]
        g = cg.gate_g3(d, meas())
        self.assertEqual(g["status"], cg.FAIL)

    def test_shared_alternative_reason_none_is_rejected(self):
        """共享替代可行且無四理由之一 → 不受理。"""
        d = decl()
        d["incompressible"][0]["shared_alternative_reason"] = "無"
        g = cg.gate_g3(d, meas())
        self.assertEqual(g["status"], cg.FAIL)

    def test_shared_alternative_reason_outside_the_four_is_rejected(self):
        d = decl()
        d["incompressible"][0]["shared_alternative_reason"] = "我比較習慣本機"
        g = cg.gate_g3(d, meas())
        self.assertEqual(g["status"], cg.FAIL)

    def test_spec_ladder_only_proving_this_spec_fails(self):
        """只證明本案規格夠、未證明下一階不足 → 不受理。"""
        m = meas(spec_ladder={"next_cheaper_insufficient": False,
                              "evidence": "本案 32GB 夠用"})
        g = cg.gate_g3(decl(), m)
        self.assertEqual(g["status"], cg.FAIL)


class TestEndToEnd(unittest.TestCase):
    def test_all_gates_pass_yields_reluctant_acceptance(self):
        """四道全過時，結論的措辭必須是「權宜推薦」而不是「核准」。"""
        res = cg.evaluate(decl(), meas())
        self.assertIsNone(res["stopped_at"])
        self.assertIn("權宜推薦", res["verdict"])

    def test_stops_at_first_failing_gate_no_layer_skipping(self):
        """⛔ 不得跳層：停在第一道不過的閘門。"""
        m = meas(g0_samples=[cpu_only(), cpu_only()], g1_l3_ab=[])  # G0 與 G1 都不過
        res = cg.evaluate(decl(), m)
        self.assertEqual(res["stopped_at"], "G0")     # 停在 G0，不繼續評 G1

    def test_cli_runs_and_is_json_serialisable(self):
        with tempfile.TemporaryDirectory() as tmp:
            dp, mp = Path(tmp) / "d.json", Path(tmp) / "m.json"
            dp.write_text(json.dumps(decl(), ensure_ascii=False), encoding="utf-8")
            mp.write_text(json.dumps(meas(), ensure_ascii=False), encoding="utf-8")
            out = subprocess.run(
                [sys.executable, str(SCRIPTS / "compute_gap.py"),
                 "--declaration", str(dp), "--measurement", str(mp), "--json"],
                capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            json.loads(out.stdout)

    def test_no_numeric_threshold_leaked_into_the_code(self):
        """
        ⛔ 標準不得內建跨裝置數值門檻（C1；n = 2）。
        R 只是提報用的量，不能有及格線 —— 所以同一份輸入把 R 拉高十倍，
        結論不應該改變。
        """
        d, m = decl(), meas()
        base = cg.evaluate(d, m)
        d2 = decl()
        d2["machine"] = dict(d2["machine"], ram_gb=1)      # R 從 0.024 變 0.39
        high = cg.evaluate(d2, m)
        self.assertNotEqual(base["ratios"]["R"], high["ratios"]["R"])
        self.assertEqual(base["verdict"], high["verdict"])


if __name__ == "__main__":
    unittest.main()
