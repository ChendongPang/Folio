"""Operator-owned execution policy; never supplied by document text or model actions."""

import json
import os
from pathlib import Path


def load_research_policy(store_root: Path) -> dict:
    path = Path(os.environ.get("FOLIO_RESEARCH_CONFIG", "")) if os.environ.get(
        "FOLIO_RESEARCH_CONFIG"
    ) else Path(store_root) / "settings" / "research.json"
    if not path.is_file():
        return {}
    if path.stat().st_size > 1000000:
        raise ValueError("Research operator policy exceeds the supported size")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Research operator policy must be a JSON object")
    return value
