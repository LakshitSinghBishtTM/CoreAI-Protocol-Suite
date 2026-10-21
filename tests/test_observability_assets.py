"""The shipped Grafana dashboard and alert rules only use metrics that exist."""

import json
import re
from pathlib import Path

import yaml

from coreai.metrics import render_prometheus
from tests.test_metrics import STATS

ROOT = Path(__file__).parent.parent
DASHBOARD = ROOT / "deploy/grafana/caips-dashboard.json"
ALERTS = ROOT / "deploy/prometheus/caips-alerts.yml"


def exposed_names():
    stats = dict(STATS)
    stats["latency_bucket_bounds_ms"] = [50, 100]
    stats["model_stats"] = [
        {
            "provider": "b",
            "model": "m",
            "requests": 1,
            "input_tokens": 1,
            "output_tokens": 1,
            "latency_sum_ms": 10.0,
            "latency_buckets": [1, 0, 0],
        }
    ]
    text = render_prometheus(stats)
    return {re.split(r"[{ ]", line)[0] for line in text.splitlines() if line[:1] != "#"}


def used_names(expr):
    return set(re.findall(r"\bcaips_[a-z0-9_]+", expr))


def dashboard_exprs():
    data = json.loads(DASHBOARD.read_text())
    return [t["expr"] for p in data["panels"] for t in p["targets"]]


def alert_exprs():
    data = yaml.safe_load(ALERTS.read_text())
    return [
        r["expr"] for g in data["groups"] for r in g["rules"] if "caips_" in r["expr"]
    ]


def test_dashboard_is_valid_and_panels_do_not_overlap():
    data = json.loads(DASHBOARD.read_text())
    ids = [p["id"] for p in data["panels"]]
    assert len(ids) == len(set(ids)) and data["uid"] == "caips-gateway"
    cells = set()
    for p in data["panels"]:
        g = p["gridPos"]
        assert g["x"] + g["w"] <= 24
        for x in range(g["x"], g["x"] + g["w"]):
            for y in range(g["y"], g["y"] + g["h"]):
                assert (x, y) not in cells, p["title"]
                cells.add((x, y))


def test_dashboard_metrics_exist():
    known = exposed_names()
    for expr in dashboard_exprs():
        assert used_names(expr), expr
        assert used_names(expr) <= known, (expr, used_names(expr) - known)


def test_alert_metrics_exist():
    known = exposed_names()
    for expr in alert_exprs():
        assert used_names(expr) <= known, (expr, used_names(expr) - known)


def test_alert_rules_are_well_formed():
    data = yaml.safe_load(ALERTS.read_text())
    names = []
    for g in data["groups"]:
        for r in g["rules"]:
            names.append(r["alert"])
            assert r["expr"].strip() and r["labels"]["severity"]
            assert r["annotations"]["summary"]
            # Pages within about a minute: no rule waits longer than 2 minutes.
            seconds = {"s": 1, "m": 60}[r["for"][-1]] * int(r["for"][:-1])
            assert seconds <= 120
    assert len(names) == len(set(names))
