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
             "shared_alternative_reason": "資料落地限制"},
        ],
    }
    d.update(over)
    return d


def g0_sample(**dirs):
    base = {k: "memory" for k in
            ("swapouts", "memory_pressure", "ps_vs_pgrep", "single_cpu")}
    base.update(dirs)
    return {k: {"direction": v, "value": "<實測值>"} for k, v in base.items()}


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
        "g1_l3_ab": [{"candidate": "svc-a", "success_rate": 1.0, "e2e_time": 12.0,
                      "e2e_bill": 0.4, "resident_footprint_mb": 400}],
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
        m = meas(g0_samples=[g0_sample()])
        self.assertEqual(cg.gate_g0(m)["status"], cg.BLOCKED)

    def test_all_compute_is_rejected(self):
        """裝置 B 那一類：四項一致指向運算 → 不受理。**這是負面對照的核心測試**。"""
        s = g0_sample(swapouts="compute", memory_pressure="compute",
                      ps_vs_pgrep="compute", single_cpu="compute")
        g = cg.gate_g0(meas(g0_samples=[s, s]))
        self.assertEqual(g["status"], cg.FAIL)
        self.assertEqual(g["verdict"], "不受理")

    def test_contradictory_is_blocked_not_forced(self):
        """⛔ 判據互相矛盾時不要硬判。"""
        s = g0_sample(single_cpu="compute")
        g = cg.gate_g0(meas(g0_samples=[s, s]))
        self.assertEqual(g["status"], cg.BLOCKED)

    def test_unlabelled_direction_is_blocked(self):
        """每項判據都要明確標方向，缺標就停住 —— 不替分析者猜。"""
        m = meas()
        m["g0_samples"][0]["swapouts"] = {"value": "1993782"}   # 沒有 direction
        self.assertEqual(cg.gate_g0(m)["status"], cg.BLOCKED)


