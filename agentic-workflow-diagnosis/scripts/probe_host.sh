#!/usr/bin/env bash
# probe_host.sh — 當下現況 probe。
#
# 為什麼有這支：同一次診斷裡，live probe 的訊號量遠勝歷史語料。
# 某機的歷史語料只有 7 個 session，五個指標全部只能標「暫定」；
# 但 live probe 直接定出瓶頸歸因（防毒常駐吃掉 2.5/8 核）、抓到 3 處設定重複、
# 並判出「第三層不受理」。結論由 probe 決定，語料只提供背景。
#
# 兩條硬規則
#   1. 只輸出統計量與設定結構。不得輸出對話內容、程式碼或業務資訊。
#   2. 唯讀。本腳本不修改任何設定、不刪除任何東西。
#      看到可疑的東西一律回報，由人決定 —— 第二層的動作多半不可逆。
#
# 平台：macOS。其他平台請見 portability.md 的人工自陳版。

set -uo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "[ERROR] 本腳本只支援 macOS（用到 vm_stat / vmmap / memory_pressure / systemextensionsctl）。"
  echo "        其他平台請走 portability.md 的人工自陳版，不要用這裡的數字。"
  exit 2
fi

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

hr() { printf '%s\n' "==============================================================" ; }
sec() { printf '\n--- %s ---\n' "$1" ; }

hr; echo "當下現況 probe — $(date -u '+%Y-%m-%dT%H:%M:%SZ')"; hr

# ── 硬體與負載 ────────────────────────────────────────────────
sec "硬體"
printf 'model=%s cores=%s mem_bytes=%s\n' \
  "$(sysctl -n hw.model)" "$(sysctl -n hw.ncpu)" "$(sysctl -n hw.memsize)"
sw_vers | tr '\n' ' '; echo
uptime

# ── 記憶體壓力 ────────────────────────────────────────────────
# sigma 在 swap 未配置時是 0/0 = UNDEFINED，不是 0。
# Swapouts 自開機單調遞增，是「洩壓閥開過沒有」最乾淨的判據。
sec "記憶體壓力"
SWAP=$(sysctl -n vm.swapusage)
echo "swapusage: $SWAP"
SWAP_TOTAL=$(printf '%s' "$SWAP" | sed -n 's/.*total = \([0-9.]*\)M.*/\1/p')
if [ "${SWAP_TOTAL:-0}" = "0.00" ] || [ -z "${SWAP_TOTAL:-}" ]; then
  echo "sigma: UNDEFINED（swap 未配置，0/0 不是 0）"
else
  echo "sigma: 見 swapusage 的 used/total"
fi
vm_stat | awk -F: '
  /Pages free|stored in compressor|occupied by compressor|Swapouts|Pageouts/ {
    gsub(/[ .]/,"",$2); print $1 "=" $2
  }'
echo "memory_pressure: $(memory_pressure 2>/dev/null | sed -n 's/.*free percentage: *//p')"

# ── ps / pgrep 一致性 ────────────────────────────────────────
# 兩者計數差很多本身就是記憶體壓力的證據（ps 在高壓下回傳截斷結果）。
# ⛔ macOS 的 pgrep 不支援 -c，一律用 pgrep -f X | wc -l。
sec "process 計數一致性"
PS_N=$(ps -ax | wc -l | tr -d ' ')
PGREP_N=$(pgrep -f . | wc -l | tr -d ' ')
echo "ps=$PS_N pgrep=$PGREP_N"
if [ "$PS_N" -gt 0 ] && [ "$PGREP_N" -gt 0 ]; then
  DIFF=$(( PS_N > PGREP_N ? PS_N - PGREP_N : PGREP_N - PS_N ))
  if [ "$DIFF" -gt $(( PGREP_N / 5 )) ]; then
    echo "⚠️ 差距 > 20% → ps 可能被截斷，這本身是壓力證據"
  else
    echo "一致 → 無壓力截斷跡象"
  fi
fi

# ── 瞬時 CPU 榜首 ────────────────────────────────────────────
# ⛔ 不用 ps -o pcpu：那是 process 生命期平均，對跑了很久的 process 會給偽陰性。
#    A/B 型的 CPU 量測一律用 top -l 2 取第二次採樣。
sec "瞬時 CPU 榜首（top -l 2 第二次採樣）"
top -l 2 -o cpu -n 8 -stats pid,cpu,mem,command 2>/dev/null \
  | awk '/^PID/{n++} n==2'

