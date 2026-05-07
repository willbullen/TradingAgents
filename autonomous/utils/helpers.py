"""
autonomous/utils/helpers.py
============================
Shared utility functions for the autonomous trading system.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REPORTS_DIR = Path.home() / ".tradingagents" / "autonomous_reports"


def is_market_open() -> bool:
    """
    Simple check: returns True if current time is within US market hours
    (9:30 AM – 4:00 PM ET, Mon–Fri). Does not account for holidays.
    For production, use the Alpaca calendar API instead.
    """
    from datetime import timezone
    import pytz

    et = pytz.timezone("America/New_York")
    now_et = datetime.now(et)

    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False

    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now_et <= market_close


def format_decision_summary(results: List[Dict]) -> str:
    """
    Format a list of run results into a human-readable summary table.

    Parameters
    ----------
    results : list of dict
        Each dict should have keys: ticker, rating, execution.

    Returns
    -------
    str
        Formatted summary string.
    """
    lines = [
        "",
        "=" * 72,
        f"  AUTONOMOUS TRADING SUMMARY — {date.today()}",
        "=" * 72,
        f"  {'TICKER':<8} {'RATING':<14} {'ACTION':<10} {'DETAILS'}",
        "  " + "-" * 68,
    ]
    for r in results:
        if "ticker" not in r:
            continue
        exec_info = r.get("execution", {})
        lines.append(
            f"  {r['ticker']:<8} {r.get('rating', 'N/A'):<14} "
            f"{exec_info.get('action', 'n/a'):<10} "
            f"{exec_info.get('message', '')[:40]}"
        )
    lines.append("=" * 72)
    return "\n".join(lines)


def save_json_report(data: Any, filename: str) -> Path:
    """Save a JSON report to the autonomous reports directory."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return path


def load_json_report(filename: str) -> Optional[Dict]:
    """Load a JSON report from the autonomous reports directory."""
    path = REPORTS_DIR / filename
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def rating_to_emoji(rating: str) -> str:
    """Convert a TradingAgents rating to a display emoji."""
    mapping = {
        "Buy":         "🟢 Buy",
        "Overweight":  "🔵 Overweight",
        "Hold":        "🟡 Hold",
        "Underweight": "🟠 Underweight",
        "Sell":        "🔴 Sell",
    }
    return mapping.get(rating, rating)
