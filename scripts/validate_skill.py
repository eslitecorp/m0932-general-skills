#!/usr/bin/env python3
"""
validate_skill.py — 在 GitHub Actions 中驗證 PR 的 SKILL.md：
1. frontmatter schema（name / description / tags）
2. 重工偵測（新 skill 的 name/tags 與現有 skill 比對）
3. tag 白名單檢查（不在清單中發 warning）
4. 內容安全掃描（prompt injection、危險 bash 指令）

確認清單勾選由 PR reviewer 人工確認，不在自動驗證範圍內。
"""

import re
import glob
import json
import subprocess
import jsonschema
from pathlib import Path

SCHEMA_PATH = ".github/skill-schema.json"
RESULT_FILE = "validation_result.txt"

VALID_TAGS = {
    "git", "api", "devops", "monitoring", "security",
    "testing", "report", "project-management", "documentation",
    "product", "communication", "data", "ai", "meta", "audit", "skill",
}

# Prompt injection patterns — error (hard fail)
# Ref: OWASP LLM01:2025, Lakera Prompt Injection Guide
PROMPT_INJECTION_PATTERNS = [
    (r'(?i)ignore\s+(previous|prior|all|your)\s+instructions?',
     '疑似 prompt injection：ignore instructions'),
    (r'(?i)disregard\s+(previous|prior|all|your|the)\s+(instructions?|guidelines?|rules?)',
     '疑似 prompt injection：disregard instructions'),
    (r'(?i)override\s+(your|the|all)\s+(instructions?|guidelines?|rules?|constraints?|directives?)',
     '疑似 prompt injection：override instructions'),
    (r'(?i)forget\s+(your|the|all|previous|prior)\s+(instructions?|guidelines?|rules?)',
     '疑似 prompt injection：forget instructions'),
    (r'(?i)you\s+are\s+now\s+(?:a\s+)?(?:different|new|another)\s+(?:ai|model|assistant|bot)',
     '疑似角色覆蓋：you are now a different AI'),
    (r'(?i)act\s+as\s+(?:an?\s+)?(?:unrestricted|jailbreak|evil|malicious|unfiltered)',
     '疑似 jailbreak：act as unrestricted'),
    (r'(?i)###\s*SYSTEM(?:\s+PROMPT)?',
     '疑似 system prompt 注入標記：### SYSTEM'),
    (r'忽略(?:之前|前面|上方|所有)的?指(?:令|示)',
     '疑似 prompt injection（中文）：忽略指令'),
    (r'(?:請|你)?(?:現在)?成為(?:一個?)?(?:不受限|惡意|越獄)',
     '疑似角色覆蓋（中文）'),
]

# Dangerous bash patterns — warning
# Ref: CWE-78 OS Command Injection, CWE-88 Argument Injection
DANGEROUS_BASH_PATTERNS = [
    (r'rm\s+-[rf]+\s+[/~]',
     '`rm -rf /` 或 `rm -rf ~`（刪除根目錄／家目錄）'),
    (r'(?:curl|wget)\s+[^\n|]*\|\s*(?:bash|sh|zsh|fish)(?:\s|$)',
     'curl/wget pipe to shell（供應鏈風險）'),
    (r'(?<!\w)\|\s*bash(?:\s|;|$|\n)',
     'pipe to bash（動態執行風險）'),
    (r'git\s+push\s+[^\n]*--force(?!-with-lease)',
     'git push --force（建議使用 --force-with-lease）'),
    (r'chmod\s+[0-9]*777',
     'chmod 777（過寬的權限設定）'),
    (r'eval\s+["\'\`]',
     'eval 動態執行字串'),
]

errors = []
warnings = []


def get_new_skill_files():
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=A", "origin/main...HEAD"],
        capture_output=True, text=True, check=True
    )
    return [f for f in result.stdout.splitlines() if f.endswith("SKILL.md")]


def get_changed_skill_files():
    """Return Added + Modified SKILL.md files for security scanning."""
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AM", "origin/main...HEAD"],
        capture_output=True, text=True, check=True
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


def scan_content_security(filepath):
    """Scan SKILL.md content for prompt injection and dangerous bash patterns.

    Prompt injection patterns → error (hard fail).
    Dangerous bash patterns   → warning.

    Ref: OWASP LLM01:2025, CWE-78, Lakera Prompt Injection Guide.
    """
    content = Path(filepath).read_text(encoding="utf-8")

    for pattern, desc in PROMPT_INJECTION_PATTERNS:
        if re.search(pattern, content):
            errors.append(
                f"**{filepath}** 🔴 安全掃描：{desc}（OWASP LLM01 Prompt Injection）"
            )

    # Strip frontmatter before scanning for bash patterns
    fm_match = re.match(r'^---\r?\n.*?\r?\n---\r?\n?', content, re.DOTALL)
    body = content[fm_match.end():] if fm_match else content

    for pattern, desc in DANGEROUS_BASH_PATTERNS:
        if re.search(pattern, body):
            warnings.append(
                f"**{filepath}** ⚠️ 危險指令：{desc}（CWE-78）"
            )


def check_dependabot_coverage():
    """Warn when a new requirements.txt has no matching entry in dependabot.yml.

    Dependabot.yml must be manually updated when a new skill adds Python deps;
    this check prevents that from being silently missed.
    """
    dependabot_path = Path(".github/dependabot.yml")
    if not dependabot_path.exists():
        warnings.append(
            "**.github/dependabot.yml** 不存在，Python 依賴 CVE 將無法自動偵測。"
        )
        return

    dependabot_text = dependabot_path.read_text(encoding="utf-8")

    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=A", "origin/main...HEAD"],
        capture_output=True, text=True, check=True
    )
    new_req_files = [f for f in result.stdout.splitlines() if f.endswith("requirements.txt")]

    for req_file in new_req_files:
        skill_dir = "/" + Path(req_file).parent.as_posix()
        if skill_dir not in dependabot_text:
            warnings.append(
                f"**{req_file}** 新增了 Python 依賴，但 `.github/dependabot.yml` "
                f"尚未加入 `directory: \"{skill_dir}\"` 條目。"
                f"請更新 dependabot.yml，否則 Dependabot 不會掃描此 skill 的 CVE。"
            )


def main():
    new_files = get_new_skill_files()
    changed_files = get_changed_skill_files()

    # Schema / tag / duplicate checks: Added files only
    for filepath in new_files:
        fm = parse_frontmatter(filepath)
        if fm is None:
            errors.append(f"**{filepath}** 缺少有效的 YAML frontmatter（需要 `---` 包圍）")
            continue
        validate_schema(filepath, fm)
        validate_tags(filepath, fm)
        detect_duplicate(filepath, fm)

    # Content security scan: Added + Modified files
    for filepath in changed_files:
        if not Path(filepath).exists():
            continue
        scan_content_security(filepath)

    # Dependabot coverage check: new requirements.txt without dependabot entry
    check_dependabot_coverage()

    lines = []
    if errors:
        lines.append("## ❌ Skill Lint 未通過\n")
        for e in errors:
            lines.append(f"- {e}")
    if warnings:
        lines.append("\n## ⚠️ 注意事項\n")
        for w in warnings:
            lines.append(f"- {w}")

    scanned = set(new_files) | set(changed_files)
    if not errors and not warnings and scanned:
        new_count = len(new_files)
        mod_count = len(set(changed_files) - set(new_files))
        lines.append("## ✅ Skill Lint 通過\n")
        lines.append(f"驗證了 {new_count} 個新 SKILL.md、{mod_count} 個已修改 SKILL.md，無問題。")

    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
