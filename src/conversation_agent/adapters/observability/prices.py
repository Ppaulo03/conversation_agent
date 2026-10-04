from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from conversation_agent.core.errors import DefinitionError
from conversation_agent.core.llm_prices import PriceTable


def load_prices(path: str | Path) -> PriceTable:
    """The operator-maintained price list (`ops/llm_prices.yaml`). An empty list is valid and
    honest: every model is then reported as UNPRICED instead of free."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return PriceTable.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise DefinitionError(f"invalid price list {path}: {exc}") from exc
