import requests
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

MINUTES_PER_WORK_DAY = 60 * 8
FIELD_PRIORITY = "Priority"
FIELD_ESTIMATION = "Estimation"
FIELD_TIME_SPENT = "Time Spent"

def get_youtrack_issues(base_url, token, query):
    try:
        response = requests.get(
            f"{base_url}/api/issues",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"query": query, "fields": "idReadable,summary,customFields(name,value(name,minutes))"},
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"API 請求失敗: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"錯誤回應: {e.response.text}")
        return None

def format_duration(minutes):
    if not minutes:
        return "N/A"
    d = minutes // MINUTES_PER_WORK_DAY
    h = (minutes % MINUTES_PER_WORK_DAY) // 60
    m = minutes % 60
    parts = [s for s in [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else ""] if s]
    return " ".join(parts) or "0m"

def get_custom_fields(issue):
    result = {}
    for field in issue.get("customFields", []):
        value = field.get("value")
        if isinstance(value, dict):
            if "minutes" in value:
                result[field["name"]] = value["minutes"]
            elif "name" in value:
                result[field["name"]] = value["name"]
    return result

def generate_report_section(title, issues, base_url):
    lines = [f"### {title}\n", "優先序 預估時間(實際時間) 事項名稱"]

    if not issues:
        lines.append("(無)")
        return "\n".join(lines) + "\n"

    for issue in issues:
        issue_id = issue.get("idReadable", "N/A")
        summary = issue.get("summary", "N/A")
        fields = get_custom_fields(issue)

        priority = fields.get(FIELD_PRIORITY, "N/A")
        estimation = format_duration(fields.get(FIELD_ESTIMATION))
        time_spent = format_duration(fields.get(FIELD_TIME_SPENT))

        parts = ([estimation] if estimation != "N/A" else []) + ([f"({time_spent})"] if time_spent != "N/A" else [])
        time_str = "".join(parts) or "N/A"

        lines.append(f'{priority} {time_str} [{issue_id} {summary}]({base_url}/issue/{issue_id})')

    return "\n".join(lines) + "\n"

def main():
    base_url = os.getenv("YOUTRACK_URL")
    token = os.getenv("YOUTRACK_TOKEN")

    if not base_url or not token:
        print("錯誤：請先在 .env 中設定 YOUTRACK_URL 與 YOUTRACK_TOKEN。")
        return

    completed_issues = get_youtrack_issues(base_url, token, "for: me State: Done resolved date: {last week}")
    pending_issues = get_youtrack_issues(base_url, token, "for: me State: -Done -{Won't fix}")

    today = datetime.now().strftime("%Y-%m-%d")
    report = f"### 週報 ({today})\n\n"
    if completed_issues is not None:
        report += generate_report_section("上週完成事項", completed_issues, base_url)
    if pending_issues is not None:
        report += "\n" + generate_report_section("尚未完成事項", pending_issues, base_url)

    print(report)

if __name__ == "__main__":
    main()
