"""Validate the generated observability artifacts against the real tools (F-009).

`app.ops check` only proves the committed files equal what `render` produces. This script proves
they are valid for the tools they claim: the alert rules and every dashboard query are parsed by
Prometheus's own `promtool`, and the dashboard is imported into a running Grafana and read back.

    python ops/validate_artifacts.py --promtool promtool [--grafana-url http://127.0.0.1:3000]

Standard library only, so it runs without the project environment. The Grafana must accept anonymous
admin requests (a throwaway instance, as in CI); the dashboard is imported with overwrite.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

OPS = Path(__file__).resolve().parent
RULES = OPS / "alerts.rules.yml"
DASHBOARD = OPS / "grafana-dashboard.json"


def promtool_check(promtool: str, path: Path) -> None:
    subprocess.run([promtool, "check", "rules", str(path)], check=True)


def dashboard_queries(dashboard: dict[str, Any]) -> list[str]:
    exprs = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", []) if t.get("expr")]
    if not exprs:
        raise SystemExit(f"{DASHBOARD.name}: no Prometheus queries found")
    return exprs


def queries_as_rules(exprs: list[str]) -> str:
    # promtool parses PromQL only inside rule files, so each query becomes a recording rule.
    rules = [{"record": f"dashboard:query_{i}", "expr": e} for i, e in enumerate(exprs)]
    # JSON is valid YAML, so no YAML library is needed.
    return json.dumps({"groups": [{"name": "dashboard-queries", "rules": rules}]})


def grafana_roundtrip(url: str, dashboard: dict[str, Any]) -> None:
    body = json.dumps({"dashboard": {**dashboard, "id": None}, "overwrite": True}).encode()
    request = urllib.request.Request(
        f"{url}/api/dashboards/db",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        saved = json.load(response)
    if saved.get("status") != "success":
        raise SystemExit(f"Grafana refused the dashboard: {saved}")
    with urllib.request.urlopen(f"{url}/api/dashboards/uid/{dashboard['uid']}") as response:
        stored = json.load(response)["dashboard"]
    expected = [p["title"] for p in dashboard["panels"]]
    if [p["title"] for p in stored["panels"]] != expected:
        raise SystemExit("Grafana stored a different set of panels than the file declares")
    version = stored["schemaVersion"]
    print(
        f"Grafana: imported {dashboard['uid']!r}, {len(expected)} panels, schemaVersion {version}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--promtool", default="promtool")
    parser.add_argument("--grafana-url", help="a running Grafana; skipped when omitted")
    args = parser.parse_args()

    promtool_check(args.promtool, RULES)
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        queries = Path(tmp) / "dashboard-queries.yml"
        queries.write_text(queries_as_rules(dashboard_queries(dashboard)), encoding="utf-8")
        promtool_check(args.promtool, queries)
    if args.grafana_url:
        grafana_roundtrip(args.grafana_url.rstrip("/"), dashboard)
    else:
        print("Grafana: skipped (no --grafana-url)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
