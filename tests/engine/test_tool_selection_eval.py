"""The tool selection evaluation: shipped data is consistent and the rules provider holds a floor."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from conversation_agent.app import tool_selection_eval as ev
from conversation_agent.core.definitions.tool_selection import ToolSelectionSettings
from conversation_agent.ports.tool_selector import ToolScore

DATA = Path(__file__).resolve().parents[2] / "examples" / "tool-selection-eval"
ARGS = ["--catalog", str(DATA / "catalog.json"), "--rules", str(DATA / "rules.yaml")]


def test_the_shipped_datasets_only_expect_catalog_tools() -> None:
    catalog = ev.load_catalog(DATA / "catalog.json")
    dev = ev.load_dataset(DATA / "dataset.jsonl", catalog)
    held = ev.load_dataset(DATA / "heldout.jsonl", catalog)
    assert len(dev) >= 50 and len(held) >= 10
    assert not {e.message for e in dev} & {e.message for e in held}


def test_an_unknown_expected_tool_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "d.jsonl"
    path.write_text('{"id": "x", "message": "oi", "expected": ["nope"]}\n', "utf-8")
    with pytest.raises(ValueError, match="not in the catalog"):
        ev.load_dataset(path, ev.load_catalog(DATA / "catalog.json"))


@pytest.mark.parametrize(("dataset", "floor"), [("dataset.jsonl", 0.95), ("heldout.jsonl", 0.75)])
def test_the_rules_provider_keeps_the_right_tool(dataset: str, floor: float, capsys) -> None:  # type: ignore[no-untyped-def]
    code = ev.main([*ARGS, "--dataset", str(DATA / dataset), "--min-hit-rate", str(floor)])
    assert code == 0
    assert "hit rate" in capsys.readouterr().out


def test_the_report_counts_saving_and_fallbacks() -> None:
    import asyncio

    catalog = ev.load_catalog(DATA / "catalog.json")
    examples = ev.load_dataset(DATA / "dataset.jsonl", catalog)
    selector = ev.load_selector("rules", DATA / "rules.yaml")
    report = asyncio.run(ev.evaluate(selector, catalog, examples, ToolSelectionSettings()))
    assert report.examples == len(examples) and report.catalog_tools == len(catalog)
    assert 0 < report.tools_removed < 1 and 0 <= report.fallback_rate < 1
    assert report.per_tool_recall["services_list"] > 0


def test_any_selector_can_be_plugged_by_module_path(monkeypatch: pytest.MonkeyPatch) -> None:
    class Everything:
        async def rank(self, message, candidates, *, tenant_id):  # type: ignore[no-untyped-def]
            return [ToolScore(t.name, 1.0) for t in candidates]

    module = types.ModuleType("fake_provider")
    module.factory = Everything  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_provider", module)
    catalog = ev.load_catalog(DATA / "catalog.json")
    examples = ev.load_dataset(DATA / "dataset.jsonl", catalog)
    import asyncio

    report = asyncio.run(
        ev.evaluate(
            ev.load_selector("fake_provider:factory", None),
            catalog,
            examples,
            ToolSelectionSettings(top_n=8),
        )
    )
    assert report.hit_rate == 1.0 and report.tools_removed == 0.0  # keeps all: no saving


def test_the_large_catalog_and_its_datasets_are_consistent() -> None:
    catalog = ev.load_catalog(DATA / "large" / "catalog.json")
    dev = ev.load_dataset(DATA / "large" / "dataset.jsonl", catalog)
    held = ev.load_dataset(DATA / "large" / "heldout.jsonl", catalog)
    assert len(catalog) >= 40 and len(dev) >= 100 and len(held) >= 40
    assert not {e.message for e in dev} & {e.message for e in held}
    assert {t for e in dev for t in e.expected} == {
        t.name for t in catalog
    }  # every tool is covered


def test_a_sweep_asks_the_provider_once_per_message_and_orders_the_saving() -> None:
    import asyncio

    calls = 0

    class Graded:
        async def rank(self, message, candidates, *, tenant_id):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            return [ToolScore(t.name, 1 / (1 + i)) for i, t in enumerate(candidates)]

    catalog = ev.load_catalog(DATA / "catalog.json")
    examples = ev.load_dataset(DATA / "dataset.jsonl", catalog)
    results = asyncio.run(
        ev.sweep(Graded(), catalog, examples, ToolSelectionSettings(top_n=8), [0.5, 0.2, 0.05])
    )
    assert calls == len(examples)
    kept = [r.tools_kept for _, r in results]
    assert kept == sorted(kept) and kept[0] < kept[-1]
    table = ev.render_sweep(results)
    assert "min_score" in table and "0.05" in table
