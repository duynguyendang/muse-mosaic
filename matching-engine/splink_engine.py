"""Matching-engine transport wrapper — no match logic lives here.

The match computation is `shared/splink_core.py` (single source of truth);
this module only re-exports it under the historic `splink_engine` name so
`main.py` and the service entrypoint are unchanged. The in-process fallback
(`mosaic-api/splink_local.py`) imports the same module: fix once, both
transports change. The Liskov parity test still pins service-vs-fallback
agreement on the hidden fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running the service from its own directory (pytest spawns uvicorn
# with cwd=matching-engine; the container runs with WORKDIR /app where
# ./shared is already on sys.path). No-op when already importable.
try:
    import shared.splink_core  # noqa: F401
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.splink_core import (  # noqa: E402
    BLOCKING_COLUMNS,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    SUPPORTED_COMPARISONS,
    blocking_rule_for,
    blocking_rules,
    comparisons,
    connected_components,
    run_match,
)

__all__ = [
    "BLOCKING_COLUMNS",
    "STATUS_OK",
    "STATUS_UNAVAILABLE",
    "SUPPORTED_COMPARISONS",
    "blocking_rule_for",
    "blocking_rules",
    "comparisons",
    "connected_components",
    "run_match",
]
