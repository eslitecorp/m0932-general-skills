#!/usr/bin/env python3
"""
把 audit-standard.md 的四道閘門變成可執行的判定。

    python3 compute_gap.py --declaration decl.json --measurement meas.json [--json]

為什麼要有這支腳本：標準寫成散文時，兩個人套同一份資料會得到不同結論
（`environment.md` 自己標著「四層歸因的分類一致性仍未驗證」）。
判定邏輯放進程式碼，同一份輸入就只有一個輸出 —— 這是分類一致性與公信力的載體。

三條設計約束，改動時不要違反：

1. ⛔ **不內建任何數值門檻。** `portability.md` 的 C1（門檻自引）＋裝置 profile `n = 2`
   使得跨裝置門檻算不出來。所以本腳本只檢查**證據的形狀與組合邏輯**：
   每項判據的方向（指向記憶體／指向運算）由分析者明確標注並附值，
   腳本驗證的是「有沒有這一類證據」與「四項怎麼組合」，不是「數字超過多少」。
   唯一允許的數值來自提案者自己的宣告，且一律標成 declared。
2. ⛔ **決定性。** 不用 time／random，同一份輸入重跑兩次必須完全一樣（可審查的前提）。
3. ⛔ **量不到寫 UNKNOWN，不寫 0。** 0 在這套判準裡是有意義的訊號
   （例如 `Swapouts = 0` 指向運算瓶頸），用它代表「量不到」等於偽造判據。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

UNKNOWN = "UNKNOWN"
UNDEFINED = "UNDEFINED"

PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"

SHARED_ALT_REASONS = {
    "資料落地限制", "延遲不可容忍", "離線可用性", "供應商中斷降級",
}

# environment.md 對 L3 的定義：「同能力有不需常駐的替代實作」。
# 宣告的不可壓縮項必須正面回答有沒有這種替代 —— ⛔ 這一問不得省略，
# 否則分析者可以把項目直接放進 L4 而永遠不必面對 A/B（第五次預先登記的理由）。
NON_RESIDENT_ALTERNATIVES = {"遠端 API", "http transport", "原生執行", "冷載入"}
NO_ALTERNATIVE = "無"


def l3_candidates(decl: dict, meas: dict) -> tuple[set, list]:
    """
    回傳 (需要 A/B 的候選集, 缺答的項目)。

    候選集 = 宣告了不需常駐替代實作的項目 ∪ 實測層別為 L3 的項目。
    ⛔ 「我把它歸成 L4」不能讓它退出候選集 —— 那正是要防的漏洞。
    """
    cands, unanswered = set(), []
    for d in decl.get("incompressible") or []:
        name = d.get("name")
        if not name:
            continue
        alt = d.get("non_resident_alternative")
        if alt is None or not str(alt).strip():
            unanswered.append(name)
        elif str(alt).strip() != NO_ALTERNATIVE:
            cands.add(name)
    for a in meas.get("attribution") or []:
        if a.get("layer") == "L3" and a.get("name"):
            cands.add(a["name"])
    return cands, unanswered


def ab_outcome(meas: dict) -> dict:
    """A/B 的結論：item → alternative_disproved（True／False／None=未測）。"""
    out = {}
    for row in meas.get("g1_l3_ab") or []:
        item = row.get("item") or row.get("candidate")
        if item:
            out[item] = row.get("alternative_disproved")
    return out


# --- 無因次比值 -------------------------------------------------------------

def ratios(decl: dict, meas: dict, baseline_mb) -> dict:
    """R／ρ／σ／φ／λ。任一輸入缺失一律回 UNKNOWN，⛔ 不以 0 代替。"""
    m = decl.get("machine") or {}
    vm = meas.get("vm") or {}
    load = meas.get("load") or {}

    ram_gb = m.get("ram_gb")
    ram_mb = ram_gb * 1024 if isinstance(ram_gb, (int, float)) and ram_gb > 0 else None

    out = {}

    # R = 應然基線 / 實體容量
    out["R"] = (round(baseline_mb / ram_mb, 3)
                if isinstance(baseline_mb, (int, float)) and ram_mb else UNKNOWN)

    # ρ = compressor 邏輯量 / compressor 實體佔用
    lo, ph = vm.get("compressor_logical_pages"), vm.get("compressor_physical_pages")
    out["rho"] = (round(lo / ph, 3)
                  if isinstance(lo, (int, float)) and isinstance(ph, (int, float)) and ph
                  else UNKNOWN)

    # σ = swap 已用 / 上限。⛔ 上限為 0 是 UNDEFINED 不是 0（portability.md B3）
    tot, used = vm.get("swap_total_mb"), vm.get("swap_used_mb")
    if not isinstance(tot, (int, float)) or not isinstance(used, (int, float)):
        out["sigma"] = UNKNOWN
    elif tot == 0:
        out["sigma"] = UNDEFINED          # swap 未配置
    else:
        out["sigma"] = round(used / tot, 3)

    # φ = free / 實體容量
    pf, psz = vm.get("pages_free"), vm.get("page_size_bytes")
    out["phi"] = (round(pf * psz / (ram_gb * 1024 ** 3), 5)
                  if isinstance(pf, (int, float)) and isinstance(psz, (int, float))
                  and isinstance(ram_gb, (int, float)) and ram_gb else UNKNOWN)

    # λ = load / 核心數
    l1, nc = load.get("load1"), load.get("ncpu")
    out["lambda"] = (round(l1 / nc, 3)
                     if isinstance(l1, (int, float)) and isinstance(nc, (int, float)) and nc
                     else UNKNOWN)
    return out


# --- 應然基線 = 宣告 ∩ 實測 L4 ---------------------------------------------

def baseline(decl: dict, meas: dict) -> dict:
    """
    只有「宣告了、實測落在 L4、且說得出觀察證據」三者同時成立才計入。

    ⛔ 三種排除都要指名，不得靜默丟掉 —— 提案者要看得到自己哪一項沒被採計、為什麼。
    """
    declared = {d["name"]: d for d in (decl.get("incompressible") or []) if d.get("name")}
    measured = {a["name"]: a for a in (meas.get("attribution") or []) if a.get("name")}
    conc = decl.get("concurrency_declared")
    cands, _ = l3_candidates(decl, meas)
    ab = ab_outcome(meas)

    counted, excluded = [], []
    for name, d in declared.items():
        a = measured.get(name)
        if a is None:
            excluded.append({"name": name, "reason": "宣告了但實測裡沒有這一項",
                             "mb": UNKNOWN})
            continue
        obs = (d.get("observation") or "").strip()
        if not obs:
            excluded.append({"name": name, "reason": "宣告了但說不出觀察證據 → 不計入",
                             "mb": a.get("footprint_mb", UNKNOWN)})
            continue
        # ⛔ 第五次預先登記：有不需常駐的替代實作、而該替代還沒被 A/B 否證的項目，
        #    一律不得計為 L4。否則「我把它歸成 L4」就能繞過整道 A/B。
        if name in cands and ab.get(name) is not True:
            state = ("A/B 未跑" if name not in ab
                     else "A/B 顯示該替代可行（未被否證）")
            excluded.append({"name": name,
                             "reason": f"宣告了不需常駐的替代實作（{d.get('non_resident_alternative')}），"
                                       f"但{state} → 仍屬 L3 待驗，不計入",
                             "mb": a.get("footprint_mb", UNKNOWN)})
            continue

        layer = a.get("layer")
        ws = d.get("working_set_mb")
        if layer != "L4":
            # 宣告工作集：整項落 L2 但其中有一部分是工作真的需要的。
            # ⛔ 這不是把 L2 洗成 L4 —— 它只讓宣告的那一段進入候選集，
            #    是不是真的不可壓縮仍由 G2 的降載實驗決定。超出的部分照樣是 L2。
            fp = a.get("footprint_mb")
            if (layer == "L2" and isinstance(ws, (int, float)) and ws > 0
                    and isinstance(fp, (int, float))):
                take = min(ws, fp)
                counted.append({"name": name, "mb": round(take, 1),
                                "note": f"宣告工作集 {ws} MB（實測 {fp} MB，"
                                        f"超出的 {round(fp - take, 1)} MB 仍為 L2）"})
                if fp > take:
                    excluded.append({"name": f"{name}（超出工作集的部分）",
                                     "reason": "超過宣告工作集 → 仍為 L2，回第一層處理",
                                     "mb": round(fp - take, 1)})
                continue
            excluded.append({"name": name,
                             "reason": f"宣告了但實測落在 {layer or UNKNOWN} 不是 L4 → 不計入"
                                       + ("" if ws else "（未宣告工作集）"),
                             "mb": a.get("footprint_mb", UNKNOWN)})
            continue

        # per-session fork 的量以宣告的並行度為上限，不用實測並行度
        if d.get("scales_with_concurrency") and isinstance(conc, (int, float)):
            per = a.get("per_unit_mb")
            if not isinstance(per, (int, float)):
                excluded.append({"name": name,
                                 "reason": "隨並行度變動但缺 per_unit_mb → 算不出上限",
                                 "mb": UNKNOWN})
                continue
            mb = per * conc
            note = f"per_unit {per} × 宣告並行度 {conc}"
        else:
            mb = a.get("footprint_mb")
            if not isinstance(mb, (int, float)):
                excluded.append({"name": name, "reason": "實測 footprint 量不到",
                                 "mb": UNKNOWN})
                continue
            note = "實測 footprint"
        counted.append({"name": name, "mb": round(mb, 1), "note": note})

    # 實測是 L4 但沒宣告 → 不計入（意外常駐，回 L1／L2）
    for name, a in measured.items():
        if a.get("layer") == "L4" and name not in declared:
            excluded.append({"name": name,
                             "reason": "實測是 L4 但未宣告 → 意外常駐，回 L1／L2 處理",
                             "mb": a.get("footprint_mb", UNKNOWN)})

    total = round(sum(c["mb"] for c in counted), 1) if counted else 0.0
    return {"baseline_mb": total, "counted": counted, "excluded": excluded,
            "intersection_empty": not counted}


# --- 四道閘門 ---------------------------------------------------------------

# G0 的證據項。每一項只在「present」時是正面證據；
# ⛔ absent 不是反面證據 —— 缺乏 X 的證據不等於非 X 的證據（見第三次預先登記的理由）。
MEMORY_EVIDENCE = {
    "M1_swapouts_rising": "Swapouts > 0 且在觀測窗內增加",
    "M2_ps_truncated": "ps -ax 計數顯著少於 pgrep -f .（ps 在壓力下被截斷）",
    "M3_compressor_absorbing": "壓縮器在觀測窗內持續吸收，且 φ 極低",
    "M4_pressure_low_falling": "memory_pressure 自報可用率低且下降",
}
COMPUTE_EVIDENCE = {
    "C1_single_process_saturating": "單一 process 瞬時 CPU 遠超 100%（top -l 2 第二次採樣）",
    "C2_load_high_without_memory": "λ 高，且無任何記憶體證據",
}


def gate_g0(meas: dict) -> dict:
    """
    G0 軸線：問兩個獨立的問題 —— 有沒有記憶體的正面證據？有沒有運算的正面證據？

    📌 第三次預先登記（2026-09-03）改成這個結構。原本是「四項判據各投一票」，
       但那四項的兩欄不對稱：`ps` 被截斷是記憶體壓力的正面證據，
       `ps` 沒被截斷卻跟每一種狀態都相容（閒置、CPU 打滿、記憶體吃緊但未達截斷門檻），
       鑑別力是零。把鑑別力為零的讀數標成「指向運算」就是製造訊號 ——
       與 A1–A4「量測失敗時給出一個看起來正常的數字」同型，只是這次是
       「缺乏證據時給出一個看起來像證據的標籤」。判據 1 與 4 有同樣的不對稱。

    ⚠️ **這次修訂的方向有利於申請方**（它把「三項記憶體＋一項空洞的運算」從矛盾變成過關）。
       第一次修訂的方向相反，所以那次可以拿方向當「不是裁剪」的證據，這次不能。
       這次的理由只能靠論證本身站住，而且必須寫在審查文件的最前面，不得藏在修訂紀錄裡。

    每一項證據由分析者標 present／absent／unknown 並附值；
    腳本驗證取樣點數與組合邏輯 —— ⛔ 仍然不自己訂「memory_pressure 幾 % 算低」。
    """
    samples = meas.get("g0_samples") or []
    if len(samples) < 2:
        return {"status": BLOCKED,
                "reason": f"只有 {len(samples)} 個取樣點。證據必須在不同負載時點各取一次，"
                          "不得用單一快照"}

    all_keys = list(MEMORY_EVIDENCE) + list(COMPUTE_EVIDENCE)
    per_sample = []
    for i, s in enumerate(samples):
        st = {k: (s.get(k) or {}).get("state") for k in all_keys}
        bad = [k for k, v in st.items() if v not in ("present", "absent", "unknown")]
        if bad:
            return {"status": BLOCKED,
                    "reason": f"取樣 {i + 1} 的證據項 {bad} 沒有標注狀態。"
                              "每一項都要標 present／absent／unknown。"
                              "⛔ 標不出來就是 unknown，不要留空也不要猜"}
        per_sample.append(st)

    mem_hits = sorted({k for d in per_sample for k in MEMORY_EVIDENCE
                       if d[k] == "present"})
    cpu_hits = sorted({k for d in per_sample for k in COMPUTE_EVIDENCE
                       if d[k] == "present"})
    detail = {"memory_evidence_present": mem_hits, "compute_evidence_present": cpu_hits,
              "per_sample": per_sample}

    if mem_hits and not cpu_hits:
        return {"status": PASS,
                "reason": f"有記憶體的正面證據 {mem_hits}，無運算的正面證據", **detail}
    if cpu_hits and not mem_hits:
        return {"status": FAIL, "verdict": "不受理",
                "reason": f"有運算的正面證據 {cpu_hits}，無記憶體的正面證據 → "
                          "本標準不受理第三層。⛔ 升記憶體不會修好它；"
                          "報告寫明並收在第二層", **detail}
    if mem_hits and cpu_hits:
        return {"status": BLOCKED,
                "reason": f"兩種正面證據同時存在（記憶體 {mem_hits}／運算 {cpu_hits}）。"
                          "⛔ 不要硬判 —— 把值都列出來，說明無法歸因，停在這裡", **detail}
    return {"status": BLOCKED,
            "reason": "兩種正面證據都沒有 —— 這台在觀測窗內沒有成形的瓶頸。"
                      "請在有負載的時點重取；⛔ 不得把「沒有瓶頸」讀成任一方的證據",
            **detail}


def gate_g1(decl: dict, meas: dict) -> dict:
    """G1 前兩層窮盡。每條要有前→後；L3 的 A/B 四欄缺一即不過。"""
    problems = []

    ev = meas.get("g1_evidence") or []
    if not ev:
        problems.append("沒有任何第一／二層的重量證據")
    for e in ev:
        name = e.get("item", "<未命名>")
        if not e.get("remeasure_cmd"):
            problems.append(f"「{name}」沒有當場重量指令")
        if e.get("before") in (None, "", UNKNOWN) or e.get("after") in (None, "", UNKNOWN):
            problems.append(f"「{name}」只有宣稱、沒有前→後的實際數字")

    ab = meas.get("g1_l3_ab") or []
    cols = ("success_rate", "e2e_time", "e2e_bill", "resident_footprint_mb")
    for row in ab:
        cand = row.get("item") or row.get("candidate") or "<未命名>"
        miss = [c for c in cols if not isinstance(row.get(c), (int, float))]
        if miss:
            problems.append(f"L3 候選「{cand}」的 A/B 缺欄 {miss} → "
                            "⛔ 不得用三欄推論")
        if row.get("alternative_disproved") not in (True, False):
            problems.append(f"L3 候選「{cand}」沒有給 alternative_disproved "
                            "（該替代到底有沒有被否證）")

    # ⛔ 第五次預先登記：候選集完整性。原本只檢查「列出來的每一列四欄齊不齊」，
    #    沒檢查「該列的候選有沒有全部列出來」—— 於是只測最好測的那一個就能過關。
    cands, unanswered = l3_candidates(decl, meas)
    if unanswered:
        problems.append(f"這些宣告項沒有回答「有沒有不需常駐的替代實作」：{unanswered}。"
                        "⛔ 這一問不得省略 —— 省略它就能把項目直接放進 L4 而不必面對 A/B")
    tested = set(ab_outcome(meas))
    missing = sorted(cands - tested)
    if missing:
        problems.append(f"這些 L3 候選還沒跑 A/B 四欄：{missing}。"
                        "⛔ 候選集要跑完，不得只測最好測的那一個")
    if not cands and not ab:
        problems.append("沒有任何 L3 候選也沒有 A/B —— 請確認每個宣告項都答過"
                        "「有沒有不需常駐的替代實作」")

    l1 = meas.get("l1_root_cause_fixed")
    if l1 is not True:
        problems.append("L1 沒有回報「修了成因」（手動清一次不算，"
                        "重開機後要複測仍為零）")

    return ({"status": PASS, "reason": "前兩層的證據齊備"} if not problems
            else {"status": FAIL, "verdict": "不受理，退回該層", "problems": problems})


def gate_g2(bl: dict, r: dict, meas: dict) -> dict:
    """G2 缺口實在。交集為空、或降載無劣化，都不過。"""
    if bl["intersection_empty"]:
        return {"status": FAIL, "verdict": "不受理",
                "reason": "宣告與實測 L4 的交集為空 → 沒有可提報的缺口"}

    dc = meas.get("downclock_experiment") or {}
    if dc.get("ran") is not True:
        return {"status": BLOCKED,
                "reason": "降載實驗尚未執行 → G2 不算過。⛔ 不得以「我覺得會很慢」代替"}
    tasks = dc.get("tasks") or []
    if len(tasks) < 10:
        return {"status": FAIL,
                "reason": f"降載實驗只有 {len(tasks)} 個任務，未達 ≥10 個近期真實任務"}
    if dc.get("degraded") is False:
        return {"status": FAIL, "verdict": "L4 分類錯誤，退回第一層",
                "reason": "降載實驗無劣化 → 那些項目其實不是 L4。"
                          "⛔ 不得解釋成「任務集不夠難」再重挑任務集"}
    if dc.get("degraded") is not True:
        return {"status": BLOCKED, "reason": "降載實驗的劣化判定是 UNKNOWN"}

    notes = []
    if r.get("sigma") == UNDEFINED:
        notes.append("σ 為 UNDEFINED（swap 未配置）→ ⛔ 不得以 σ 支撐缺口，"
                     "改以 Swapouts 為主判據")
    return {"status": PASS, "reason": "缺口有降載實測支撐", "notes": notes}


def gate_g3(decl: dict, meas: dict, baseline_mb=None) -> dict:
    """G3 替代已證否。可事後升級、共享替代可行、階梯未證否，都不過。"""
    problems = []
    m = decl.get("machine") or {}
    if m.get("ram_upgradeable") is True:
        problems.append("機型可事後升級 → 不走第三層，走加購記憶體")
    elif m.get("ram_upgradeable") is not False:
        problems.append("沒有正面陳述機型可否事後升級（這一問不得省略）")

    for d in decl.get("incompressible") or []:
        name = d.get("name", "<未命名>")
        reason = (d.get("shared_alternative_reason") or "").strip()
        if not reason:
            problems.append(f"「{name}」沒有回答共享替代（不得留空）")
        elif reason == "無" and d.get("observation"):
            problems.append(f"「{name}」的共享替代理由是「無」→ "
                            "可由共用服務滿足者不受理")
        elif reason != "無" and reason not in SHARED_ALT_REASONS:
            problems.append(f"「{name}」的共享替代理由「{reason}」不在四個正當理由內："
                            f"{sorted(SHARED_ALT_REASONS)}")

    ladder = meas.get("spec_ladder")
    notes = []
    if not isinstance(ladder, dict):
        problems.append("沒有規格階梯（證否式或宣告式擇一，不得留空）")
    else:
        policy = ladder.get("policy", "disproof")
        if policy == "disproof":
            if ladder.get("next_cheaper_insufficient") is not True:
                problems.append("規格階梯只證明本案規格夠，未證明「下一階較便宜的規格不足」")
            if not (ladder.get("evidence") or "").strip():
                problems.append("規格階梯證否沒有附證據")
        elif policy == "declared_headroom":
            # ⚠️ 宣告式：以「覆蓋應然基線的那一階，再加一階」定機型。
            #    比證否式寬鬆 —— 它不要求證明下一階不足，而是明白地買餘裕。
            #    唯一的硬要求是**錨點必須是應然基線不是現值**。
            tiers = ladder.get("tiers_gb")
            anchor = ladder.get("anchor_mb")
            rec = ladder.get("recommended_gb")
            above = ladder.get("tiers_above", 1)
            if not (isinstance(tiers, list) and tiers):
                problems.append("宣告式階梯要列出可選規格階（tiers_gb）")
            elif not isinstance(anchor, (int, float)):
                problems.append("宣告式階梯要給錨點（anchor_mb）")
            elif abs(anchor - baseline_mb) > 1:
                problems.append(
                    f"錨點 {anchor} MB 不等於應然基線 {baseline_mb} MB。"
                    "標準明訂不得以現值當需求基準 —— 現值可能已經含該被刪掉的浪費")
            else:
                cover = next((t for t in sorted(tiers) if anchor / 1024.0 <= t),
                             sorted(tiers)[-1])
                idx = sorted(tiers).index(cover)
                expect = sorted(tiers)[min(idx + above, len(tiers) - 1)]
                if rec != expect:
                    problems.append(f"宣告式階梯：錨點 {anchor / 1024:.1f} GB 的覆蓋階是 "
                                    f"{cover} GB，加 {above} 階應為 {expect} GB，"
                                    f"但建議寫 {rec} GB")
                else:
                    notes.append(f"宣告式階梯：錨點 {anchor / 1024:.1f} GB → 覆蓋階 "
                                 f"{cover} GB → 加 {above} 階 → 建議 {expect} GB。"
                                 "⚠️ 這是宣告的餘裕政策，不是證否 —— "
                                 "審查文件要寫明它比證否式寬鬆")
        else:
            problems.append(f"未知的規格階梯政策「{policy}」"
                            "（只接受 disproof 或 declared_headroom）")

    return ({"status": PASS, "reason": "替代已證否", "notes": notes} if not problems
            else {"status": FAIL, "verdict": "不受理", "problems": problems})


# --- 組裝 -------------------------------------------------------------------

def evaluate(decl: dict, meas: dict) -> dict:
    bl = baseline(decl, meas)
    r = ratios(decl, meas, bl["baseline_mb"])
    g = {"G0": gate_g0(meas), "G1": gate_g1(decl, meas),
         "G2": gate_g2(bl, r, meas), "G3": gate_g3(decl, meas, bl["baseline_mb"])}

    order = ["G0", "G1", "G2", "G3"]
    stopped_at, verdict = None, None
    for k in order:                      # ⛔ 不得跳層：停在第一道不過的閘門
        if g[k]["status"] != PASS:
            stopped_at = k
            verdict = "不受理" if g[k]["status"] == FAIL else "停住（無法判定）"
            break
    if stopped_at is None:
        verdict = "受理 —— 但這是遷就現況的權宜推薦，不是本 skill 的成就"

    return {
        "verdict": verdict,
        "stopped_at": stopped_at,
        "gates": g,
        "ratios": r,
        "baseline": bl,
        "declared": {
            "concurrency": decl.get("concurrency_declared", UNKNOWN),
            "degradation_definition": decl.get("degradation_definition", UNKNOWN),
            "task_set_rule": decl.get("task_set_rule", UNKNOWN),
            "ram_upgradeable": (decl.get("machine") or {}).get("ram_upgradeable", UNKNOWN),
        },
        "_note": "門檻是證據要求不是數值（portability.md C1；裝置 profile n = 2）。"
                 "declared 欄位是提案者的宣告，不是校準過的門檻。",
    }


def render(res: dict) -> str:
    L = ["=" * 62, "第三層受理判定 — audit-standard.md 四道閘門", "=" * 62]
    a = L.append
    a(f"結論　　{res['verdict']}")
    if res["stopped_at"]:
        a(f"停在　　{res['stopped_at']}")
    a("")
    a("--- 四道閘門 ---")
    for k in ("G0", "G1", "G2", "G3"):
        gg = res["gates"][k]
        a(f"  {k}  {gg['status']}")
        if gg.get("reason"):
            a(f"      {gg['reason']}")
        for p in gg.get("problems", []):
            a(f"      ⛔ {p}")
        for n in gg.get("notes", []):
            a(f"      ⚠️ {n}")
    a("")
    a("--- 無因次比值（提報用，不是及格線）---")
    for k, v in res["ratios"].items():
        a(f"  {k:<7} {v}")
    a("")
    bl = res["baseline"]
    a(f"--- 應然基線 = 宣告 ∩ 實測 L4：{bl['baseline_mb']} MB ---")
    for c in bl["counted"]:
        a(f"  ✅ {c['name']:<22} {c['mb']:>8} MB  （{c['note']}）")
    for e in bl["excluded"]:
        a(f"  ⛔ {e['name']:<22} {str(e['mb']):>8}     {e['reason']}")
    a("=" * 62)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(
        description="依 audit-standard.md 的四道閘門判定第三層是否受理")
    ap.add_argument("--declaration", required=True, help="工作負載宣告 JSON（人）")
    ap.add_argument("--measurement", required=True, help="量測結果 JSON（機）")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    args = ap.parse_args()

    try:
        decl = json.loads(Path(args.declaration).read_text(encoding="utf-8"))
        meas = json.loads(Path(args.measurement).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"[ERROR] 讀不到或解不開輸入：{e}")

    res = evaluate(decl, meas)
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else render(res))


if __name__ == "__main__":
    main()
