"""Monthly Sales Chart data — actual (blue bars) vs target (green line) for the
current month and the previous 4 (5 points total). Hover values live in the
per-point arrays."""
from __future__ import annotations

from typing import Any, Optional


def build_chart(
    periods: list[str],
    sales: list[float],
    targets: list[Optional[float]],
    months: int = 5,
) -> dict[str, Any]:
    """The trailing ``months`` points (default 5 = current + previous 4)."""
    k = min(months, len(periods))
    window = slice(len(periods) - k, len(periods))
    return {
        "periods": periods[window],
        "actual": sales[window],
        "target": targets[window],
    }
