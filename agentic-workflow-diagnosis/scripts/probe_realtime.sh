#!/usr/bin/env bash
# 即時性干擾軸的取樣器 —— 量「窗內尾端」，不是總量或平均。
#
# 用途：在一通真的視訊會議裡，對「AI 閒置段」與「AI 執行段」各跑一次，比較尾端值。
# 唯讀、免 sudo、不修改任何設定。
#
# ⛔ 這一軸不回報平均。一次 200 ms 的停頓就毀掉一通會議，而它在平均裡看不見。
# ⛔ 觀測者效應在這一軸比別處嚴重：取樣器自己吃 CPU，而被量的是延遲敏感的任務。
#    所以開場先空跑量自己的成本並印出來；超過單核 5% 就自動降頻。
# ⛔ 主機側指標只解釋因果，不是結果指標。結果指標是客戶端自己的掉格／凍結數
#    （Google Meet on Chrome：chrome://webrtc-internals）。拿不到就標 UNKNOWN，
#    不得用主機側數字冒充（R1）。
#
# 用法：
#   ./probe_realtime.sh 120 idle      # AI 閒置段，120 秒
#   ./probe_realtime.sh 120 busy      # AI 執行段，120 秒
#   ./probe_realtime.sh --selftest    # 只量取樣器自己的成本
#
# 平台：macOS（與 environment.md 同一範圍）。其他平台請走 portability.md 的自陳版。

set -uo pipefail

DUR="${1:-120}"
LABEL="${2:-unlabeled}"
HZ=1
SELFTEST_SECS=10
OUT="${PROBE_OUT:-$(pwd)/realtime-${LABEL}-$(date +%Y%m%d-%H%M%S).tsv}"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "[ERROR] 本腳本只支援 macOS。其他平台走 portability.md 的人工自陳版，" >&2
  echo "        並在報告寫明這一軸是盲區。⛔ 不要假裝跑得出數字。" >&2
  exit 2
fi

# --- 取數原子（每一項的成本都量過，見 interference.md）-----------------------
# 逐項實測：vm_stat 11.0 ms｜sysctl(合併) 10.8 ms｜pgrep -x claude 31.9 ms
#           docker ps -q 84.7 ms｜top -l 2 2,540.8 ms
#
# ⛔ 迴圈裡只留「會逐秒變、且解釋得了卡頓」的東西。
#    第一版把 docker ps 與 pgrep 放進 1 Hz 迴圈、又呼叫了三次 vm_stat，
#    自量出來是 200.6 ms／次 = 單核 20.1% —— 對延遲敏感的量測完全不可接受，
#    取樣器自己就成了干擾源。並行度不會逐秒變，移到頭尾各取一次即可。
# ⛔ CPU 一律走背景 top 串流，不放進迴圈（陷阱五：ps -o pcpu 是生命期平均）。
sample_line() {
  # 一次 vm_stat 取三個計數器（呼叫三次要 33 ms，一次 11 ms）
  vm_stat | awk -F: '
    /Pageins/  {gsub(/[^0-9]/,"",$2); pi=$2}
    /Swapouts/ {gsub(/[^0-9]/,"",$2); so=$2}
    /Swapins/  {gsub(/[^0-9]/,"",$2); si=$2}
    END {printf "%s\t%s\t%s\t", (pi?pi:0), (so?so:0), (si?si:0)}'
  # 一次 sysctl 取兩個值
  sysctl -n vm.loadavg vm.swapusage | awk '
    NR==1 {lo=$2}
    NR==2 {for(i=1;i<=NF;i++) if($i=="used"){gsub(/M/,"",$(i+2)); swu=$(i+2)}}
    END {printf "%s\t%s\n", (lo?lo:0), (swu?swu:0)}'
}

# 並行度：頭尾各取一次（docker ps 84.7 ms + pgrep 31.9 ms，不進迴圈）
concurrency_snapshot() {
  printf 'claude=%s containers=%s\n' \
    "$(pgrep -x claude 2>/dev/null | wc -l | tr -d ' ')" \
    "$(docker ps -q 2>/dev/null | wc -l | tr -d ' ')"
}

# --- 自量成本（開場必做，不可跳）---------------------------------------------
selftest() {
  local n=0 s e
  s=$(python3 -c 'import time;print(time.time())')
  local deadline=$(( $(date +%s) + SELFTEST_SECS ))
  while [ "$(date +%s)" -lt "$deadline" ]; do sample_line >/dev/null; n=$((n+1)); done
  e=$(python3 -c 'import time;print(time.time())')
  python3 - "$s" "$e" "$n" "$SELFTEST_SECS" <<'PY'
import sys
s,e,n,secs=float(sys.argv[1]),float(sys.argv[2]),int(sys.argv[3]),int(sys.argv[4])
per=(e-s)/max(n,1)*1000
share=per/1000*100     # 1 Hz 下佔單核的百分比
print(f"  取樣器自身成本：單次 {per:.1f} ms → 1 Hz 下約佔單核 {share:.1f}%")
print(f"  （{secs} 秒內完成 {n} 次取樣）")
print("OVER" if share>5 else "OK")
PY
}

echo "=============================================================="
echo "即時性干擾取樣器 — 先量自己的成本（觀測者效應）"
echo "=============================================================="
ST=$(selftest)
echo "$ST" | grep -Ev '^(OK|OVER)$'
if echo "$ST" | grep -q '^OVER$'; then
  HZ=0.5
  echo "  ⚠️ 自身成本超過單核 5% → 降到 0.5 Hz。報告要寫明取樣頻率與這個數字。"
