"""The web domain map: each Atlas asset's official website domain and
English Wikipedia article, verified, for web ranking datasets.

Built and refreshed by ``integrations/web`` (see
``docs/web-domain-map.md``); this module only locates and reads it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data" / "web"
DOMAIN_MAP_PATH = DATA_DIR / "domain_map.json"
OVERRIDES_PATH = DATA_DIR / "overrides.json"


def load_domain_map(path: Path = DOMAIN_MAP_PATH) -> dict[str, Any]:
    """The map file: ``schema_version``, ``generated_at``, ``atlas_source``,
    ``entries`` (sorted by ``ticker``) and ``unmapped`` (key -> reason)."""
    return json.loads(path.read_text(encoding="utf-8"))
