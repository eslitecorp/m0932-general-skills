
import configparser
import requests
import os
from datetime import datetime, timedelta

# 技能根目錄
SKILL_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(SKILL_DIR, 'config.ini')

def get_youtrack_issues(base_url, token, query):
    """
    向 YouTrack API 查詢議題
    """
    api_url = f"{base_url}/api/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    params = {
        "query": query,
        "fields": "idReadable,summary,customFields(name,value(name,minutes))"
    }
    try:
        response = requests.get(api_url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"API 請求失敗: {e}")
        if e.response:
            print(f"錯誤回應: {e.response.text}")
        return None

def format_duration(minutes):
    """
    將分鐘數格式化為易讀的字串 (e.g., 1d 2h 30m)
    """
    if not minutes:
        return "N/A"
    d = minutes // (60 * 8) # 假設一天工作 8 小時
    h = (minutes % (60 * 8)) // 60
    m = minutes % 60
    parts = []
    if d > 0:
        parts.append(f"{d}d")
    if h > 0:
        parts.append(f"{h}h")
    if m > 0:
        parts.append(f"{m}m")
    return " ".join(parts) if parts else "0m"

def get_custom_field_value(issue, field_name):
    """
    從議題中取得自訂欄位的值
    """
    for field in issue.get("customFields", []):
        if field.get("name") == field_name:
            value = field.get("value")
            if value:
                if "minutes" in value:
                    return value["minutes"]
                if "name" in value:
                    return value["name"]
            return None
    return None

def generate_report_section(title, issues, base_url):
    """
    產生報告的單一區塊
    """
    section = f"### {title}\n\n"
    section += "優先序 預估時間(實際時間) 事項名稱\n"

    if not issues:
        section += "(無)\n"
        return section

    for issue in issues:
        issue_id = issue.get("idReadable", "N/A")
        summary = issue.get("summary", "N/A")
        issue_url = f"{base_url}/issue/{issue_id}"

        priority = get_custom_field_value(issue, "Priority") or "N/A"
        estimation = format_duration(get_custom_field_value(issue, "Estimation"))
        time_spent = format_duration(get_custom_field_value(issue, "Time Spent"))

        time_parts = []
        if estimation != "N/A":
            time_parts.append(estimation)
        if time_spent != "N/A":
            time_parts.append(f"({time_spent})")
        time_str = "".join(time_parts)
        if not time_str:
            time_str = "N/A"

        section += f'{priority} {time_str} [{issue_id} {summary}]({issue_url})\n'
    
    return section

def main():
    """
    主程式
    """
    config = configparser.ConfigParser()
    try:
        if not os.path.exists(CONFIG_PATH):
            print(f"錯誤：找不到設定檔 {CONFIG_PATH}")
            print("請確認技能資料夾中包含 config.ini 檔案。")
            return
            
        config.read(CONFIG_PATH)
        base_url = config['youtrack']['url'].strip('\'"')
        token = config['youtrack']['token'].strip('\'"')
    except (KeyError, configparser.NoSectionError):
        print("錯誤：找不到或 config.ini 格式不正確。")
        print("請確認 config.ini 檔案存在且包含 [youtrack] 區塊與 url/token。")
        return

    if base_url == "https://your-youtrack-instance.youtrack.cloud" or token == "YOUR_YOUTRACK_API_TOKEN":
        print(f"錯誤：請先在 {CONFIG_PATH} 中設定您的 YouTrack 網址和 API Token。")
        return

    # 查詢上週完成事項
    completed_query = "for: me State: Done resolved date: {last week}"
    completed_issues = get_youtrack_issues(base_url, token, completed_query)

    # 查詢尚未完成事項
    pending_query = "for: me State: -Done -{Won\'t fix}"
    pending_issues = get_youtrack_issues(base_url, token, pending_query)

    # 產生報告
    today = datetime.now().strftime("%Y-%m-%d")
    report = f"### 週報 ({today})\n\n"
    
    if completed_issues is not None:
        report += generate_report_section("上週完成事項", completed_issues, base_url)
    
    if pending_issues is not None:
        report += "\n" + generate_report_section("尚未完成事項", pending_issues, base_url)

    print(report)

if __name__ == "__main__":
    main()
