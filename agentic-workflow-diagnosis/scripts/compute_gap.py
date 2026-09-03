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
        layer = a.get("layer")
        if layer != "L4":
            excluded.append({"name": name,
                             "reason": f"宣告了但實測落在 {layer or UNKNOWN} 不是 L4 → 不計入",
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

def gate_g0(meas: dict) -> dict:
    """
    G0 軸線。四項判據的方向由分析者標注（"memory"／"compute"／"inconclusive"），
    腳本驗證取樣點數與組合邏輯 —— ⛔ 不自己訂 memory_pressure 幾 % 算低。
    """
    samples = meas.get("g0_samples") or []
    if len(samples) < 2:
        return {"status": BLOCKED,
                "reason": f"只有 {len(samples)} 個取樣點。判據必須在不同負載時點各取一次，"
                          "不得用單一快照"}
    keys = ("swapouts", "memory_pressure", "ps_vs_pgrep", "single_cpu")
    per_sample = []
    for i, s in enumerate(samples):
        dirs = {k: (s.get(k) or {}).get("direction") for k in keys}
        missing = [k for k, v in dirs.items() if v not in
                   ("memory", "compute", "inconclusive")]
        if missing:
            return {"status": BLOCKED,
                    "reason": f"取樣 {i + 1} 的判據 {missing} 沒有標注方向。"
                              "每一項都要明確標 memory／compute／inconclusive"}
        per_sample.append(dirs)

    # 四種情況，逐一判定。
    #
    # 📌 修訂（第二次預先登記，2026-09-03）：原本的規則是「四項在**每一個**取樣點
    #    都一致指向運算」才不受理。第一次真的套用就發現它過嚴：某台機器 12 個判據讀數
    #    裡 11 個指向運算、0 個指向記憶體，只因為有一個取樣點落在閒置時刻
    #    （突發的 CPU 消耗者剛結束，`single_cpu` 標成 inconclusive）就輸出「無法歸因」。
    #    G0 問的是「瓶頸是不是記憶體」；**沒有任何一項指向記憶體時，這個問題已經有答案了**，
    #    閒置時刻的 inconclusive 不該把它救回來。
    #    改法對「有判據指向記憶體」的機器不生效 —— 那些走情況 3／4，結果不變。
    mem_any = any(d[k] == "memory" for d in per_sample for k in keys)
    compute_any = any(d[k] == "compute" for d in per_sample for k in keys)
    all_compute_samples = [i + 1 for i, d in enumerate(per_sample)
                           if all(d[k] == "compute" for k in keys)]

    # 情況 1：沒有任何判據指向記憶體，且至少一個取樣點四項全指向運算 → 不受理
    if not mem_any and all_compute_samples:
        return {"status": FAIL, "verdict": "不受理",
                "reason": f"沒有任何判據指向記憶體，且取樣 {all_compute_samples} "
                          "四項全指向運算 → 本標準不受理第三層。"
                          "⛔ 升記憶體不會修好它；報告寫明並收在第二層",
                "per_sample": per_sample}

    # 情況 2：有的指向記憶體、有的指向運算 → 真正的矛盾，不硬判
    if mem_any and compute_any:
        return {"status": BLOCKED,
                "reason": "判據互相矛盾（同時有指向記憶體與指向運算的讀數）。"
                          "⛔ 不要硬判 —— 把四個值都列出來，說明無法歸因，停在這裡",
                "per_sample": per_sample}

    # 情況 3：完全沒有訊號（都是 inconclusive，或沒有一個取樣點成形）→ 無法歸因
    if not mem_any:
        return {"status": BLOCKED,
                "reason": "所有取樣點都沒有成形的訊號（無判據指向記憶體，"
                          "也沒有任何一個取樣點四項全指向運算）→ 無法歸因。"
                          "請在有負載的時點重取",
                "per_sample": per_sample}

    # 情況 4：有判據指向記憶體且無指向運算者 → 過
    return {"status": PASS, "reason": "判據指向記憶體軸，無指向運算的讀數",
            "per_sample": per_sample}


def gate_g1(meas: dict) -> dict:
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
    if not ab:
        problems.append("L3 的 A/B 四欄從未實測（environment.md L3 第一條：缺一不受理）")
    cols = ("success_rate", "e2e_time", "e2e_bill", "resident_footprint_mb")
    for row in ab:
        cand = row.get("candidate", "<未命名>")
        miss = [c for c in cols if not isinstance(row.get(c), (int, float))]
        if miss:
            problems.append(f"L3 候選「{cand}」的 A/B 缺欄 {miss} → "
                            "⛔ 不得用三欄推論")

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


def gate_g3(decl: dict, meas: dict) -> dict:
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
    if not isinstance(ladder, dict):
        problems.append("沒有規格階梯證否")
    else:
        if ladder.get("next_cheaper_insufficient") is not True:
            problems.append("規格階梯只證明本案規格夠，未證明「下一階較便宜的規格不足」")
        if not (ladder.get("evidence") or "").strip():
            problems.append("規格階梯證否沒有附證據")

    return ({"status": PASS, "reason": "替代已證否"} if not problems
            else {"status": FAIL, "verdict": "不受理", "problems": problems})


# --- 組裝 -------------------------------------------------------------------

def evaluate(decl: dict, meas: dict) -> dict:
    bl = baseline(decl, meas)
    r = ratios(decl, meas, bl["baseline_mb"])
    g = {"G0": gate_g0(meas), "G1": gate_g1(meas),
         "G2": gate_g2(bl, r, meas), "G3": gate_g3(decl, meas)}

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
