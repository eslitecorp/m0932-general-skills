#!/usr/bin/env python3
"""
Datadog 監控自動設定腳本

用法:
  python3 setup_monitoring.py \
    --service athena-api \
    --endpoint "GET /api/v4/products" \
    --resource-filter "get_/api/v4/products*" \
    --p99-threshold 4 \
    --dashboard-id uh3-7r2-uzx

或使用 --auto-threshold 自動查詢過去 7 天 P99 並建議閾值:
  python3 setup_monitoring.py \
    --service athena-api \
    --endpoint "GET /api/v4/products" \
    --resource-filter "get_/api/v4/products*" \
    --auto-threshold \
    --dashboard-id uh3-7r2-uzx
"""

import argparse
import json
import math
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _load_dotenv():
    """自動載入腳本同目錄的 .env 檔案（不覆蓋已存在的環境變數）"""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

# ── 設定 ────────────────────────────────────────────
DD_SITE    = os.getenv("DD_SITE", "us5.datadoghq.com")
DD_API_KEY = os.getenv("DD_API_KEY", "")
DD_APP_KEY = os.getenv("DD_APP_KEY", "")
APM_METRIC = "trace.OpenTelemetry_Instrumentation_Rack.server"
# ────────────────────────────────────────────────────


def dd_request(method, path, body=None):
    url = f"https://api.{DD_SITE}/api/{path}"
    headers = {
        "Content-Type": "application/json",
        "DD-API-KEY": DD_API_KEY,
        "DD-APPLICATION-KEY": DD_APP_KEY,
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def query_p99_stats(service, resource_filter, days=7):
    """查詢過去 N 天的 P99 延遲統計"""
    now = int(datetime.now(timezone.utc).timestamp())
    frm = now - days * 86400
    q = urllib.parse.quote(
        f"p99:{APM_METRIC}{{service:{service},resource_name:{resource_filter}}}"
    )
    resp = dd_request("GET", f"v1/query?from={frm}&to={now}&query={q}")

    series = resp.get("series", [])
    if not series:
        return None

    points = [p[1] for p in series[0].get("pointlist", []) if p[1] is not None]
    if not points:
        return None

    sorted_pts = sorted(points)
    p95_idx = int(len(sorted_pts) * 0.95)
    return {
        "min": min(points),
        "max": max(points),
        "avg": sum(points) / len(points),
        "p95_of_p99": sorted_pts[p95_idx],
        "count": len(points),
    }


def suggest_threshold(stats):
    """根據 P99 統計自動建議閾值（取 p95 of p99，無條件進位到整數）"""
    raw = stats["p95_of_p99"]
    return math.ceil(raw) if raw >= 1 else round(raw + 0.1, 1)


# ── Monitors ────────────────────────────────────────

def create_error_rate_monitor(service, endpoint, resource_filter, threshold, priority=2):
    warning = round(threshold * 0.4, 3)
    body = {
        "name": f"[{service}] {endpoint} Error Rate > {threshold}%",
        "type": "metric alert",
        "query": (
            f"sum(last_5m):("
            f" sum:{APM_METRIC}.hits{{service:{service},resource_name:{resource_filter},error:true}}.as_rate()"
            f" / sum:{APM_METRIC}.hits{{service:{service},resource_name:{resource_filter}}}.as_rate()"
            f") * 100 > {threshold}"
        ),
        "message": (
            f"**{service}** `{endpoint}` 錯誤率超過 {threshold}%\n\n"
            f"- 服務: {service}\n- 端點: {endpoint}\n- 閾值: {threshold}%\n\n@slack-alerts"
        ),
        "tags": [f"service:{service}", "env:production", "team:backend"],
        "options": {
            "thresholds": {"critical": threshold, "warning": warning},
            "notify_no_data": False,
            "evaluation_delay": 60,
            "include_tags": True,
        },
        "priority": priority,
    }
    resp = dd_request("POST", "v1/monitor", body)
    return resp.get("id"), resp.get("errors")


def create_p99_latency_monitor(service, endpoint, resource_filter, threshold, priority=2):
    warning = round(threshold * 0.625, 3)
    body = {
        "name": f"[{service}] {endpoint} P99 Latency > {threshold}s",
        "type": "metric alert",
        "query": (
            f"avg(last_5m):p99:{APM_METRIC}"
            f"{{service:{service},resource_name:{resource_filter}}} > {threshold}"
        ),
        "message": (
            f"**{service}** `{endpoint}` P99 延遲超過 {threshold}s\n\n"
            f"- 服務: {service}\n- 端點: {endpoint}\n- 閾值: {threshold}s\n\n@slack-alerts"
        ),
        "tags": [f"service:{service}", "env:production", "team:backend"],
        "options": {
            "thresholds": {"critical": threshold, "warning": warning},
            "notify_no_data": False,
            "evaluation_delay": 60,
            "include_tags": True,
        },
        "priority": priority,
    }
    resp = dd_request("POST", "v1/monitor", body)
    return resp.get("id"), resp.get("errors")


# ── SLOs ────────────────────────────────────────────

def create_availability_slo(service, endpoint, error_monitor_id, target=99.5, window="30d"):
    body = {
        "name": f"[{service}] {endpoint} Availability SLO",
        "description": f"{service} {endpoint} 可用性目標 {target}%（{window} 滾動視窗）",
        "type": "monitor",
        "monitor_ids": [error_monitor_id],
        "thresholds": [
            {"timeframe": window, "target": target, "warning": min(target + 0.2, 100)}
        ],
        "tags": [f"service:{service}", "env:production", "team:backend"],
    }
    resp = dd_request("POST", "v1/slo", body)
    data = resp.get("data", [])
    if data:
        return data[0].get("id"), None
    return None, resp.get("errors")


def create_p99_latency_slo(service, endpoint, resource_filter, threshold, target=99.0, window="30d"):
    body = {
        "name": f"[{service}] {endpoint} P99 Latency SLO",
        "description": (
            f"{service} {endpoint} P99 延遲目標 < {threshold}s（{window} 滾動視窗）"
        ),
        "type": "time_slice",
        "slo_timeframe": window,
        "sli_specification": {
            "time_slice": {
                "query": {
                    "formulas": [{"formula": "query1"}],
                    "queries": [
                        {
                            "name": "query1",
                            "data_source": "metrics",
                            "query": (
                                f"p99:{APM_METRIC}"
                                f"{{service:{service},resource_name:{resource_filter}}}"
                            ),
                        }
                    ],
                },
                "comparator": "<",
                "threshold": threshold,
                "no_data_strategy": "COUNT_AS_UPTIME",
            }
        },
        "thresholds": [
            {"timeframe": window, "target": target, "warning": min(target + 0.5, 100)}
        ],
        "tags": [f"service:{service}", "env:production", "team:backend"],
    }
    resp = dd_request("POST", "v1/slo", body)
    data = resp.get("data", [])
    if data:
        return data[0].get("id"), None
    return None, resp.get("errors")


# ── Dashboard ────────────────────────────────────────

def add_dashboard_section(dashboard_id, service, endpoint, resource_filter,
                           avail_slo_id, latency_slo_id, p99_threshold):
    current = dd_request("GET", f"v1/dashboard/{dashboard_id}")
    if "errors" in current:
        return False, current["errors"]

    widgets = current.get("widgets", [])
    if widgets:
        last = max(
            w.get("layout", {}).get("y", 0) + w.get("layout", {}).get("height", 3)
            for w in widgets
        )
    else:
        last = 0

    new_widgets = [
        {
            "definition": {
                "type": "note",
                "content": f"## {endpoint}",
                "background_color": "blue",
                "font_size": "18",
                "text_align": "left",
                "show_tick": False,
            },
            "layout": {"x": 0, "y": last, "width": 12, "height": 1},
        },
        {
            "definition": {
                "type": "slo",
                "title": "Availability SLO (99.5% / 30d)",
                "title_size": "16",
                "title_align": "left",
                "slo_id": avail_slo_id,
                "time_windows": ["30d"],
                "show_error_budget": True,
                "view_type": "detail",
                "view_mode": "both",
            },
            "layout": {"x": 0, "y": last + 1, "width": 6, "height": 3},
        },
        {
            "definition": {
                "type": "slo",
                "title": f"P99 Latency SLO (99% / 30d)",
                "title_size": "16",
                "title_align": "left",
                "slo_id": latency_slo_id,
                "time_windows": ["30d"],
                "show_error_budget": True,
                "view_type": "detail",
                "view_mode": "both",
            },
            "layout": {"x": 6, "y": last + 1, "width": 6, "height": 3},
        },
        {
            "definition": {
                "type": "timeseries",
                "title": "Error Rate",
                "show_legend": True,
                "requests": [
                    {
                        "q": (
                            f"sum:{APM_METRIC}.hits"
                            f"{{service:{service},resource_name:{resource_filter},error:true}}.as_rate()"
                        ),
                        "display_type": "bars",
                        "style": {"palette": "warm"},
                    }
                ],
            },
            "layout": {"x": 0, "y": last + 4, "width": 6, "height": 3},
        },
        {
            "definition": {
                "type": "timeseries",
                "title": "P99 Latency (s)",
                "show_legend": True,
                "requests": [
                    {
                        "q": (
                            f"p99:{APM_METRIC}"
                            f"{{service:{service},resource_name:{resource_filter}}}"
                        ),
                        "display_type": "line",
                        "style": {"palette": "dog_classic"},
                    }
                ],
                "markers": [
                    {
                        "value": f"y = {p99_threshold}",
                        "display_type": "error dashed",
                        "label": f"P99 SLO {p99_threshold}s",
                    }
                ],
            },
            "layout": {"x": 6, "y": last + 4, "width": 6, "height": 3},
        },
    ]

    resp = dd_request(
        "PUT",
        f"v1/dashboard/{dashboard_id}",
        {
            "title": current["title"],
            "layout_type": current["layout_type"],
            "description": current.get("description", ""),
            "widgets": widgets + new_widgets,
        },
    )
    return "id" in resp, resp.get("errors")


# ── Main ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Datadog 監控自動設定腳本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--service",         required=True, help="服務名稱，對應 APM service tag (e.g., athena-api)")
    parser.add_argument("--endpoint",        required=True, help='端點描述 (e.g., "GET /api/v4/products")')
    parser.add_argument("--resource-filter", required=True, help='APM resource_name tag 值，支援 * 萬用字元 (e.g., "get_/api/v4/products*")')
    parser.add_argument("--p99-threshold",   type=float,    help="P99 延遲警報閾值（秒），與 --auto-threshold 擇一使用")
    parser.add_argument("--auto-threshold",  action="store_true", help="自動查詢過去 7 天 P99 並計算建議閾值")
    parser.add_argument("--error-threshold", type=float, default=0.5, help="錯誤率警報閾值（%%，預設 0.5）")
    parser.add_argument("--dashboard-id",    help="要新增 section 的 Dashboard ID（可選）")
    parser.add_argument("--priority",        type=int, default=2, help="Monitor priority 1~5（預設 2）")
    args = parser.parse_args()

    # Validate
    if not DD_API_KEY or not DD_APP_KEY:
        print("❌ 請先設定環境變數 DD_API_KEY 和 DD_APP_KEY")
        print("   export DD_API_KEY=xxx")
        print("   export DD_APP_KEY=xxx")
        sys.exit(1)

    if not args.p99_threshold and not args.auto_threshold:
        print("❌ 請指定 --p99-threshold 或 --auto-threshold")
        sys.exit(1)

    service         = args.service
    endpoint        = args.endpoint
    resource_filter = args.resource_filter
    error_threshold = args.error_threshold

    # Auto threshold
    if args.auto_threshold:
        print(f"🔍 查詢過去 7 天 P99 數據...")
        stats = query_p99_stats(service, resource_filter)
        if not stats:
            print("   ⚠️  查無數據，請手動指定 --p99-threshold")
            sys.exit(1)
        p99_threshold = suggest_threshold(stats)
        print(f"   min={stats['min']:.2f}s  avg={stats['avg']:.2f}s  max={stats['max']:.2f}s")
        print(f"   建議閾值: {p99_threshold}s（p95 of p99 無條件進位）")
    else:
        p99_threshold = args.p99_threshold

    print(f"\n🔧 開始設定監控: [{service}] {endpoint}")
    print(f"   P99 閾值: {p99_threshold}s | 錯誤率閾值: {error_threshold}%")
    print()

    results = {}

    # Step 1: Monitors
    print("📊 Step 1 / 3  建立 Monitors...")

    err_monitor_id, err = create_error_rate_monitor(
        service, endpoint, resource_filter, error_threshold, args.priority
    )
    if err_monitor_id:
        print(f"  ✅ Error Rate Monitor  (id: {err_monitor_id})")
        results["monitor_error_rate"] = err_monitor_id
    else:
        print(f"  ❌ Error Rate Monitor 失敗: {err}")
        sys.exit(1)

    lat_monitor_id, err = create_p99_latency_monitor(
        service, endpoint, resource_filter, p99_threshold, args.priority
    )
    if lat_monitor_id:
        print(f"  ✅ P99 Latency Monitor (id: {lat_monitor_id})")
        results["monitor_p99_latency"] = lat_monitor_id
    else:
        print(f"  ❌ P99 Latency Monitor 失敗: {err}")
        sys.exit(1)

    # Step 2: SLOs
    print("\n🎯 Step 2 / 3  建立 SLOs...")

    avail_slo_id, err = create_availability_slo(service, endpoint, err_monitor_id)
    if avail_slo_id:
        print(f"  ✅ Availability SLO    (id: {avail_slo_id})")
        results["slo_availability"] = avail_slo_id
    else:
        print(f"  ❌ Availability SLO 失敗: {err}")

    latency_slo_id, err = create_p99_latency_slo(
        service, endpoint, resource_filter, p99_threshold
    )
    if latency_slo_id:
        print(f"  ✅ P99 Latency SLO     (id: {latency_slo_id})")
        results["slo_p99_latency"] = latency_slo_id
    else:
        print(f"  ❌ P99 Latency SLO 失敗: {err}")

    # Step 3: Dashboard
    if args.dashboard_id and avail_slo_id and latency_slo_id:
        print(f"\n📈 Step 3 / 3  更新 Dashboard ({args.dashboard_id})...")
        ok, err = add_dashboard_section(
            args.dashboard_id, service, endpoint, resource_filter,
            avail_slo_id, latency_slo_id, p99_threshold,
        )
        if ok:
            print(f"  ✅ Dashboard section 新增完成")
            results["dashboard"] = args.dashboard_id
        else:
            print(f"  ❌ Dashboard 更新失敗: {err}")
    elif not args.dashboard_id:
        print("\n📈 Step 3 / 3  跳過 Dashboard（未指定 --dashboard-id）")

    # Summary
    print("\n" + "=" * 50)
    print("✅ 完成！建立項目摘要：")
    print(f"   Monitor (Error Rate) : {results.get('monitor_error_rate')}")
    print(f"   Monitor (P99 Latency): {results.get('monitor_p99_latency')}")
    print(f"   SLO (Availability)   : {results.get('slo_availability')}")
    print(f"   SLO (P99 Latency)    : {results.get('slo_p99_latency')}")
    if "dashboard" in results:
        print(f"   Dashboard            : https://{DD_SITE}/dashboard/{results['dashboard']}")
    print()


if __name__ == "__main__":
    main()
