"""In-process Splink fallback — no match logic lives here.

Single-container topology: the same match interface (same input shape, same
output shape) behind a function call instead of HTTP. The computation is
`shared/splink_core.py` (single source of truth), imported here and by the
`matching-engine` service alike: fix once, both transports change. Only
mosaic-api's in-process fallback path imports this module (lazy import);
the three-container flow never calls it. The Liskov parity test still pins
service-vs-fallback agreement on the hidden fixtures.
"""

from __future__ import annotations

from shared.splink_core import (
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
