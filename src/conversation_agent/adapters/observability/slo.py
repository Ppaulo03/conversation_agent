"""SLOs as data, and the alert rules and dashboard generated from them.

`ops/slo.yaml` is the single source. Every expression is checked against the metrics the exporter
really emits (`prometheus.available_metrics`), and every SLO names the runbook section that says
what to do when it fires, so neither an alert on a metric that does not exist nor an alert nobody
knows how to act on can be committed. `app.ops check` fails when the generated files are stale.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from conversation_agent.adapters.observability.prometheus import PREFIX, available_metrics

_METRIC = re.compile(rf"{PREFIX}[a-z0-9_]+")


class SLO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9]*$")
    description: str
    expr: str  # PromQL: true when the objective is VIOLATED
    for_: str = Field(alias="for", pattern=r"^\d+[smh]$")
    severity: Literal["warning", "critical"]
    runbook: str = Field(pattern=r"^[a-z0-9-]+$")  # an anchor in docs/OPERATIONS.md
    unit: str = ""


class SLOFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slos: tuple[SLO, ...] = Field(min_length=1)


def load(text: str) -> SLOFile:
    return SLOFile.model_validate(yaml.safe_load(text))


def metrics_in(expr: str) -> set[str]:
    return set(_METRIC.findall(expr))


def problems(slos: SLOFile, runbook_text: str | None = None) -> list[str]:
    found: list[str] = []
    known = available_metrics()
    names = [s.name for s in slos.slos]
    found += [f"duplicate SLO name {n!r}" for n in sorted({n for n in names if names.count(n) > 1})]
    for slo in slos.slos:
        unknown = sorted(m for m in metrics_in(slo.expr) if m not in known)
        found += [f"{slo.name}: unknown metric {m}" for m in unknown]
        if not metrics_in(slo.expr):
            found.append(f"{slo.name}: the expression uses no conversation_agent metric")
        if runbook_text is not None and not re.search(
            rf"^#+ {re.escape(slo.runbook)}\s*$", runbook_text, re.MULTILINE
        ):
            found.append(f"{slo.name}: runbook anchor #{slo.runbook} is not in the runbook")
    return found


def render_rules(slos: SLOFile) -> str:
    rules = [
        {
            "alert": f"ConversationAgent{slo.name}",
            "expr": slo.expr,
            "for": slo.for_,
            "labels": {"severity": slo.severity},
            "annotations": {
                "summary": slo.description,
                "runbook": f"docs/OPERATIONS.md#{slo.runbook}",
            },
        }
        for slo in slos.slos
    ]
    document = {"groups": [{"name": "conversation_agent.slo", "rules": rules}]}
    header = "# GENERATED from ops/slo.yaml by `python -m conversation_agent.app.ops render`.\n"
    return header + yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)


def render_dashboard(slos: SLOFile) -> str:
    panels: list[dict[str, Any]] = []
    for index, slo in enumerate(slos.slos):
        panels.append(
            {
                "id": index + 1,
                "type": "timeseries",
                "title": slo.name,
                "description": slo.description,
                "gridPos": {"h": 7, "w": 12, "x": (index % 2) * 12, "y": (index // 2) * 7},
                "fieldConfig": {"defaults": {"unit": slo.unit or "short"}},
                "targets": [{"expr": slo.expr, "legendFormat": slo.name, "refId": "A"}],
            }
        )
    dashboard = {
        "title": "conversation_agent: runtime health",
        "uid": "conversation-agent-health",
        "schemaVersion": 39,
        "version": 1,
        "tags": ["conversation-agent"],
        "time": {"from": "now-6h", "to": "now"},
        "panels": panels,
    }
    return json.dumps(dashboard, indent=2, ensure_ascii=False) + "\n"
