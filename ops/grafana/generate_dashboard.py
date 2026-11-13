"""Generate ops/grafana/dashboards/mediaflow.json. Edit here, then run:
    python3 ops/grafana/generate_dashboard.py ops/grafana/dashboards/mediaflow.json
"""
import json
import sys

DS = {"type": "prometheus", "uid": "prometheus"}
panels = []
_id = 0


def nid():
    global _id
    _id += 1
    return _id


def row(title, y):
    panels.append({"type": "row", "title": title, "id": nid(), "collapsed": False,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}})


def stat(title, expr, x, y, w=4, unit="short", steps=None, desc=""):
    panels.append({
        "type": "stat", "title": title, "id": nid(), "datasource": DS, "description": desc,
        "gridPos": {"h": 4, "w": w, "x": x, "y": y},
        "targets": [{"refId": "A", "datasource": DS, "expr": expr, "instant": True}],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background",
                    "graphMode": "area", "textMode": "value"},
        "fieldConfig": {"defaults": {
            "unit": unit, "noValue": "0",
            "thresholds": {"mode": "absolute",
                           "steps": steps or [{"color": "green", "value": None}]},
        }, "overrides": []},
    })


def ts(title, targets, x, y, w=12, h=8, unit="short", stack=False, desc="", lines=None):
    defaults = {"unit": unit, "custom": {"fillOpacity": 15 if stack else 5, "lineWidth": 2,
                                         "stacking": {"mode": "normal" if stack else "none"},
                                         "showPoints": "never"}}
    if lines:
        defaults["custom"]["thresholdsStyle"] = {"mode": "line+area"}
        defaults["thresholds"] = {"mode": "absolute", "steps": lines}
    panels.append({
        "type": "timeseries", "title": title, "id": nid(), "datasource": DS, "description": desc,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": legend}
                    for i, (e, legend) in enumerate(targets)],
        "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "fieldConfig": {"defaults": defaults, "overrides": []},
    })


GREEN, AMBER, RED = "green", "orange", "red"


def levels(base, *steps):
    """Threshold steps: a base colour, then (colour, from_value) pairs."""
    return [{"color": base, "value": None}] + [{"color": c, "value": v} for c, v in steps]


y = 0
row("Right now", y)
y += 1
stat("Waiting to start", 'sum(mediaflow_queue_depth{state="ready"})', 0, y,
     steps=levels(GREEN, (AMBER, 20), (RED, 100)),
     desc="Jobs in the ready list.")
stat("Oldest waiting", "max(mediaflow_queue_oldest_ready_age_seconds)", 4, y, unit="s",
     steps=levels(GREEN, (AMBER, 60), (RED, 300)),
     desc="How long the next job to be picked up has waited since upload. Alert: >5 min.")
stat("Analysing", 'sum(mediaflow_queue_depth{state="processing"})', 8, y,
     desc="Jobs a worker holds a lease on.")
stat("Retrying", 'sum(mediaflow_queue_depth{state="deferred"})', 12, y,
     steps=levels(GREEN, (AMBER, 10)))
stat("Dead letters", 'sum(mediaflow_queue_depth{state="dead"})', 16, y,
     steps=levels(GREEN, (RED, 1)),
     desc="Jobs that ran out of retries. See runbook: MediaFlowDeadLetters.")
stat("Workers up", 'sum(up{job="mediaflow-worker"})', 20, y,
     steps=levels(RED, (GREEN, 1)))
y += 4
stat("Gemini quota used", "max(mediaflow_gemini_calls_today) / max(mediaflow_gemini_daily_limit)",
     0, y, unit="percentunit",
     steps=levels(GREEN, (AMBER, 0.7), (RED, 0.9)))
stat("Rate-limit pause", "max(mediaflow_gemini_cooldown_seconds)", 4, y, unit="s",
     steps=levels(GREEN, (AMBER, 1)),
     desc="Fleet-wide pause after a 429 from Gemini.")
stat("Redis", "min(mediaflow_redis_up)", 8, y,
     steps=levels(RED, (GREEN, 1)))
stat("API instances", 'sum(up{job="mediaflow-api"})', 12, y,
     steps=levels(RED, (GREEN, 1)))
stat("p95 upload → draft (30m)",
     "histogram_quantile(0.95, sum by (le) (rate(mediaflow_job_end_to_end_seconds_bucket[30m])))",
     16, y, unit="s", w=8,
     steps=levels(GREEN, (AMBER, 60), (RED, 120)),
     desc="The user-facing SLI. SLO: 95% of jobs within 2 minutes.")
