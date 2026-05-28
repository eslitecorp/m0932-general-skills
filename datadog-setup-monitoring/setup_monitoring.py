#!/usr/bin/env python3
"""
Datadog 監控自動設定腳本

用法:
  python3 setup_monitoring.py \
    --service my-api \
    --endpoint "GET /api/v1/orders" \
    --resource-filter "get_/api/v1/orders*" \
    --p99-threshold 4 \
    --dashboard-id <dashboard_id>

或使用 --auto-threshold 自動查詢過去 7 天 P99 並建議閾值:
  python3 setup_monitoring.py \
    --service my-api \
    --endpoint "GET /api/v1/orders" \
    --resource-filter "get_/api/v1/orders*" \
    --auto-threshold \
    --dashboard-id <dashboard_id>
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
from datetime import datetime, timezone

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
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


# ── Auto Threshold ───────────────────────────────────

# 單位換算：days / weeks / months → 秒數
UNIT_TO_DAYS = {"days": 1, "weeks": 7, "months": 30}


def resolve_days(value, unit="days"):
    """將 (value, unit) 轉換為天數"""
    multiplier = UNIT_TO_DAYS.get(unit, 1)
    return int(value) * multiplier


def query_p99_stats(service, resource_filter, days=7):
    """查詢過去 N 天的 P95 延遲統計（用於計算 P99 SLO threshold）

    使用 p95: metric 查詢，取這些 p95 數據點的 p95（即 p95 of p95），
    無條件進位到整數秒作為 P99 Latency SLO threshold。
    """
    now = int(datetime.now(timezone.utc).timestamp())
    frm = now - days * 86400
    q = urllib.parse.quote(
        f"p95:{APM_METRIC}{{service:{service},resource_name:{resource_filter}}}"
    )
    resp = dd_request("GET", f"v1/query?from={frm}&to={now}&query={q}")
    series = resp.get("series", [])
    if not series:
        return None
    points = [p[1] for p in series[0].get("pointlist", []) if p[1] is not None]
    if not points:
        return None
    sorted_pts = sorted(points)
    p95_idx = min(len(sorted_pts) - 1, int(len(sorted_pts) * 0.95))
    return {
        "min": min(points),
        "max": max(points),
        "avg": sum(points) / len(points),
        "p95_of_p95": sorted_pts[p95_idx],
        "count": len(points),
    }


def query_request_count_stats(service, resource_filter, days=7):
    """查詢過去 N 天的 request count 統計（用於計算 p99 警示線）

    注意：Datadog metrics API 回傳的 as_count() 每個點是 rollup 區間的累計值，
    例如查詢 7 天時 interval=3600s（1小時），每點 = 該小時的總 count。
    需除以 interval_min 才能換算成 req/min。
    """
    now = int(datetime.now(timezone.utc).timestamp())
    frm = now - days * 86400
    q = urllib.parse.quote(
        f"sum:{APM_METRIC}.hits{{service:{service},resource_name:{resource_filter}}}.as_count()"
    )
    resp = dd_request("GET", f"v1/query?from={frm}&to={now}&query={q}")
    series = resp.get("series", [])
    if not series:
        return None
    pointlist = [(p[0], p[1]) for p in series[0].get("pointlist", []) if p[1] is not None and p[1] > 0]
    if len(pointlist) < 2:
        return None

    # 計算 rollup interval（毫秒 → 分鐘）
    pointlist.sort(key=lambda x: x[0])
    interval_ms = pointlist[-1][0] - pointlist[-2][0]
    interval_min = interval_ms / 1000 / 60  # ms → min

    # 換算每個點為 req/min
    points_per_min = [v / interval_min for _, v in pointlist]

    sorted_pts = sorted(points_per_min)
    n = len(sorted_pts)
    p99_idx = min(n - 1, int(n * 0.99))
    p5_idx = max(0, int(n * 0.05))
    return {
        "min": min(points_per_min),
        "max": max(points_per_min),
        "avg": sum(points_per_min) / n,
        "p99": sorted_pts[p99_idx],   # req/min，用於 Sum(Requests) 警示線（高峰）
        "p5": sorted_pts[p5_idx],     # req/min，用於 Request Rate SLO 閾值（低谷）
        "count": n,
        "interval_min": interval_min,
    }


def suggest_p99_threshold(stats):
    """根據 P95 統計自動建議閾值（取 p95 of p95，無條件進位到整數）"""
    raw = stats["p95_of_p95"]
    return math.ceil(raw) if raw >= 1 else round(raw + 0.1, 1)


# ── Monitors ────────────────────────────────────────

def create_error_rate_monitor(service, endpoint, resource_filter, threshold, priority=2):
    warning = round(threshold * 0.4, 3)
    body = {
        "name": f"[{service}] {endpoint} Error Rate > {threshold}%",
        "type": "metric alert",
        "query": (
            f"sum(last_5m):("
            f" sum:{APM_METRIC}.hits{{service:{service},resource_name:{resource_filter},!http.status_code:200}}.as_rate()"
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


def create_request_count_monitor(service, endpoint, resource_filter, threshold_count, priority=2):
    """建立 Sum(Requests) 超過 p99 高峰值的 Monitor（流量異常偏高告警）"""
    warning = int(threshold_count * 0.8)
    body = {
        "name": f"[{service}] {endpoint} Request Count > p99 ({threshold_count}/min)",
        "type": "metric alert",
        "query": (
            f"sum(last_5m):sum:{APM_METRIC}.hits"
            f"{{service:{service},resource_name:{resource_filter}}}.as_count() > {threshold_count}"
        ),
        "message": (
            f"**{service}** `{endpoint}` 請求量超過 p99 高峰值 {threshold_count}/min\n\n"
            f"- 服務: {service}\n- 端點: {endpoint}\n- 閾值: {threshold_count} req/min\n"
            f"- 可能原因：流量異常爆量、DDoS、或程式錯誤導致重複請求\n\n@slack-alerts"
        ),
        "tags": [f"service:{service}", "env:production", "team:backend"],
        "options": {
            "thresholds": {"critical": threshold_count, "warning": warning},
            "notify_no_data": False,
            "evaluation_delay": 60,
            "include_tags": True,
        },
        "priority": priority,
    }
    resp = dd_request("POST", "v1/monitor", body)
    return resp.get("id"), resp.get("errors")


def create_error_rate_anomaly_monitor(service, endpoint, resource_filter, priority=3):
    """建立 Error Rate Anomaly Monitor（基於歷史數據自動偵測異常）"""
    body = {
        "name": f"[{service}] {endpoint} Error Rate Anomaly",
        "type": "metric alert",
        "query": (
            f"avg(last_30m):anomalies("
            f"sum:{APM_METRIC}.hits{{service:{service},resource_name:{resource_filter},!http.status_code:200}}.as_rate()"
            f" / sum:{APM_METRIC}.hits{{service:{service},resource_name:{resource_filter}}}.as_rate()"
            f" * 100, 'agile', 2, direction='above', alert_window='last_30m', interval=60, count_default_zero='true'"
            f") >= 1"
        ),
        "message": (
            f"**{service}** `{endpoint}` Error Rate 異常（超出歷史正常範圍）\n\n"
            f"- 服務: {service}\n- 端點: {endpoint}\n\n@slack-alerts"
        ),
        "tags": [f"service:{service}", "env:production", "team:backend", "monitor_type:anomaly"],
        "options": {
            "thresholds": {"critical": 1, "warning": 0.5},
            "notify_no_data": False,
            "evaluation_delay": 60,
            "include_tags": True,
            "renotify_interval": 60,
        },
        "priority": priority,
    }
    resp = dd_request("POST", "v1/monitor", body)
    return resp.get("id"), resp.get("errors")


def create_p99_latency_anomaly_monitor(service, endpoint, resource_filter, priority=3):
    """建立 P99 Latency Anomaly Monitor（robust 演算法，對突發異常更穩健）"""
    body = {
        "name": f"[{service}] {endpoint} P99 Latency Anomaly",
        "type": "metric alert",
        "query": (
            f"avg(last_30m):anomalies("
            f"p99:{APM_METRIC}{{service:{service},resource_name:{resource_filter}}}"
            f", 'robust', 2, direction='above', alert_window='last_30m', interval=60, count_default_zero='true'"
            f") >= 1"
        ),
        "message": (
            f"**{service}** `{endpoint}` P99 延遲異常（超出歷史正常範圍）\n\n"
            f"- 服務: {service}\n- 端點: {endpoint}\n\n@slack-alerts"
        ),
        "tags": [f"service:{service}", "env:production", "team:backend", "monitor_type:anomaly"],
        "options": {
            "thresholds": {"critical": 1, "warning": 0.5},
            "notify_no_data": False,
            "evaluation_delay": 60,
            "include_tags": True,
            "renotify_interval": 60,
        },
        "priority": priority,
    }
    resp = dd_request("POST", "v1/monitor", body)
    return resp.get("id"), resp.get("errors")


def create_request_rate_anomaly_monitor(service, endpoint, resource_filter, priority=3):
    """建立 Request Rate Anomaly Monitor（agile 演算法，偵測流量異常高低）"""
    body = {
        "name": f"[{service}] {endpoint} Request Rate Anomaly",
        "type": "metric alert",
        "query": (
            f"avg(last_30m):anomalies("
            f"sum:{APM_METRIC}.hits{{service:{service},resource_name:{resource_filter}}}.as_rate()"
            f", 'agile', 2, direction='both', alert_window='last_30m', interval=60, count_default_zero='true'"
            f") >= 1"
        ),
        "message": (
            f"**{service}** `{endpoint}` 請求量異常（超出歷史正常範圍）\n\n"
            f"- 服務: {service}\n- 端點: {endpoint}\n\n@slack-alerts"
        ),
        "tags": [f"service:{service}", "env:production", "team:backend", "monitor_type:anomaly"],
        "options": {
            "thresholds": {"critical": 1, "warning": 0.5},
            "notify_no_data": False,
            "evaluation_delay": 60,
            "include_tags": True,
            "renotify_interval": 60,
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
        "description": f"{service} {endpoint} P99 延遲目標 < {threshold}s（{window} 滾動視窗）",
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


def create_request_rate_slo(service, endpoint, resource_filter, threshold_rps, target=99.0, window="30d"):
    """建立 Request Rate SLO（time_slice：rate >= threshold_rps）"""
    body = {
        "name": f"[{service}] {endpoint} Request Rate SLO",
        "description": (
            f"{service} {endpoint} Request Rate 目標 >= {threshold_rps} req/s（{window} 滾動視窗）"
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
                                f"sum:{APM_METRIC}.hits"
                                f"{{service:{service},resource_name:{resource_filter}}}.as_rate()"
                            ),
                        }
                    ],
                },
                "comparator": ">=",
                "threshold": threshold_rps,
                "no_data_strategy": "COUNT_AS_DOWNTIME",
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
    return None, resp.get("errors", resp.get("error"))


# ── Dashboard ────────────────────────────────────────

def add_dashboard_section(dashboard_id, service, endpoint, resource_filter,
                           avail_slo_id, latency_slo_id,
                           p99_threshold):
    """
    新增 Dashboard section，每個 section 包含 6 個 widget：

      行 y+0: [note 標題 w=12]
      行 y+1: [Availability SLO w=3][P99 Latency SLO w=3][P99 Latency 圖 w=6]
      行 y+4: [Error Rate w=6][Sum(Requests) w=6]

    每個 section 高度 = 7 行

    Widget 特性：
    - P99 Latency：原始 p99 line + anomaly band overlay（robust 演算法）
    - Error Rate：bars（非200/total×100）+ anomaly line overlay（agile 演算法）
    - Sum(Requests)：as_count line + anomaly line overlay（agile 演算法）
    - 所有 timeseries 均有 Monitor event overlay + Change Tracking overlay
    """
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

    # Event overlay：Monitor 觸發事件 + Change Tracking 部署事件
    EVENT_OVERLAY = [
        {"q": "sources:monitor status:error,warning", "tags_execution": "and"},
        {"q": f"sources:change_tracking service:{service} env:prod", "tags_execution": "and"},
    ]

    # $env template variable filter（對齊 Dashboard template variable）
    env_filter = f"service:{service},$env,resource_name:{resource_filter}"
    env_filter_errors = f"service:{service},$env,resource_name:{resource_filter},!http.status_code:200"

    new_widgets = [
        # 1. 標題
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
        # 2. Availability SLO（左 1/4）
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
            "layout": {"x": 0, "y": last + 1, "width": 3, "height": 3},
        },
        # 3. P99 Latency SLO（中 1/4）
        {
            "definition": {
                "type": "slo",
                "title": "P99 Latency SLO (99% / 30d)",
                "title_size": "16",
                "title_align": "left",
                "slo_id": latency_slo_id,
                "time_windows": ["30d"],
                "show_error_budget": True,
                "view_type": "detail",
                "view_mode": "both",
            },
            "layout": {"x": 3, "y": last + 1, "width": 3, "height": 3},
        },
        # 4. P99 Latency 圖（右 1/2）
        #    - request 1: 原始 p99 line（dog_classic）
        #    - request 2: anomaly band（robust, bounds=2）
        {
            "definition": {
                "type": "timeseries",
                "title": "P99 Latency (s)",
                "show_legend": True,
                "requests": [
                    {
                        "response_format": "timeseries",
                        "queries": [
                            {
                                "data_source": "metrics",
                                "name": "query1",
                                "query": f"p99:{APM_METRIC}{{{env_filter}}}",
                            }
                        ],
                        "display_type": "line",
                        "style": {"palette": "dog_classic"},
                    },
                    {
                        "response_format": "timeseries",
                        "queries": [
                            {
                                "data_source": "metrics",
                                "name": "anomaly",
                                "query": f"anomalies(p99:{APM_METRIC}{{{env_filter}}}, 'robust', 2)",
                            }
                        ],
                        "display_type": "line",
                        "style": {"palette": "gray"},
                    },
                ],
                "markers": [],
                "events": EVENT_OVERLAY,
            },
            "layout": {"x": 6, "y": last + 1, "width": 6, "height": 3},
        },
        # 5. Error Rate（左 1/2）— 非 200 / 所有 request
        #    - request 1: bars（errors/total×100）
        #    - request 2: anomaly line（agile, bounds=2）
        {
            "definition": {
                "type": "timeseries",
                "title": "Error Rate",
                "show_legend": True,
                "requests": [
                    {
                        "response_format": "timeseries",
                        "queries": [
                            {
                                "data_source": "metrics",
                                "name": "errors",
                                "query": f"sum:{APM_METRIC}.hits{{{env_filter_errors}}}.as_rate()",
                            },
                            {
                                "data_source": "metrics",
                                "name": "total",
                                "query": f"sum:{APM_METRIC}.hits{{{env_filter}}}.as_rate()",
                            },
                        ],
                        "formulas": [
                            {
                                "formula": "errors / total * 100",
                                "alias": "Error Rate (%)",
                            }
                        ],
                        "display_type": "bars",
                        "style": {"palette": "warm"},
                    },
                    {
                        "response_format": "timeseries",
                        "queries": [
                            {
                                "data_source": "metrics",
                                "name": "anomaly",
                                "query": (
                                    f"anomalies("
                                    f"sum:{APM_METRIC}.hits{{{env_filter_errors}}}.as_rate()"
                                    f" / sum:{APM_METRIC}.hits{{{env_filter}}}.as_rate()"
                                    f" * 100, 'agile', 2)"
                                ),
                            }
                        ],
                        "display_type": "line",
                        "style": {"palette": "gray"},
                    },
                ],
                "markers": [],
                "events": EVENT_OVERLAY,
            },
            "layout": {"x": 0, "y": last + 4, "width": 6, "height": 3},
        },
        # 6. Sum(Requests)（右 1/2）
        #    - request 1: as_count line（blue）
        #    - request 2: anomaly line（agile, bounds=2）
        {
            "definition": {
                "type": "timeseries",
                "title": "Sum(Requests)",
                "show_legend": True,
                "requests": [
                    {
                        "response_format": "timeseries",
                        "queries": [
                            {
                                "data_source": "metrics",
                                "name": "query1",
                                "query": f"sum:{APM_METRIC}.hits{{{env_filter}}}.as_count()",
                            }
                        ],
                        "display_type": "line",
                        "style": {"palette": "blue"},
                    },
                    {
                        "response_format": "timeseries",
                        "queries": [
                            {
                                "data_source": "metrics",
                                "name": "query1",
                                "query": f"sum:{APM_METRIC}.hits{{{env_filter}}}.as_rate()",
                            }
                        ],
                        "formulas": [
                            {"formula": "anomalies(query1, 'agile', 2)"}
                        ],
                        "display_type": "line",
                        "style": {"palette": "gray"},
                    },
                ],
                "markers": [],
                "events": EVENT_OVERLAY,
            },
            "layout": {"x": 6, "y": last + 4, "width": 6, "height": 3},
        },
    ]

    put_body = {
        "title": current["title"],
        "layout_type": current["layout_type"],
        "description": current.get("description", ""),
        "widgets": widgets + new_widgets,
    }
    # 保留現有 template_variables（含 $env 等），若無則不設定
    if current.get("template_variables"):
        put_body["template_variables"] = current["template_variables"]

    resp = dd_request("PUT", f"v1/dashboard/{dashboard_id}", put_body)
    return "id" in resp, resp.get("errors")


# ── Main ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Datadog 監控自動設定腳本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--service",          required=True, help="服務名稱，對應 APM service tag (e.g., my-api)")
    parser.add_argument("--endpoint",         required=True, help='端點描述 (e.g., "GET /api/v1/orders")')
    parser.add_argument("--resource-filter",  required=True, help='APM resource_name tag 值，支援 * 萬用字元 (e.g., "get_/api/v4/products*")')
    parser.add_argument("--p99-threshold",    type=float,    help="P99 延遲警報閾值（秒），與 --auto-threshold 擇一使用")
    parser.add_argument("--auto-threshold",   action="store_true", help="自動查詢歷史 P99 並計算建議閾值")
    parser.add_argument("--error-threshold",  type=float, default=0.5, help="錯誤率警報閾值（%%，預設 0.5）")
    parser.add_argument("--threshold-value",  type=int, default=7,
                        help="查詢歷史數據的數量（預設 7，搭配 --threshold-unit 使用）")
    parser.add_argument("--threshold-unit",   choices=["days", "weeks", "months"], default="days",
                        help="查詢歷史數據的單位：days / weeks / months（預設 days）")
    parser.add_argument("--dashboard-id",     help="要新增 section 的 Dashboard ID（可選）")
    parser.add_argument("--priority",         type=int, default=2, help="Monitor priority 1~5（預設 2）")
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

    # 計算查詢天數
    lookup_days = resolve_days(args.threshold_value, args.threshold_unit)
    lookup_label = f"{args.threshold_value} {args.threshold_unit}"

    # Auto threshold（P95 延遲）
    if args.auto_threshold:
        print(f"🔍 查詢過去 {lookup_label} P95 數據...")
        stats = query_p99_stats(service, resource_filter, days=lookup_days)
        if not stats:
            print("   ⚠️  查無數據，請手動指定 --p99-threshold")
            sys.exit(1)
        p99_threshold = suggest_p99_threshold(stats)
        print(f"   min={stats['min']:.2f}s  avg={stats['avg']:.2f}s  max={stats['max']:.2f}s")
        print(f"   建議閾值: {p99_threshold}s（p95 of p95 無條件進位）")
    else:
        p99_threshold = args.p99_threshold

    # 查詢 Request Count 統計（用於 Sum(Requests) 警示線 + Request Rate SLO 閾值）
    print(f"🔍 查詢過去 {lookup_label} Request Count 數據...")
    req_stats = query_request_count_stats(service, resource_filter, days=lookup_days)
    req_count_p99 = None
    req_rate_threshold_rps = None
    if req_stats:
        req_count_p99 = math.ceil(req_stats["p99"])
        # req_stats["p5"] 已是 req/min（已除以 interval_min），再除以 60 換算 req/s
        req_rate_threshold_rps = math.floor(req_stats["p5"] / 60 * 100) / 100  # req/min → req/s
        interval_min = req_stats.get("interval_min", 60)
        print(f"   rollup interval: {interval_min:.0f} min")
        print(f"   avg={req_stats['avg']:.1f}/min  p5={req_stats['p5']:.1f}/min  p99={req_stats['p99']:.1f}/min")
        print(f"   Sum(Requests) 警示線: {req_count_p99} req/min（p99）")
        print(f"   Request Rate SLO 閾值: {req_rate_threshold_rps} req/s（p5）")
    else:
        print("   ⚠️  查無 Request Count 數據，跳過 Request Rate SLO")

    print(f"\n🔧 開始設定監控: [{service}] {endpoint}")
    print(f"   P99 閾值: {p99_threshold}s | 錯誤率閾值: {error_threshold}%")
    print()

    results = {}

    # Step 1: Monitors
    print("📊 Step 1 / 5  建立 Monitors...")

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

    # Request Count Monitor（流量異常偏高告警）
    req_count_monitor_id = None
    if req_count_p99 is not None:
        req_count_monitor_id, err = create_request_count_monitor(
            service, endpoint, resource_filter, req_count_p99, args.priority
        )
        if req_count_monitor_id:
            print(f"  ✅ Request Count Monitor (id: {req_count_monitor_id})")
            results["monitor_request_count"] = req_count_monitor_id
        else:
            print(f"  ⚠️  Request Count Monitor 失敗: {err}（非必要，繼續執行）")

    # Anomaly Monitors（基於歷史數據自動偵測異常）
    anomaly_priority = min(args.priority + 1, 5)  # anomaly 優先級比靜態 monitor 低一級
    err_anomaly_id, err = create_error_rate_anomaly_monitor(
        service, endpoint, resource_filter, anomaly_priority
    )
    if err_anomaly_id:
        print(f"  ✅ Error Rate Anomaly Monitor (id: {err_anomaly_id})")
        results["monitor_error_rate_anomaly"] = err_anomaly_id
    else:
        print(f"  ⚠️  Error Rate Anomaly Monitor 失敗: {err}（非必要，繼續執行）")

    lat_anomaly_id, err = create_p99_latency_anomaly_monitor(
        service, endpoint, resource_filter, anomaly_priority
    )
    if lat_anomaly_id:
        print(f"  ✅ P99 Latency Anomaly Monitor (id: {lat_anomaly_id})")
        results["monitor_p99_latency_anomaly"] = lat_anomaly_id
    else:
        print(f"  ⚠️  P99 Latency Anomaly Monitor 失敗: {err}（非必要，繼續執行）")

    req_anomaly_id, err = create_request_rate_anomaly_monitor(
        service, endpoint, resource_filter, anomaly_priority
    )
    if req_anomaly_id:
        print(f"  ✅ Request Rate Anomaly Monitor (id: {req_anomaly_id})")
        results["monitor_request_rate_anomaly"] = req_anomaly_id
    else:
        print(f"  ⚠️  Request Rate Anomaly Monitor 失敗: {err}（非必要，繼續執行）")

    # Step 2: SLOs（Availability + P99 Latency）
    print("\n🎯 Step 2 / 5  建立 Availability & P99 SLOs...")

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

    # Step 3: Request Rate SLO
    req_rate_slo_id = None
    if req_rate_threshold_rps is not None:
        print(f"\n📈 Step 3 / 5  建立 Request Rate SLO (>= {req_rate_threshold_rps} req/s)...")
        req_rate_slo_id, err = create_request_rate_slo(
            service, endpoint, resource_filter, req_rate_threshold_rps
        )
        if req_rate_slo_id:
            print(f"  ✅ Request Rate SLO    (id: {req_rate_slo_id})")
            results["slo_request_rate"] = req_rate_slo_id
        else:
            print(f"  ❌ Request Rate SLO 失敗: {err}")
    else:
        print(f"\n📈 Step 3 / 5  跳過 Request Rate SLO（無流量數據）")

    # Step 4: Dashboard
    if args.dashboard_id and avail_slo_id and latency_slo_id:
        print(f"\n🖥️  Step 4 / 5  更新 Dashboard ({args.dashboard_id})...")
        ok, err = add_dashboard_section(
            args.dashboard_id, service, endpoint, resource_filter,
            avail_slo_id, latency_slo_id,
            p99_threshold,
        )
        if ok:
            print(f"  ✅ Dashboard section 新增完成")
            results["dashboard"] = args.dashboard_id
        else:
            print(f"  ❌ Dashboard 更新失敗: {err}")
    elif not args.dashboard_id:
        print(f"\n🖥️  Step 4 / 5  跳過 Dashboard（未指定 --dashboard-id）")
    else:
        print(f"\n🖥️  Step 4 / 5  跳過 Dashboard（SLO 建立未完成）")

    # Step 5: Summary
    print(f"\n📋 Step 5 / 5  完成！建立項目摘要：")
    print("\n" + "=" * 50)
    print(f"   Monitor (Error Rate) : {results.get('monitor_error_rate')}")
    print(f"   Monitor (P99 Latency): {results.get('monitor_p99_latency')}")
    print(f"   Monitor (Req Count)  : {results.get('monitor_request_count')}")
    print(f"   Anomaly (Error Rate) : {results.get('monitor_error_rate_anomaly')}")
    print(f"   Anomaly (P99 Latency): {results.get('monitor_p99_latency_anomaly')}")
    print(f"   Anomaly (Req Rate)   : {results.get('monitor_request_rate_anomaly')}")
    print(f"   SLO (Availability)   : {results.get('slo_availability')}")
    print(f"   SLO (P99 Latency)    : {results.get('slo_p99_latency')}")
    print(f"   SLO (Request Rate)   : {results.get('slo_request_rate')}")
    if "dashboard" in results:
        print(f"   Dashboard            : https://{DD_SITE}/dashboard/{results['dashboard']}")
    print()


if __name__ == "__main__":
    main()
