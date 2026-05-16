#!/usr/bin/env python3
"""
validate_skill.py — 在 GitHub Actions 中驗證 PR 的 SKILL.md：
1. frontmatter schema（name / description / tags）
2. PR description checklist 全部勾選
3. 重工偵測（新 skill 的 name/tags 與現有 skill 比對）
"""

import os
import re
import glob
import json
import yaml
import jsonschema
from pathlib import Path

SCHEMA_PATH = ".github/skill-schema.json"
RESULT_FILE = "validation_result.txt"

errors = []
warnings = []


# ── 1. 找出本次 PR 新增的 SKILL.md ──────────────────────────────────────────
def get_new_skill_files():
    """從 git diff 找出新增的 SKILL.md 路徑"""
    import subprocess
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=A", "origin/main...HEAD"],
        capture_output=True, text=True
    )
    return [f for f in result.stdout.splitlines() if f.endswith("SKILL.md")]


# ── 2. 解析 frontmatter ──────────────────────────────────────────────────────
def parse_frontmatter(filepath):
    content = Path(filepath).read_text(encoding="utf-8")
    match = re.match(r"^---
(.*?)
---", content, re.DOTALL)
    if not match:
        return None
    try:
        return yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None


# ── 3. 驗證 schema ───────────────────────────────────────────────────────────
def validate_schema(filepath, frontmatter):
    with open(SCHEMA_PATH) as f:
        schema = json.load(f)
    try:
        jsonschema.validate(frontmatter, schema)
    except jsonschema.ValidationError as e:
        errors.append(f"**{filepath}** frontmatter 錯誤：`{e.message}`")


# ── 4. PR description checklist 驗證 ────────────────────────────────────────
def validate_checklist():
    pr_body = os.environ.get("PR_BODY", "")
    unchecked = re.findall(r"- \[ \]", pr_body)
    if unchecked:
        errors.append(
            f"PR checklist 有 **{len(unchecked)}** 項未勾選，請確認所有項目都打 `[x]`"
        )


# ── 5. 重工偵測 ──────────────────────────────────────────────────────────────
def detect_duplicate(filepath, new_fm):
    new_name = (new_fm.get("name") or "").lower()
    new_tags = {t.lower() for t in (new_fm.get("tags") or [])}

    for existing in glob.glob("*/SKILL.md"):
        if existing == filepath:
            continue
        existing_fm = parse_frontmatter(existing)
        if not existing_fm:
            continue
        existing_name = (existing_fm.get("name") or "").lower()
        existing_tags = {t.lower() for t in (existing_fm.get("tags") or [])}

        name_match = new_name and new_name == existing_name
        tag_overlap = new_tags & existing_tags
        if name_match or len(tag_overlap) >= 2:
            warnings.append(
                f"**{filepath}** 與現有 `{existing}` 可能重複"
                + (f"（相同 name: `{new_name}`）" if name_match else "")
                + (f"（重疊 tags: {', '.join(sorted(tag_overlap))}）" if tag_overlap else "")
            )


# ── 主流程 ───────────────────────────────────────────────────────────────────
def main():
    new_files = get_new_skill_files()

    validate_checklist()

    for filepath in new_files:
        fm = parse_frontmatter(filepath)
        if fm is None:
            errors.append(f"**{filepath}** 缺少有效的 YAML frontmatter（需要 `---` 包圍）")
            continue
        validate_schema(filepath, fm)
        detect_duplicate(filepath, fm)

    # ── 產出 comment 內容 ───────────────────────────────────────────────────
    lines = []
    if errors:
        lines.append("## ❌ Skill Lint 未通過
")
        for e in errors:
            lines.append(f"- {e}")
    if warnings:
        lines.append("
## ⚠️ 重工偵測警告
")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("
> 請確認與現有 skill 無重複後在 PR description 補充說明。")
    if not errors and not warnings and new_files:
        lines.append("## ✅ Skill Lint 通過
")
        lines.append(f"驗證了 {len(new_files)} 個新 SKILL.md，無問題。")

    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        f.write("
".join(lines))

    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