# ── MCP 設定重複 ─────────────────────────────────────────────
sec "MCP 設定去重"
python3 - "$CLAUDE_DIR" <<'PY'
import json, pathlib, sys, collections
claude_dir = pathlib.Path(sys.argv[1])
seen = collections.Counter()
where = collections.defaultdict(list)

def harvest(obj, origin, path="<root>"):
    if isinstance(obj, dict):
        ms = obj.get("mcpServers")
        if isinstance(ms, dict):
            for name in ms:
                seen[name] += 1
                where[name].append(f"{origin}:{path}")
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and k != "mcpServers":
                harvest(v, origin, k)
    elif isinstance(obj, list):
        for v in obj:
            harvest(v, origin, path)

for p in (pathlib.Path.home() / ".claude.json", claude_dir / ".mcp.json"):
    try:
        harvest(json.loads(p.read_text(encoding="utf-8")), p.name)
    except (OSError, json.JSONDecodeError):
        continue

dupes = {n: c for n, c in seen.items() if c > 1}
print(f"server 總數={len(seen)} 重複定義={len(dupes)}")
for n, c in sorted(dupes.items()):
    print(f"  ⚠️ {n} 被定義 {c} 次 → {', '.join(where[n])}")
if not dupes:
    print("  無重複定義")
PY

sec "MCP process 實況"
MCP_PROCS=$(pgrep -f 'mcp' | wc -l | tr -d ' ')
echo "符合 'mcp' 的 process=$MCP_PROCS"
pgrep -f 'mcp' 2>/dev/null | while read -r p; do
  printf '  %s\t%s\n' "$p" "$(ps -o comm= -p "$p" 2>/dev/null | sed 's#.*/##')"
done
echo "容器（docker/colima/podman）=$(pgrep -f 'docker|colima|podman' | wc -l | tr -d ' ')"

# ── hook 重複定義 ────────────────────────────────────────────
# 成本不在記憶體，在每個 session 的 context —— 同一段提示被注入 N 次。
sec "hook 重複定義"
python3 - "$CLAUDE_DIR" <<'PY'
import json, pathlib, sys, collections
claude_dir = pathlib.Path(sys.argv[1])
found = False
for name in ("settings.json", "settings.local.json"):
    p = claude_dir / name
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        continue
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        continue
    for event, matchers in hooks.items():
        if not isinstance(matchers, list):
            continue
        cmds = [h.get("command") for m in matchers
                if isinstance(m, dict)
                for h in m.get("hooks", []) if isinstance(h, dict)]
        for cmd, n in collections.Counter(c for c in cmds if c).items():
            if n > 1:
                found = True
                print(f"  ⚠️ {name} {event}：同一命令重複 {n} 次 → {cmd}")
if not found:
    print("  無重複定義")
PY

# ── 系統擴充孤兒 ─────────────────────────────────────────────
# 「已解除安裝」的產品其系統擴充可能仍註冊在案。
sec "系統擴充"
systemextensionsctl list 2>/dev/null \
  | awk '/activated enabled/ {for(i=1;i<=NF;i++) if($i ~ /^[a-z0-9]+\./) {print "  " $i; break}}' \
  | sort -u

# ── 保留期與語料涵蓋 ─────────────────────────────────────────
# ⛔ 偵測到截斷不要建議延長保留期。要機器配合工具是本末倒置 ——
#    聲明限制、限縮結論，判斷重心走 live probe。
sec "session 語料涵蓋"
python3 - "$CLAUDE_DIR" <<'PY'
import json, pathlib, sys
claude_dir = pathlib.Path(sys.argv[1])
proj = claude_dir / "projects"
files = list(proj.rglob("*.jsonl")) if proj.is_dir() else []
days = None
for name in ("settings.json", "settings.local.json"):
    try:
        cfg = json.loads((claude_dir / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        continue
    if isinstance(cfg.get("cleanupPeriodDays"), int):
        days = cfg["cleanupPeriodDays"]
        break
print(f"  jsonl 檔數={len(files)} 保留期={days if days is not None else '30（預設，未設定）'} 天")
print("  → 與 list_sessions 的最舊 lastActivityAt 對照，落差即為已被刪除的部分")
print("  ⛔ 不要為了讓數字好看去延長保留期；聲明限制即可")
PY

hr
echo "以上皆為唯讀量測。任何清除／刪除動作都請先人工確認對象確實可刪。"
hr