else
  echo "  → 維持 1 Hz"
fi

[ "${1:-}" = "--selftest" ] && exit 0

# --- CPU 走背景串流（實測 4 秒得 3 筆 ≈ 1 Hz；第二筆之後才是區間值）---------
# ⛔ 陷阱五：ps -o pcpu 是 process 生命期平均，A/B 型量測會得到偽陰性。
TOPLOG=$(mktemp -t probe_rt_top)
top -l 0 -s 1 -n 6 -o cpu -stats pid,cpu,command > "$TOPLOG" 2>/dev/null &
TOPPID=$!
cleanup() { kill "$TOPPID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo
CONC_START=$(concurrency_snapshot)
echo "取樣中：${DUR} 秒，標籤 ${LABEL}，頻率 ${HZ} Hz"
echo "  起始並行度：$CONC_START"
echo "  → 輸出 $OUT"
printf 'ts\tpageins\tswapouts\tswapins\tload1\tswap_used_mb\n' > "$OUT"
END=$(( $(date +%s) + DUR ))
SLEEP=$(python3 -c "print(1/$HZ)")
while [ "$(date +%s)" -lt "$END" ]; do
  printf '%s\t' "$(date +%s)" >> "$OUT"
  sample_line >> "$OUT"
  python3 -c "import time;time.sleep($SLEEP)"
done
cleanup
CONC_END=$(concurrency_snapshot)
echo "  結束並行度：$CONC_END"

# --- 回報尾端，不回報平均 -----------------------------------------------------
python3 - "$OUT" "$TOPLOG" "$LABEL" "$HZ" <<'PY'
import sys, re
path, toplog, label, hz = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
rows=[l.rstrip("\n").split("\t") for l in open(path,encoding="utf-8")][1:]
if len(rows)<3:
    print("\n[UNKNOWN] 取樣筆數不足（<3），無法算尾端。⛔ 不要用不足的樣本硬算 p95。")
    sys.exit(0)
def col(i): return [float(r[i]) for r in rows]
def deltas(i):
    v=col(i); return [max(0.0,(v[k+1]-v[k])) for k in range(len(v)-1)]
def tail(xs, unit):
    if not xs: return "UNKNOWN（無資料）"
    xs=sorted(xs); n=len(xs)
    p95=xs[min(n-1,int(round(0.95*(n-1))))]
    return f"p95={p95:.1f} max={max(xs):.1f} {unit}（n={n}）"

print()
print("="*62)
print(f"尾端統計 — 標籤 {label}｜取樣 {hz} Hz｜{len(rows)} 筆")
print("="*62)
print("⛔ 本節刻意不輸出平均。一次停頓就毀掉一通會議，平均會把它抹掉。")
print()
print(f"  分頁換入速率 Pageins/取樣  {tail(deltas(1),'頁')}")
print(f"  換出速率     Swapouts/取樣 {tail(deltas(2),'頁')}")
print(f"  換入速率     Swapins/取樣  {tail(deltas(3),'頁')}")
print(f"  load(1m)                   {tail(col(4),'')}")
print(f"  swap 已用                  {tail(col(5),'MB')}")
print("  並行度（claude／容器）      見上方頭尾兩次快照 —— 不逐秒取，")
print("                             因為 docker ps 84.7 ms 會讓取樣器自己變成干擾源")

# top 串流：跳過第一次採樣（無前一次可比，數字不可信）
blocks=[]; cur=None
for line in open(toplog,encoding="utf-8",errors="replace"):
    if line.startswith("PID"):
        if cur is not None: blocks.append(cur)
        cur=[]
    elif cur is not None:
        m=re.match(r"\s*(\d+)\s+([\d.]+)\s+(.*)",line)
        if m: cur.append((m.group(3).strip(), float(m.group(2))))
if cur: blocks.append(cur)
blocks=blocks[1:]          # ⛔ 陷阱五：第一次採樣不可信，一律丟掉
if not blocks:
    print("\n  瞬時 CPU  UNKNOWN（top 串流無有效採樣；⛔ 不得退回 ps -o pcpu）")
else:
    agg={}
    for b in blocks:
        for name,cpu in b: agg.setdefault(name,[]).append(cpu)
    print(f"\n  瞬時 CPU 榜（{len(blocks)} 次有效採樣，已丟掉第一次）")
    for name,vals in sorted(agg.items(), key=lambda kv:-max(kv[1]))[:6]:
        print(f"    {name[:30]:<32} max={max(vals):6.1f}%  p95={sorted(vals)[min(len(vals)-1,int(round(0.95*(len(vals)-1))))]:6.1f}%")
print()
print("下一步：把同一通會議的 idle 段與 busy 段兩份輸出並排比較尾端值。")
print("⛔ 不得跨會議比較 —— 網路條件、參與人數、畫面配置都會變。")
print("⛔ 結果指標仍在客戶端（webrtc-internals 的 framesDropped／freezeCount）；")
print("   本節只解釋因果。拿不到客戶端數字就標 UNKNOWN，不得用這裡的數字冒充。")
PY
rm -f "$TOPLOG"
echo "=============================================================="
echo "以上皆為唯讀量測。未修改任何設定。"
echo "=============================================================="
