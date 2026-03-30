#!/bin/bash
# 使用 venv 絕對路徑執行，繞過 GVM 等 shell hook 對 python 指令的攔截

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"

# 建立 venv（若不存在）
if [ ! -d "$VENV_DIR" ]; then
    echo "建立虛擬環境..."
    python3 -m venv "$VENV_DIR"
fi

# 同步相依套件
"$VENV_DIR/bin/pip" install -r "$REQUIREMENTS" --quiet 2>/dev/null

# 執行主程式
"$VENV_DIR/bin/python" "$SCRIPT_DIR/run.py"
