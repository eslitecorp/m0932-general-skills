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
# ⛔ 兩種重複要分開報，只查其中一種會漏掉另一種：
#    1. 同名被多份設定檔各定義一次（設定衝突）
#    2. 不同名字指向同一支 binary（同能力兩份，名字不同所以第 1 種查不到）
#
# ⛔ 去重的 key 必須含 **scope**（全域／哪一個專案）。同名出現在兩個不同專案層是
#    **載入範圍收斂**，不是重複 —— 一個 session 只有一個 cwd，兩者不會同時載入。
#    〔事證：依 SKILL.md 複測表把一個 MCP 從全域收斂到兩個專案層之後，本檢查
#     立刻回報「被定義 2 次」。照 skill 的建議做，skill 自己的 probe 就誤報〕
#    這與 hook 那邊「同一命令掛不同 matcher 是事件覆蓋」是同一個 A5 形態。
#    〔事證：某機的 codebase-memory 與 codebase-memory-mcp 是不同名字、同一支
#     /Users/…/.local/bin/codebase-memory-mcp，每個 session 因此起兩個 server、
#     兩個 writer 寫同一批 SQLite。只比名字的版本回報「無重複」〕
sec "MCP 設定去重"
python3 - "$CLAUDE_DIR" <<'PY'
import json, pathlib, sys, collections
claude_dir = pathlib.Path(sys.argv[1])
seen = collections.Counter()          # (server 名, scope) -> 次數
scopes = collections.defaultdict(set)  # server 名 -> {scope}
where = collections.defaultdict(list)
impl = collections.defaultdict(set)   # (command, args) -> {server 名}

def harvest(obj, origin, path="<root>"):
    if isinstance(obj, dict):
        ms = obj.get("mcpServers")
        if isinstance(ms, dict):
            for name, spec in ms.items():
                seen[(name, path)] += 1
                scopes[name].add(path)
                where[name].append(f"{origin}:{path}")
                if isinstance(spec, dict):
                    cmd = spec.get("command")
                    args = spec.get("args") or []
                    if cmd and isinstance(args, list):
                        impl[(cmd, tuple(str(a) for a in args))].add(name)
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

# 真重複＝同一個 scope 內同名被定義多次
dupes = {ns: c for ns, c in seen.items() if c > 1}
# 全域與專案層都有 → 覆寫關係，值得提醒但不是雙載
shadowed = {n: sc for n, sc in scopes.items() if len(sc) > 1 and "<root>" in sc}
# 只出現在多個專案層 → 載入範圍收斂，⛔ 不是重複，不報
aliases = {k: v for k, v in impl.items() if len(v) > 1}
names = {n for n, _ in seen}
print(f"server 總數={len(names)} 同名重複={len(dupes)} "
      f"全域被專案層覆寫={len(shadowed)} 同實作不同名={len(aliases)}")
for (n, sc), c in sorted(dupes.items()):
    print(f"  ⚠️ {n} 在同一 scope（{sc}）被定義 {c} 次 → {', '.join(where[n])}")
for n, sc in sorted(shadowed.items()):
    proj = sorted(x for x in sc if x != "<root>")
    print(f"  ℹ️ {n} 全域與專案層都有定義 → 專案層 {', '.join(proj)}（覆寫，非雙載）")
for (cmd, args), nm in sorted(aliases.items(), key=lambda kv: sorted(kv[1])):
    shown = cmd if not args else cmd + " " + " ".join(args)
    print(f"  ⚠️ {', '.join(sorted(nm))} 指向同一支實作 → {shown}")
if not dupes and not shadowed and not aliases:
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
#
# ⛔ 去重的 key 必須含 matcher。同一命令掛在不同 matcher 上是**事件覆蓋**，不是重複。
#    〔事證：某機 cbm-session-reminder 掛在 startup／resume／clear／compact 四個
#     matcher（腳本註解自己寫明是刻意的），只比 command 的版本判成「重複 4 次」——
#     誤報，而且誤報的同時漏掉了下面那個真的〕
#
# ⛔ 只讀 settings*.json 讀不到真正最貴的那一類：**同一支腳本被手動設定與
#    enabled plugin 各註冊一次**。plugin 自己的 plugin.json 也註冊 hook，兩邊都會觸發。
#    〔事證：同一台機器上 caveman 的 activate／tracker 兩支腳本，settings.json 與
#     plugin caveman@caveman 各註冊一次（檔案 byte-identical）→ 每個 prompt 注入兩份〕
#    比對用腳本檔名，不用命令字串 —— plugin 走 ${CLAUDE_PLUGIN_ROOT}/…、
#    手動設定走絕對路徑，字串不同但是同一支。
sec "hook 重複定義"
python3 - "$CLAUDE_DIR" <<'PY'
import json, pathlib, re, sys, collections
claude_dir = pathlib.Path(sys.argv[1])

SCRIPT_RE = re.compile(r"\.(js|mjs|cjs|ts|sh|bash|py)$")

def script_ids(cmd):
    """從 hook 命令抽出腳本身分（檔名）。抽不到就回空集合，不猜。"""
    out = set()
    for tok in re.findall(r"""[^\s"']+""", cmd or ""):
        base = tok.rstrip("\"'").rsplit("/", 1)[-1]
        if SCRIPT_RE.search(base):
            out.add(base)
    return out

found = False
declared = collections.defaultdict(list)   # 腳本檔名 -> settings 側來源

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
        # key 含 matcher：同 matcher 下才算重複
        seen = collections.Counter()
        for m in matchers:
            if not isinstance(m, dict):
                continue
            mt = m.get("matcher")
            for h in m.get("hooks", []):
                if not isinstance(h, dict):
                    continue
                cmd = h.get("command")
                if not cmd:
                    continue
                seen[(mt, cmd)] += 1
                for sid in script_ids(cmd):
                    label = f"{name}:{event}" + (f"[{mt}]" if mt else "")
                    declared[sid].append(label)
        for (mt, cmd), n in seen.items():
            if n > 1:
                found = True
                shown = f"matcher={mt}" if mt else "無 matcher"
                print(f"  ⚠️ {name} {event}（{shown}）：同一命令重複 {n} 次 → {cmd}")

# enabled plugin 也註冊 hook —— 與 settings 側交叉比對
plugin_declared = collections.defaultdict(list)
try:
    root_cfg = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    enabled = {k for k, v in (root_cfg.get("enabledPlugins") or {}).items() if v}
except (OSError, json.JSONDecodeError):
    enabled = set()
try:
    installed = json.loads(
        (claude_dir / "plugins" / "installed_plugins.json").read_text(encoding="utf-8")
    ).get("plugins") or {}
except (OSError, json.JSONDecodeError):
    installed = {}

for key in sorted(enabled):
    for entry in installed.get(key) or []:
        ip = entry.get("installPath")
        if not ip:
            continue
        pj = pathlib.Path(ip) / ".claude-plugin" / "plugin.json"
        try:
            spec = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for event, matchers in (spec.get("hooks") or {}).items():
            if not isinstance(matchers, list):
                continue
            for m in matchers:
                if not isinstance(m, dict):
                    continue
                for h in m.get("hooks", []):
                    if isinstance(h, dict):
                        for sid in script_ids(h.get("command")):
                            plugin_declared[sid].append(f"plugin {key}:{event}")

for sid in sorted(set(declared) & set(plugin_declared)):
    found = True
    srcs = ", ".join(declared[sid] + plugin_declared[sid])
    print(f"  ⚠️ {sid} 被手動設定與 plugin 各註冊一次 → {srcs}")
    print(f"     成本在每個 session／每個 prompt 各注入一份重複 context")

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