y += 4

row("Jobs", y)
y += 1
ts("Job outcomes per minute",
   [('sum by (outcome) (rate(mediaflow_job_outcomes_total[5m])) * 60', "{{outcome}}")],
   0, y, stack=True, unit="short",
   desc="Every worker attempt: done, cached, retry, deferred (rate-limited), failed, dead.")
ts("Failure ratio",
   [('(sum(rate(mediaflow_job_outcomes_total{outcome=~"failed|dead"}[15m])) or vector(0)) / '
     'sum(rate(mediaflow_job_outcomes_total{outcome=~"done|cached|failed|dead"}[15m]))',
     "failed or dead")],
   12, y, unit="percentunit", lines=[{"color": "transparent", "value": None},
                                     {"color": RED, "value": 0.1}],
   desc="Alert above 10% for 10 minutes.")
y += 8
ts("Time in queue (upload → started)",
   [("histogram_quantile(0.5, sum by (le) (rate(mediaflow_job_queue_wait_seconds_bucket[5m])))",
     "p50"),
    ("histogram_quantile(0.95, sum by (le) (rate(mediaflow_job_queue_wait_seconds_bucket[5m])))",
     "p95")], 0, y, unit="s")
ts("Upload → draft stored",
   [("histogram_quantile(0.5, sum by (le) (rate(mediaflow_job_end_to_end_seconds_bucket[5m])))",
     "p50"),
    ("histogram_quantile(0.95, sum by (le) (rate(mediaflow_job_end_to_end_seconds_bucket[5m])))",
     "p95")], 12, y, unit="s",
   lines=levels("transparent", (RED, 120)),
   desc="Red line: the 2-minute SLO.")
y += 8

row("Gemini", y)
y += 1
ts("Analysis call latency (p95)",
   [("histogram_quantile(0.95, sum by (le, kind) "
     "(rate(mediaflow_gemini_request_seconds_bucket[5m])))", "{{kind}}")], 0, y, unit="s")
ts("Analysis calls by result",
   [("sum by (result) (rate(mediaflow_gemini_request_seconds_count[5m])) * 60", "{{result}}")],
   12, y, stack=True, desc="ok, rate_limited (429), transient (5xx/network), permanent, error.")
y += 8

row("API", y)
y += 1
ts("Requests per second by status",
   [('sum by (status) (rate(mediaflow_http_requests_total{route!~"/health/.*"}[5m]))',
     "{{status}}")], 0, y, stack=True, unit="reqps")
ts("p95 latency by route (top 8)",
   [('topk(8, histogram_quantile(0.95, sum by (le, method, route) '
     '(rate(mediaflow_http_request_duration_seconds_bucket{route!~"/health/.*"}[5m]))))',
     "{{method}} {{route}}")], 12, y, unit="s",
   desc="POST /api/v1/jobs waits for the result by design, so it is slow on purpose.")
y += 8

row("Consistency and housekeeping", y)
y += 1
ts("Reconciler repairs",
   [('sum by (action) (increase(mediaflow_reconciler_actions_total{action!="unchanged"}[15m]))',
     "{{action}}")], 0, y, stack=True,
   desc="Any 'recovered', 'requeued' or 'abandoned' means Redis lost data. See JOB_LIFECYCLE.md.")
ts("Uploads accepted and originals purged",
   [("sum by (status) (increase(mediaflow_jobs_accepted_total[15m]))", "accepted: {{status}}"),
    ("sum(increase(mediaflow_retention_purged_total[1h]))", "purged by retention (1h)")],
   12, y)

dashboard = {
    "uid": "mediaflow-ops", "title": "MediaFlow – Operations", "tags": ["mediaflow"],
    "timezone": "browser", "schemaVersion": 39, "version": 1, "refresh": "10s",
    "time": {"from": "now-1h", "to": "now"}, "editable": False, "graphTooltip": 1,
    "description": "Queue health, job SLI, Gemini, API and DB<->Redis consistency. "
                   "Runbook: docs/OPERATIONS.md",
    "links": [],
    "panels": panels, "templating": {"list": []}, "annotations": {"list": []},
}
json.dump(dashboard, open(sys.argv[1], "w"), indent=2)
print(f"{len(panels)} panels")
