#!/usr/bin/env bash
# 降載實驗的壓艙物 —— 把可用記憶體壓到目標上限，讓「不升規會怎樣」變成可實測的。
#
# 用法：
#   ./downclock_ballast.sh --ceiling 24 --confirm     # 把有效可用壓到約 24 GB
#   ./downclock_ballast.sh --ceiling 24               # 只試算，不配置（預設）
#
# ⚠️ **這支腳本會讓機器變慢，這是它的用途，不是副作用。** 跑之前先存檔。
#    Ctrl-C 或關掉終端機就立刻釋放；它不寫任何檔案、不改任何設定。
#
# ⛔ 誠實的限制，寫在最前面：
#    macOS 沒有 sudo 就**無法硬性限制**一個 process 以外的記憶體上限。
#    這支腳本用的是「熱壓艙物」——配置並持續觸碰 N GB，逼其餘負載擠進剩下的空間。
#    這是**近似**，不是 cgroup 那種硬上限：
#      - 壓艙物自己也會被壓縮／換出，所以實際壓下去的量小於配置量
#      - 系統可能選擇換出別的東西而不是縮小你的工作負載
#    因此實驗結果要讀成「**在這個近似條件下**成功率有沒有掉」，
#    ⛔ 不得宣稱「已把機器限制在 N GB」。報告要附本節這段話。

set -uo pipefail

CEILING_GB=""
CONFIRM=0
CHUNK_MB=256
TOUCH_INTERVAL=5

while [ $# -gt 0 ]; do
  case "$1" in
    --ceiling) CEILING_GB="${2:-}"; shift 2 ;;
    --confirm) CONFIRM=1; shift ;;
    --chunk-mb) CHUNK_MB="${2:-256}"; shift 2 ;;
    *) echo "[ERROR] 不認得的參數：$1" >&2; exit 2 ;;
  esac
done

if [ "$(uname -s)" != "Darwin" ]; then
  echo "[ERROR] 只支援 macOS（與 environment.md 同一範圍）。" >&2
  exit 2
fi
if ! [[ "$CEILING_GB" =~ ^[0-9]+$ ]]; then
  echo "[ERROR] 需要 --ceiling <GB>，例如 --ceiling 24" >&2
  exit 2
fi

RAM_BYTES=$(sysctl -n hw.memsize)
RAM_GB=$(( RAM_BYTES / 1024 / 1024 / 1024 ))

echo "=============================================================="
echo "降載實驗壓艙物 — 試算"
echo "=============================================================="
echo "  實體記憶體      ${RAM_GB} GB"
echo "  目標有效上限    ${CEILING_GB} GB"

if [ "$CEILING_GB" -ge "$RAM_GB" ]; then
  echo
  echo "[ERROR] 目標上限 ${CEILING_GB} GB 不小於實體 ${RAM_GB} GB —— 沒有東西要壓。" >&2
  echo "        降載實驗的意義是「壓到目標規格以下看會不會壞」，" >&2
  echo "        要模擬**更大**的機器請直接在那台機器上跑，⛔ 壓艙物做不到。" >&2
  exit 2
fi

BALLAST_GB=$(( RAM_GB - CEILING_GB ))
echo "  需要的壓艙物    ${BALLAST_GB} GB"

MIN_HEADROOM_GB=4
if [ "$CEILING_GB" -lt "$MIN_HEADROOM_GB" ]; then
  echo
  echo "[ERROR] 目標上限低於 ${MIN_HEADROOM_GB} GB，機器可能無法操作到足以跑完任務集。" >&2
  echo "        ⛔ 拒絕執行 —— 跑不完的實驗產不出「成功率下降」，只會產出「當掉」。" >&2
  exit 2
fi

if [ "$CONFIRM" -ne 1 ]; then
  echo
  echo "  這是試算。要真的配置請加 --confirm。"
  echo "  ⚠️ 加了之後機器會明顯變慢，先存檔。Ctrl-C 立即釋放。"
  echo "=============================================================="
  exit 0
fi

echo
echo "  ⚠️ 開始配置 ${BALLAST_GB} GB 壓艙物。Ctrl-C 釋放。"
echo "  每 ${TOUCH_INTERVAL} 秒重新觸碰一次，避免壓艙物自己被換出而失效。"
echo "=============================================================="

python3 - "$BALLAST_GB" "$CHUNK_MB" "$TOUCH_INTERVAL" <<'PY'
import sys, time, signal

ballast_gb, chunk_mb, interval = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
blocks = []
running = True

def stop(signum, frame):
    global running
    running = False

signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)

target = ballast_gb * 1024 // chunk_mb
print(f"  配置中：{target} × {chunk_mb} MB …", flush=True)
try:
    for i in range(target):
        if not running:
            break
        # bytearray 是真的實體配置；寫入才會 fault in，不寫的話只是虛擬位址
        b = bytearray(chunk_mb * 1024 * 1024)
        for off in range(0, len(b), 16384):      # 每頁碰一次（16 KB page）
            b[off] = 1
        blocks.append(b)
        if (i + 1) % 8 == 0:
            print(f"    已配置 {(i + 1) * chunk_mb / 1024:.1f} GB", flush=True)
except MemoryError:
    print(f"  ⚠️ 配置到 {len(blocks) * chunk_mb / 1024:.1f} GB 時記憶體不足，就停在這裡。"
          f"報告要寫實際配置量，⛔ 不要寫目標量。", flush=True)

held = len(blocks) * chunk_mb / 1024
print(f"\n  壓艙物就位：{held:.1f} GB（目標 {ballast_gb} GB）", flush=True)
print("  ⛔ 報告要寫這個實際值，並附腳本開頭的『這是近似不是硬上限』那段話。", flush=True)
print("  現在去跑任務集。跑完回來按 Ctrl-C 釋放。\n", flush=True)

while running:
    # 重新觸碰，避免壓艙物被壓縮／換出而失去效果
    for b in blocks:
        b[0] = (b[0] + 1) % 256
        b[len(b) // 2] = 1
    time.sleep(interval)

print("\n  釋放中 …", flush=True)
blocks.clear()
print("  已釋放。", flush=True)
PY

echo "=============================================================="
echo "壓艙物已釋放。未修改任何設定、未寫入任何檔案。"
echo "=============================================================="
