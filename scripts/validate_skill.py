#!/usr/bin/env python3
"""
validate_skill.py — 在 GitHub Actions 中驗證 PR 的 SKILL.md：
1. frontmatter schema（name / description / tags）
2. PR description checklist 全部勾選
3. 重工偵測（新 skill 的 name/tags 與現有 skill 比對）
4. tag 白名單檢查（不在清單中發 warning）
"""

import os
import re
import glob
import json
import jsonschema
from pathlib import Path

SCHEMA_PATH = ".github/skill-schema.json"
RESULT_FILE = "validation_result.txt"

VALID_TAGS = {
    "git", "api", "devops", "monitoring", "security",
    "testing", "report", "project-management", "documentation",
    "product", "communication", "data", "ai", "meta", "audit", "skill",
}

errors = []
warnings = []


def get_new_skill_files():
    import subprocess
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=A", "origin/main...HEAD"],
        capture_output=True, text=True
    )
    return [f for f in result.stdout.splitlines() if f.endswith("SKILL.md")]


def parse_frontmatter(filepath):
    content = Path(filepath).read_text(encoding="utf-8")
    match = re.match(r"^---\r?\n(.*?)\r?\n---", content, re.DOTALL)
    if not match:
        return None
    fm = {}
    for line in match.group(1).splitlines():
        m = re.match(r'^(\w[\w-]*):\s*(.+)', line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val.startswith('[') and val.endswith(']'):
                items = re.findall(r'"([^"]+)"|\'([^\']+)\'|([\w-]+)', val)
                val = [a or b or c for a, b, c in items]
            fm[key] = val
    return fm


def validate_schema(filepath, frontmatter):
    with open(SCHEMA_PATH) as f:
        schema = json.load(f)
    try:
        jsonschema.validate(frontmatter, schema)
    except jsonschema.ValidationError as e:
        errors.append(f"**{filepath}** frontmatter 錯誤：`{e.message}`")


def validate_tags(filepath, frontmatter):
    tags = frontmatter.get("tags", [])
    if not isinstance(tags, list):
        return
    unknown = [t for t in tags if t not in VALID_TAGS]
    if unknown:
        tag_list = ", ".join(f"`{t}`" for t in unknown)
        warnings.append(
            f"**{filepath}** 包含非標準 tag：{tag_list}。"
            f"建議從 [Tag 清單](https://github.com/eslitecorp/m0932-general-skills/wiki/Tag-%E6%B8%85%E5%96%AE) 中選擇。"
        )


def validate_checklist():
    pr_body = os.environ.get("PR_BODY", "")
    unchecked = re.findall(r"- \[ \]", pr_body)
    if unchecked:
        errors.append(
            f"PR checklist 有 **{len(unchecked)}** 項未勾選，請確認所有項目都打 `[x]`"
        )


def detect_duplicate(filepath, new_fm):
    new_name = (new_fm.get("name") or "").lower()
    new_tags = {t.lower() for t in (new_fm.get("tags") or []) if isinstance(new_fm.get("tags"), list)}

    for existing in glob.glob("**/SKILL.md", recursive=True):
        if existing == filepath:
            continue
        existing_fm = parse_frontmatter(existing)
        if not existing_fm:
            continue
        existing_name = (existing_fm.get("name") or "").lower()
        existing_tags = {t.lower() for t in (existing_fm.get("tags") or []) if isinstance(existing_fm.get("tags"), list)}

        name_match = new_name and new_name == existing_name
        tag_overlap = new_tags & existing_tags
        if name_match or len(tag_overlap) >= 2:
            warnings.append(
                f"**{filepath}** 與現有 `{existing}` 可能重複"
                + (f"（相同 name: `{new_name}`）" if name_match else "")
                + (f"（重疊 tags: {', '.join(sorted(tag_overlap))}）" if tag_overlap else "")
            )


def main():
    new_files = get_new_skill_files()

    validate_checklist()

    for filepath in new_files:
        fm = parse_frontmatter(filepath)
        if fm is None:
            errors.append(f"**{filepath}** 缺少有效的 YAML frontmatter（需要 `---` 包圍）")
            continue
        validate_schema(filepath, fm)
        validate_tags(filepath, fm)
        detect_duplicate(filepath, fm)

    lines = []
    if errors:
        lines.append("## ❌ Skill Lint 未通過
")
        for e in errors:
            lines.append(f"- {e}")
    if warnings:
        lines.append("
## ⚠️ 注意事項
")
        for w in warnings:
            lines.append(f"- {w}")
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