class TestG0RevisionOne(unittest.TestCase):
    """
    修訂 1（audit-standard.md 第十節）：`inconclusive` 的處置。

    原規則要求「每一個取樣點都四項全 compute」，於是一個落在閒置時刻的取樣點
    （突發 CPU 消耗者剛結束 → `single_cpu` 為 inconclusive）就把一個明確非記憶體的
    案子打成「無法歸因」。四種情況現在窮盡且互斥。
    """

    def test_idle_sample_does_not_rescue_a_compute_bound_case(self):
        """
        情況 1：實地形態 —— 三個取樣點、12 個讀數，11 個 compute、0 個 memory，
        其中一個取樣點的 single_cpu 是 inconclusive。必須判不受理。
        """
        s_busy = g0_sample(swapouts="compute", memory_pressure="compute",
                           ps_vs_pgrep="compute", single_cpu="compute")
        s_idle = g0_sample(swapouts="compute", memory_pressure="compute",
                           ps_vs_pgrep="compute", single_cpu="inconclusive")
        g = cg.gate_g0(meas(g0_samples=[s_busy, s_busy, s_idle]))
        self.assertEqual(g["status"], cg.FAIL)
        self.assertEqual(g["verdict"], "不受理")

    def test_all_inconclusive_is_blocked_not_rejected(self):
        """情況 3：完全沒有成形的訊號 → 停住並要求在有負載的時點重取，不是不受理。"""
        s = g0_sample(swapouts="inconclusive", memory_pressure="inconclusive",
                      ps_vs_pgrep="inconclusive", single_cpu="inconclusive")
        g = cg.gate_g0(meas(g0_samples=[s, s]))
        self.assertEqual(g["status"], cg.BLOCKED)
        self.assertIn("重取", g["reason"])

    def test_memory_in_one_sample_compute_in_another_is_blocked(self):
        """情況 2：跨取樣點的矛盾也算矛盾 —— 不硬判。"""
        s_mem = g0_sample()
        s_cpu = g0_sample(swapouts="compute", memory_pressure="compute",
                          ps_vs_pgrep="compute", single_cpu="compute")
        g = cg.gate_g0(meas(g0_samples=[s_mem, s_cpu]))
        self.assertEqual(g["status"], cg.BLOCKED)
        self.assertIn("矛盾", g["reason"])

    def test_memory_with_inconclusive_still_passes(self):
        """情況 4：有記憶體證據、無運算證據 → 過。inconclusive 不擋。"""
        s = g0_sample(single_cpu="inconclusive")
        g = cg.gate_g0(meas(g0_samples=[s, s]))
        self.assertEqual(g["status"], cg.PASS)

    def test_revision_does_not_change_verdicts_for_memory_bound_machines(self):
        """
        ⛔ 修訂的驗收條件：**不得改變任何既有案子的結論**。
        有判據指向記憶體的機器走情況 3／4，結果必須與修訂前相同（PASS）。
        這條測試就是「這不是為了讓某台機器過關而裁剪」的機械證明。
        """
        for extra in ({}, {"single_cpu": "inconclusive"},
                      {"ps_vs_pgrep": "inconclusive", "single_cpu": "inconclusive"}):
            s = g0_sample(**extra)
            with self.subTest(extra=extra):
                self.assertEqual(cg.gate_g0(meas(g0_samples=[s, s]))["status"], cg.PASS)

    def test_revision_only_moves_verdicts_toward_rejection(self):
        """
        修訂的方向性：它讓標準**更容易判不受理**，不是更容易受理。
        原規則會把「無記憶體證據＋部分 inconclusive」判成 BLOCKED；
        新規則判 FAIL。⛔ 沒有任何輸入從 FAIL／BLOCKED 變成 PASS。
        """
        s_busy = g0_sample(swapouts="compute", memory_pressure="compute",
                           ps_vs_pgrep="compute", single_cpu="compute")
        s_idle = g0_sample(swapouts="compute", memory_pressure="compute",
                           ps_vs_pgrep="compute", single_cpu="inconclusive")
        # 任何「無 memory 讀數」的組合都不可能是 PASS
        for combo in ([s_busy, s_idle], [s_idle, s_idle], [s_busy, s_busy]):
            with self.subTest(n_idle=sum(1 for x in combo if x is s_idle)):
                self.assertNotEqual(cg.gate_g0(meas(g0_samples=combo))["status"], cg.PASS)


class TestG1Exhaustion(unittest.TestCase):
    def test_claim_without_before_after_fails(self):
        """⛔ 只有宣稱沒有前→後 → 不受理，退回該層。"""
        m = meas(g1_evidence=[{"item": "某建議", "remeasure_cmd": "cmd",
                               "before": None, "after": "好了"}])
        g = cg.gate_g1(m)
        self.assertEqual(g["status"], cg.FAIL)
        self.assertTrue(any("沒有前→後" in p for p in g["problems"]))

    def test_l3_ab_missing_column_fails(self):
        """⛔ A/B 缺任一欄即不受理，不得用三欄推論。"""
        m = meas(g1_l3_ab=[{"candidate": "svc-a", "success_rate": 1.0,
                            "e2e_time": 12.0, "resident_footprint_mb": 400}])
        g = cg.gate_g1(m)
        self.assertEqual(g["status"], cg.FAIL)
        self.assertTrue(any("缺欄" in p for p in g["problems"]))

    def test_l3_ab_never_run_fails(self):
        """A/B 從未實測 → 不過。這是本標準目前真實的狀態。"""
        g = cg.gate_g1(meas(g1_l3_ab=[]))
        self.assertEqual(g["status"], cg.FAIL)

    def test_l1_manual_cleanup_without_root_cause_fails(self):
        g = cg.gate_g1(meas(l1_root_cause_fixed=False))
        self.assertEqual(g["status"], cg.FAIL)
        self.assertTrue(any("成因" in p for p in g["problems"]))


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
        s = g0_sample(swapouts="compute", memory_pressure="compute",
                      ps_vs_pgrep="compute", single_cpu="compute")
        m = meas(g0_samples=[s, s], g1_l3_ab=[])      # G0 與 G1 都不過
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
